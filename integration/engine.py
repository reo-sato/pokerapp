from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Callable, Optional

from core.event_queue import EventQueue
from core.events import AudioEvent
from core.game_state import GameStateManager, Street
from core.hand_log import ActionRecord, HandSummary
from output.json_writer import JsonWriter

logger = logging.getLogger(__name__)

# Phase 1: 音声イベントのみ処理。カメラとの ±2秒マッチングは Phase 3 で追加。


class IntegrationThread(threading.Thread):
    """Phase 1 簡易版: audio_queue を消費して game_state を更新し、
    ActionRecord を JsonWriter に書き出す。

    GUI通知とカメラとの ±2秒マッチングは Phase 3 以降で追加する。
    """

    def __init__(
        self,
        audio_queue: EventQueue,
        game_state: GameStateManager,
        json_writer: JsonWriter,
        on_action: Optional[Callable[[ActionRecord], None]] = None,
        stop_event: Optional[threading.Event] = None,
    ) -> None:
        super().__init__(daemon=True, name="IntegrationThread")
        self._audio_queue = audio_queue
        self._game_state = game_state
        self._json_writer = json_writer
        self._on_action = on_action  # GUI通知用コールバック（Phase 4 で活用）
        self._stop_event = stop_event or threading.Event()

        # ハンド内の一時アクションバッファ（hand_summary 生成用）
        self._current_actions: list[ActionRecord] = []
        self._hand_started_at: str = _now_iso()
        self._stack_start: dict[int, int] = {}

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        logger.info("IntegrationThread started")
        while not self._stop_event.is_set():
            try:
                event = self._audio_queue.get(timeout=0.5)
            except Exception:
                continue

            try:
                self._handle_audio_event(event)
            except Exception:
                logger.exception("Error handling audio event: %s", event)

        logger.info("IntegrationThread stopped")

    # ――― イベントハンドラ ―――

    def _handle_audio_event(self, event: AudioEvent) -> None:
        action = event.action
        gs = self._game_state

        if action == "new_hand":
            self._start_new_hand()
            return

        if action == "showdown":
            gs.advance_street(Street.SHOWDOWN)
            return

        if action == "winner":
            # FR-17: 音声のみ → 現在ターンプレイヤーを推定して winner とする
            # "シート3 ウィナー" のような発話から席番号を抽出する試み
            winner_seat = _extract_seat_from_text(event.raw_text)
            if winner_seat is None:
                winner_seat = gs.get_current_player()
                logger.warning(
                    "Could not extract winner seat from %r, using current player seat=%d",
                    event.raw_text, winner_seat,
                )
            self._finalize_hand(winner_seat)
            return

        # 通常アクション: bet / call / raise / check / fold / allin
        seat = gs.get_current_player()

        try:
            gs.apply_action(seat, action, event.amount)
        except ValueError:
            logger.exception("apply_action failed (seat=%d, action=%s)", seat, action)
            needs_review = True
        else:
            needs_review = False

        record = ActionRecord(
            hand_id=gs.hand_id,
            timestamp=_now_iso(),
            street=gs.street,
            seat=seat,
            player_name=gs.get_player_name(seat),
            action=action,
            amount=event.amount,
            pot_after=gs.pot,
            stack_after=gs.get_stack(seat),
            source={"camera": False, "audio": True, "rfid": False},
            needs_review=needs_review,
            confidence=0.0,
        )
        self._current_actions.append(record)

        if self._on_action:
            self._on_action(record)

        # ターン進行は apply_action() 内で完結するため、ここでは呼ばない
        logger.debug("ActionRecord: %s", record)

    # ――― ハンド開始 / 終了 ―――

    def _start_new_hand(self) -> None:
        gs = self._game_state
        gs.new_hand()
        self._current_actions = []
        self._hand_started_at = _now_iso()
        self._stack_start = gs.get_stacks()
        logger.info("New hand started: hand_id=%d", gs.hand_id)

    def _finalize_hand(self, winner_seat: int) -> None:
        gs = self._game_state
        gs.end_hand(winner_seat)

        stacks_end = gs.get_stacks()
        players_info = [
            {
                "seat": seat,
                "name": gs.get_player_name(seat),
                "hole_cards": None,
                "stack_start": self._stack_start.get(seat, 0),
                "stack_end": stacks_end[seat],
                "result": stacks_end[seat] - self._stack_start.get(seat, 0),
            }
            for seat in sorted(stacks_end.keys())
        ]

        summary = HandSummary(
            hand_id=gs.hand_id,
            session_id=self._json_writer._session_id,
            started_at=self._hand_started_at,
            ended_at=_now_iso(),
            blinds={"sb": gs._sb, "bb": gs._bb},  # noqa: SLF001
            board=[],
            board_source="",
            players=players_info,
            pot_total=sum(a.amount for a in self._current_actions if a.action in ("bet", "raise", "call", "allin")),
            winner_seat=winner_seat,
            actions=list(self._current_actions),
            review_required=any(a.needs_review for a in self._current_actions),
        )

        self._json_writer.append_hand_summary(summary)
        logger.info("Hand %d finalized. Winner: seat %d", gs.hand_id, winner_seat)
        self._current_actions = []


# ――― ユーティリティ ―――

def _now_iso() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def _extract_seat_from_text(text: str) -> Optional[int]:
    """テキストから席番号を抽出する。例: "シート3 ウィナー" → 3。"""
    import re

    m = re.search(r"(?:シート|seat)\s*(\d+)", text, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return None
