"""integration/engine.py

Phase 7: audio / camera / RFID の 3 ソースを統合し、confidence スコアを算出する。

ソース優先度 (v2.0 §1.3): RFID > audio > camera

Confidence 行列:
  RFID + audio + camera : 1.00
  RFID + audio          : 0.95
  RFID + camera         : 0.85
  RFID のみ             : 0.70
  audio + camera        : 0.80
  audio のみ            : 0.50
  camera のみ           : 0.30
  なし                  : 0.00

マッチングウィンドウ:
  ±MATCH_WINDOW (2.0 秒) 以内の同席イベントを照合する。
  それ以上古いイベントは CAMERA_BUFFER_TTL (4.0 秒) で破棄する。

ボードカード:
  role="board" の RFIDEvent を受信したら HandSummary.board に追記する。
  board_source = "rfid" として記録する。
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from datetime import datetime
from typing import Callable, Optional

from core.event_queue import EventQueue
from core.events import AudioEvent, CameraEvent, RFIDEvent
from core.game_state import GameStateManager, Street
from core.hand_log import ActionRecord, HandSummary
from output.json_writer import JsonWriter

logger = logging.getLogger(__name__)

MATCH_WINDOW = 2.0             # 秒: マッチング対象とする時間幅
CAMERA_BUFFER_TTL = MATCH_WINDOW * 2  # 秒: バッファの最大保持時間

# ――― Confidence スコア定数 ―――
_CONF_RFID_AUDIO_CAMERA = 1.00
_CONF_RFID_AUDIO        = 0.95
_CONF_RFID_CAMERA       = 0.85
_CONF_RFID_ONLY         = 0.70
_CONF_AUDIO_CAMERA      = 0.80
_CONF_AUDIO_ONLY        = 0.50
_CONF_CAMERA_ONLY       = 0.30


def calc_confidence(has_rfid: bool, has_audio: bool, has_camera: bool) -> float:
    """センサー組み合わせから confidence スコアを返す。

    Args:
        has_rfid:   RFID イベントがマッチしたか
        has_audio:  音声イベントがあるか（通常 True）
        has_camera: カメライベントがマッチしたか
    """
    if has_rfid and has_audio and has_camera:
        return _CONF_RFID_AUDIO_CAMERA
    if has_rfid and has_audio:
        return _CONF_RFID_AUDIO
    if has_rfid and has_camera:
        return _CONF_RFID_CAMERA
    if has_rfid:
        return _CONF_RFID_ONLY
    if has_audio and has_camera:
        return _CONF_AUDIO_CAMERA
    if has_audio:
        return _CONF_AUDIO_ONLY
    if has_camera:
        return _CONF_CAMERA_ONLY
    return 0.0


class IntegrationThread(threading.Thread):
    """Phase 7: audio / camera / RFID の 3 キューを消費して game_state を更新し、
    ActionRecord を JsonWriter に書き出す。

    各 AudioEvent に対し、±MATCH_WINDOW 秒以内の同席 CameraEvent / RFIDEvent を
    照合して source フラグと confidence スコアを決定する。
    role="board" の RFIDEvent はハンドのボードカードとして蓄積する。
    """

    def __init__(
        self,
        audio_queue: EventQueue,
        game_state: GameStateManager,
        json_writer: JsonWriter,
        camera_queue: Optional[EventQueue] = None,
        rfid_queue: Optional[EventQueue] = None,
        on_action: Optional[Callable[[ActionRecord], None]] = None,
        stop_event: Optional[threading.Event] = None,
    ) -> None:
        super().__init__(daemon=True, name="IntegrationThread")
        self._audio_queue = audio_queue
        self._camera_queue = camera_queue
        self._rfid_queue = rfid_queue
        self._game_state = game_state
        self._json_writer = json_writer
        self._on_action = on_action
        self._stop_event = stop_event or threading.Event()

        # センサーイベントのバッファ（AudioEvent 処理時に照合する）
        self._camera_buffer: list[CameraEvent] = []
        self._rfid_seat_buffer: list[RFIDEvent] = []  # role="seat" のみ

        # ハンド内の一時バッファ
        self._current_actions: list[ActionRecord] = []
        self._hand_started_at: str = _now_iso()
        self._stack_start: dict[int, int] = {}
        self._board_cards: list[str] = []   # role="board" RFID で検出したボードカード
        self._board_source: str = ""

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        logger.info("IntegrationThread started")
        while not self._stop_event.is_set():
            # 1. カメラ・RFID キューを非ブロッキングで全件バッファに溜める
            self._drain_camera_queue()
            self._drain_rfid_queue()

            # 2. 音声イベントを短いタイムアウトで取得
            try:
                event = self._audio_queue.get(timeout=0.1)
            except queue.Empty:
                self._expire_buffers()
                continue

            try:
                self._handle_audio_event(event)
            except Exception:
                logger.exception("Error handling audio event: %s", event)

            # 3. 古いイベントを破棄
            self._expire_buffers()

        logger.info("IntegrationThread stopped")

    # ――― バッファ管理 ―――

    def _drain_camera_queue(self) -> None:
        if self._camera_queue is None:
            return
        while True:
            try:
                self._camera_buffer.append(self._camera_queue.get_nowait())
            except queue.Empty:
                break

    def _drain_rfid_queue(self) -> None:
        if self._rfid_queue is None:
            return
        while True:
            try:
                ev: RFIDEvent = self._rfid_queue.get_nowait()
                if ev.role == "board":
                    # ボードカードはバッファではなく直接リストに追加
                    if ev.card:
                        self._board_cards.append(ev.card)
                        if not self._board_source:
                            self._board_source = "rfid"
                        logger.debug("Board card detected: %s (reader=%s)", ev.card, ev.reader_id)
                else:
                    # role="seat": AudioEvent とマッチングするためバッファに保持
                    self._rfid_seat_buffer.append(ev)
            except queue.Empty:
                break

    def _expire_buffers(self) -> None:
        cutoff = time.time() - CAMERA_BUFFER_TTL
        self._camera_buffer = [e for e in self._camera_buffer if e.timestamp >= cutoff]
        self._rfid_seat_buffer = [e for e in self._rfid_seat_buffer if e.timestamp >= cutoff]

    def _pop_matching_camera_event(self, seat: int, ts: float) -> Optional[CameraEvent]:
        candidates = [
            e for e in self._camera_buffer
            if e.seat == seat and abs(e.timestamp - ts) <= MATCH_WINDOW
        ]
        if not candidates:
            return None
        best = min(candidates, key=lambda e: abs(e.timestamp - ts))
        self._camera_buffer.remove(best)
        return best

    def _pop_matching_rfid_event(self, seat: int, ts: float) -> Optional[RFIDEvent]:
        """同席・±MATCH_WINDOW 秒以内の RFIDEvent (role="seat") を返し除去する。"""
        candidates = [
            e for e in self._rfid_seat_buffer
            if e.seat == seat and abs(e.timestamp - ts) <= MATCH_WINDOW
        ]
        if not candidates:
            return None
        best = min(candidates, key=lambda e: abs(e.timestamp - ts))
        self._rfid_seat_buffer.remove(best)
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

        # センサーマッチング
        cam_event  = self._pop_matching_camera_event(seat, event.timestamp)
        rfid_event = self._pop_matching_rfid_event(seat, event.timestamp)

        has_camera = cam_event is not None
        has_rfid   = rfid_event is not None

        source = {
            "camera": has_camera,
            "audio":  True,
            "rfid":   has_rfid,
        }
        confidence = calc_confidence(has_rfid=has_rfid, has_audio=True, has_camera=has_camera)

        if has_rfid:
            logger.debug(
                "RFID corroboration: seat=%d tag=%s card=%r Δ=%.3fs",
                seat, rfid_event.tag_id, rfid_event.card,
                abs(rfid_event.timestamp - event.timestamp),
            )
        if has_camera:
            logger.debug(
                "Camera corroboration: seat=%d Δ=%.3fs",
                seat, abs(cam_event.timestamp - event.timestamp),
            )

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
        self._board_cards = []
        self._board_source = ""
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
            board=list(self._board_cards),
            board_source=self._board_source,
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
