from __future__ import annotations

import logging
import queue
import threading
import time
from datetime import datetime
from typing import Callable, Optional

from core.event_queue import EventQueue
from core.events import AudioEvent, CameraEvent
from core.game_state import GameStateManager, Street
from core.hand_log import ActionRecord, HandSummary
from output.json_writer import JsonWriter

logger = logging.getLogger(__name__)

# Phase 3: audio_queue + camera_queue を統合し、±MATCH_WINDOW 秒以内の
# 同席イベント同士をマッチングして confidence スコアを算出する。
# RFID との統合は Phase 7 で追加する。

MATCH_WINDOW = 2.0   # 秒: この時間幅内のカメライベントをマッチング対象とする
CAMERA_BUFFER_TTL = MATCH_WINDOW * 2  # 秒: カメラバッファの最大保持時間

# Confidence スコア定数（v2.0 §1.3 優先度: RFID > audio > camera）
_CONF_AUDIO_ONLY = 0.5
_CONF_AUDIO_CAMERA = 0.8   # 音声 + カメラで一致
# RFID は Phase 7 で追加: _CONF_RFID_ONLY=0.7, _CONF_RFID_AUDIO=0.95 等


class IntegrationThread(threading.Thread):
    """Phase 3: audio_queue と camera_queue を消費して game_state を更新し、
    ActionRecord を JsonWriter に書き出す。

    ±MATCH_WINDOW 秒以内に同じ席の CameraEvent が存在すれば source.camera=True
    とし、confidence スコアを引き上げる。
    """

    def __init__(
        self,
        audio_queue: EventQueue,
        game_state: GameStateManager,
        json_writer: JsonWriter,
        camera_queue: Optional[EventQueue] = None,
        on_action: Optional[Callable[[ActionRecord], None]] = None,
        stop_event: Optional[threading.Event] = None,
    ) -> None:
        super().__init__(daemon=True, name="IntegrationThread")
        self._audio_queue = audio_queue
        self._camera_queue = camera_queue
        self._game_state = game_state
        self._json_writer = json_writer
        self._on_action = on_action  # GUI通知用コールバック（Phase 4 で活用）
        self._stop_event = stop_event or threading.Event()

        # カメライベントのバッファ（audio イベント処理時に照合する）
        self._camera_buffer: list[CameraEvent] = []

        # ハンド内の一時アクションバッファ（hand_summary 生成用）
        self._current_actions: list[ActionRecord] = []
        self._hand_started_at: str = _now_iso()
        self._stack_start: dict[int, int] = {}

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        logger.info("IntegrationThread started")
        while not self._stop_event.is_set():
            # 1. カメラキューを非ブロッキングで全件バッファに溜める
            self._drain_camera_queue()

            # 2. 音声イベントを短いタイムアウトで取得（カメラ drain を定期実行するため短くする）
            try:
                event = self._audio_queue.get(timeout=0.1)
            except queue.Empty:
                self._expire_camera_buffer()
                continue

            try:
                self._handle_audio_event(event)
            except Exception:
                logger.exception("Error handling audio event: %s", event)

            # 3. 古いカメライベントを破棄
            self._expire_camera_buffer()

        logger.info("IntegrationThread stopped")

    # ――― カメラバッファ管理 ―――

    def _drain_camera_queue(self) -> None:
        """camera_queue の全イベントをバッファに移す（非ブロッキング）。"""
        if self._camera_queue is None:
            return
        while True:
            try:
                ev = self._camera_queue.get_nowait()
                self._camera_buffer.append(ev)
            except queue.Empty:
                break

    def _expire_camera_buffer(self) -> None:
        """CAMERA_BUFFER_TTL より古いカメライベントをバッファから除去する。"""
        cutoff = time.time() - CAMERA_BUFFER_TTL
        self._camera_buffer = [e for e in self._camera_buffer if e.timestamp >= cutoff]

    def _pop_matching_camera_event(self, seat: int, audio_ts: float) -> Optional[CameraEvent]:
        """seat かつ audio_ts の ±MATCH_WINDOW 秒以内の CameraEvent を返し、
        バッファから除去する。複数あれば最も近いものを選ぶ。
        """
        candidates = [
            e for e in self._camera_buffer
            if e.seat == seat and abs(e.timestamp - audio_ts) <= MATCH_WINDOW
        ]
        if not candidates:
            return None
        best = min(candidates, key=lambda e: abs(e.timestamp - audio_ts))
        self._camera_buffer.remove(best)
        return best

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

        # カメライベントとのマッチング
        cam_event = self._pop_matching_camera_event(seat, event.timestamp)
        if cam_event is not None:
            source = {"camera": True, "audio": True, "rfid": False}
            confidence = _CONF_AUDIO_CAMERA
            logger.debug(
                "Camera corroboration: seat=%d, audio_ts=%.3f, cam_ts=%.3f, Δ=%.3fs",
                seat, event.timestamp, cam_event.timestamp,
                abs(event.timestamp - cam_event.timestamp),
            )
        else:
            source = {"camera": False, "audio": True, "rfid": False}
            confidence = _CONF_AUDIO_ONLY

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
            source=source,
            needs_review=needs_review,
            confidence=confidence,
        )
        self._current_actions.append(record)

        if self._on_action:
            self._on_action(record)

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
