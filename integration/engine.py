"""integration/engine.py

audio / camera / RFID (ESP32 HTTP) の 3 ソースを統合し、confidence スコアを算出する。

ソース優先度: RFID > audio > camera

Confidence 行列:
  RFID + audio + camera : 1.00
  RFID + audio          : 0.95
  RFID + camera         : 0.85
  RFID のみ             : 0.70
  audio + camera        : 0.80
  audio のみ            : 0.50
  camera のみ           : 0.30
  なし                  : 0.00

カード情報 (ESP32 RFID):
  role="board" かつ board_index 付きイベント
      → _board_positions[board_index] に格納、ボード枚数でストリート自動推移
  role="seat" かつ card 付きイベント
      → _hole_cards[seat] に最大 2 枚蓄積、手終了時に HandSummary に反映

アクション照合 (role="seat"):
  ±MATCH_WINDOW 秒以内の同席 AudioEvent と照合して confidence 向上
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from datetime import datetime
from typing import TYPE_CHECKING, Callable, Optional

from audio.recognizer import apply_corrections
from core.event_queue import EventQueue
from core.events import AudioEvent, CameraEvent, RFIDEvent
from core.game_state import GameStateManager, Street
from core.hand_log import ActionRecord, HandSummary
from output.event_recorder import EventRecorder
from output.json_writer import JsonWriter

if TYPE_CHECKING:
    from core.engine_types import LegalContext

logger = logging.getLogger(__name__)

MATCH_WINDOW = 2.0
CAMERA_BUFFER_TTL = MATCH_WINDOW * 2

# ――― Confidence スコア定数 ―――
_CONF_RFID_AUDIO_CAMERA = 1.00
_CONF_RFID_AUDIO        = 0.95
_CONF_RFID_CAMERA       = 0.85
_CONF_RFID_ONLY         = 0.70
_CONF_AUDIO_CAMERA      = 0.80
_CONF_AUDIO_ONLY        = 0.50
_CONF_CAMERA_ONLY       = 0.30

# board_index → street 推移しきい値 (1-indexed, ≥N 枚でその street)
_BOARD_STREET_THRESHOLDS = {3: "flop", 4: "turn", 5: "river"}


def calc_confidence(has_rfid: bool, has_audio: bool, has_camera: bool) -> float:
    """センサー組み合わせから confidence スコアを返す。"""
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
    """audio / camera / RFID の 3 キューを消費してゲーム状態を更新する。"""

    def __init__(
        self,
        audio_queue: EventQueue,
        game_state: GameStateManager,
        json_writer: JsonWriter,
        camera_queue: Optional[EventQueue] = None,
        rfid_queue: Optional[EventQueue] = None,
        on_action: Optional[Callable[[ActionRecord], None]] = None,
        on_rfid_card: Optional[Callable[[RFIDEvent], None]] = None,
        stop_event: Optional[threading.Event] = None,
        event_recorder: Optional[EventRecorder] = None,
    ) -> None:
        """
        Args:
            on_rfid_card: カード検出時のコールバック (GUI スレッドには渡さず
                          _update_queue 経由で処理すること)。スレッド安全に設計すること。
            event_recorder: 生イベントを sidecar に記録する recorder (R1, ADR-0010)。
                            None なら記録しない (= 挙動不変)。解釈前に呼ばれる。
        """
        super().__init__(daemon=True, name="IntegrationThread")
        self._audio_queue = audio_queue
        self._camera_queue = camera_queue
        self._rfid_queue = rfid_queue
        self._game_state = game_state
        self._json_writer = json_writer
        self._on_action = on_action
        self._on_rfid_card = on_rfid_card
        self._stop_event = stop_event or threading.Event()
        self._event_recorder = event_recorder

        # センサーイベントのバッファ
        self._camera_buffer: list[CameraEvent] = []
        self._rfid_seat_buffer: list[RFIDEvent] = []

        # ハンド内の一時バッファ
        self._current_actions: list[ActionRecord] = []
        self._hand_started_at: str = _now_iso()
        self._stack_start: dict[int, int] = {}

        # RFID カード情報
        self._board_cards: list[str] = []          # 順序付きボードカード（表示用）
        self._board_positions: dict[int, str] = {} # board_index → card
        self._board_source: str = ""
        self._hole_cards: dict[int, list[str]] = {}  # seat → [card1, card2]

        # RFID カードがマスター未解決のままハンドが進んだ場合、ハンド全体を
        # 要レビューにする（個々の ActionRecord では捕捉できないため）。
        self._hand_needs_review: bool = False

    def stop(self) -> None:
        self._stop_event.set()

    def _record(self, event: AudioEvent | CameraEvent | RFIDEvent) -> None:
        """生イベントを sidecar に記録する (recorder 未設定なら no-op = 挙動不変)。解釈前に呼ぶ。"""
        if self._event_recorder is not None:
            self._event_recorder.record(event)

    def run(self) -> None:
        logger.info("IntegrationThread started")
        while not self._stop_event.is_set():
            self._drain_camera_queue()
            self._drain_rfid_queue()

            try:
                event = self._audio_queue.get(timeout=0.1)
            except queue.Empty:
                self._expire_buffers()
                continue

            self._record(event)

            try:
                self._handle_audio_event(event)
            except Exception:
                logger.exception("Error handling audio event: %s", event)

            self._expire_buffers()

        logger.info("IntegrationThread stopped")

    # ――― バッファ管理 ―――

    def _drain_camera_queue(self) -> None:
        if self._camera_queue is None:
            return
        while True:
            try:
                ev = self._camera_queue.get_nowait()
            except queue.Empty:
                break
            self._record(ev)
            self._camera_buffer.append(ev)

    def _drain_rfid_queue(self) -> None:
        if self._rfid_queue is None:
            return
        while True:
            try:
                ev: RFIDEvent = self._rfid_queue.get_nowait()
            except queue.Empty:
                break
            self._record(ev)
            self._process_rfid_event(ev)

    def _process_rfid_event(self, ev: RFIDEvent) -> None:
        """受信した RFIDEvent を役割に応じて振り分ける。"""
        if ev.role == "board":
            self._handle_board_rfid(ev)
        else:
            self._handle_seat_rfid(ev)

    def _handle_board_rfid(self, ev: RFIDEvent) -> None:
        """ボードカードの RFID イベントを処理する。"""
        if not ev.card:
            logger.warning(
                "Board RFID event has no card (tag=%s reader=%s) — needs_review",
                ev.tag_id, ev.reader_id,
            )
            self._hand_needs_review = True
            return

        if ev.board_index is not None:
            # 位置指定あり: board_positions に格納して順序保証
            self._board_positions[ev.board_index] = ev.card
            self._board_cards = [
                self._board_positions[i]
                for i in sorted(self._board_positions)
            ]
            logger.info(
                "Board card [pos=%d]: %s — board so far: %s",
                ev.board_index, ev.card, self._board_cards,
            )
            self._try_advance_street_from_rfid()
        else:
            # board_index なし: 末尾に追記
            self._board_cards.append(ev.card)
            logger.info(
                "Board card (no index): %s — board so far: %s",
                ev.card, self._board_cards,
            )

        if not self._board_source:
            self._board_source = "rfid"

        if self._on_rfid_card:
            self._on_rfid_card(ev)

    def _handle_seat_rfid(self, ev: RFIDEvent) -> None:
        """座席カードの RFID イベントを処理する (ホールカード蓄積 + アクション照合用)。"""
        # ホールカード蓄積 (カード情報がある場合のみ)
        if ev.card and ev.seat is not None:
            seat_cards = self._hole_cards.setdefault(ev.seat, [])
            if ev.card not in seat_cards and len(seat_cards) < 2:
                seat_cards.append(ev.card)
                logger.info(
                    "Hole card detected: seat=%d card=%s (cards so far: %s)",
                    ev.seat, ev.card, seat_cards,
                )
                if self._on_rfid_card:
                    self._on_rfid_card(ev)
        elif not ev.card:
            logger.warning(
                "Seat RFID event has no card (tag=%s reader=%s seat=%s) — needs_review",
                ev.tag_id, ev.reader_id, ev.seat,
            )
            self._hand_needs_review = True

        # アクション照合バッファに追加
        self._rfid_seat_buffer.append(ev)

    def _try_advance_street_from_rfid(self) -> None:
        """ボードカード枚数に応じてストリートを自動推移する (RFID 優先証拠)。"""
        n = len(self._board_positions)
        gs = self._game_state
        target_street = _BOARD_STREET_THRESHOLDS.get(n)
        if target_street is None:
            return
        street_enum = {
            "flop":  Street.FLOP,
            "turn":  Street.TURN,
            "river": Street.RIVER,
        }.get(target_street)
        if street_enum is None:
            return
        if gs.street == street_enum.value:
            return  # 既にそのストリート
        try:
            gs.advance_street(street_enum)
            logger.info(
                "Street auto-advanced to %s by RFID board cards (%d cards detected)",
                target_street, n,
            )
        except ValueError:
            # 後退遷移など無効な場合は無視
            logger.debug(
                "RFID street advance to %s skipped (current=%s)",
                target_street, gs.street,
            )

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
        """同席・±MATCH_WINDOW 秒以内の RFID seat イベントを返し除去する。"""
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

        # ベッティングアクション。rules-aware backend（pokerkit）は境界で actor 推定 + 合法手
        # 射影、legacy（空 legal_context）は従来経路で挙動不変（ADR-0009 §1）。
        legal_ctx = gs.legal_context()
        if legal_ctx.legal_actions:
            self._handle_rules_aware_action(event, legal_ctx)
        else:
            self._handle_legacy_action(event)

    def _handle_legacy_action(self, event: AudioEvent) -> None:
        """rules-aware でない backend（legacy）の従来アクション処理（挙動不変）。"""
        action = event.action
        gs = self._game_state
        seat = gs.get_current_player()

        try:
            gs.apply_action(seat, action, event.amount)
        except ValueError:
            logger.exception("apply_action failed (seat=%d, action=%s)", seat, action)
            needs_review = True
        else:
            needs_review = False

        cam_event  = self._pop_matching_camera_event(seat, event.timestamp)
        rfid_event = self._pop_matching_rfid_event(seat, event.timestamp)

        has_camera = cam_event is not None
        has_rfid   = rfid_event is not None

        source = {"camera": has_camera, "audio": True, "rfid": has_rfid}
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

    def _resolve_actor(self, event: AudioEvent, legal_ctx: LegalContext) -> tuple[int, bool]:
        """D2a: actor を engine の合法手番(prior)に固定し、明示発話席 / 窓内 RFID 席が prior と
        食い違えば競合（out-of-turn / 未宣言 fold の兆候）として needs_review を立てる。

        prior を sensor で上書きする silent-fold 合成（fold_through）は後続増分（誤 fold リスクが
        高く Phase F の golden fixtures で検証するため）。
        """
        prior = legal_ctx.actor_seat
        sensed: set[int] = set()
        if event.seat is not None:
            sensed.add(event.seat)
        for e in self._rfid_seat_buffer:
            if e.seat is not None and abs(e.timestamp - event.timestamp) <= MATCH_WINDOW:
                sensed.add(e.seat)
        conflict = any(s != prior for s in sensed)
        return prior, conflict

    def _handle_rules_aware_action(self, event: AudioEvent, legal_ctx: LegalContext) -> None:
        """rules-aware backend（pokerkit）でのアクション処理（ADR-0009 §5: 合法手への射影）。

        D2a: apply_corrections をライブ適用し actor 競合を検出（prior に固定）。silent-fold 合成と
        派生 confidence（D3）は後続。
        """
        gs = self._game_state
        actor, actor_conflict = self._resolve_actor(event, legal_ctx)
        corrected = apply_corrections(event.action, event.amount, legal_ctx, event.confidence)

        try:
            gs.apply_action(actor, corrected.action, corrected.amount)
            apply_ok = True
        except ValueError:
            logger.exception(
                "rules-aware apply_action failed (seat=%s action=%s amount=%s)",
                actor, corrected.action, corrected.amount,
            )
            apply_ok = False

        cam_event  = self._pop_matching_camera_event(actor, event.timestamp)
        rfid_event = self._pop_matching_rfid_event(actor, event.timestamp)
        has_camera = cam_event is not None
        has_rfid   = rfid_event is not None

        source = {"camera": has_camera, "audio": True, "rfid": has_rfid}
        # D2a: confidence は既存 calc_confidence を流用（3 因子融合は D3）。
        confidence = calc_confidence(has_rfid=has_rfid, has_audio=True, has_camera=has_camera)
        needs_review = (not apply_ok) or corrected.needs_review or actor_conflict

        record = ActionRecord(
            hand_id=gs.hand_id,
            timestamp=_now_iso(),
            street=gs.street,
            seat=actor,
            player_name=gs.get_player_name(actor),
            action=corrected.action,
            amount=corrected.amount,
            pot_after=gs.pot,
            stack_after=gs.get_stack(actor),
            source=source,
            needs_review=needs_review,
            confidence=confidence,
        )
        self._current_actions.append(record)

        if self._on_action:
            self._on_action(record)

        logger.debug(
            "ActionRecord (rules-aware): seat=%d action=%s amount=%d corrected_from=%s reason=%s review=%s",
            actor, corrected.action, corrected.amount,
            corrected.corrected_from, corrected.reason, needs_review,
        )

    # ――― ハンド開始 / 終了 ―――

    def _start_new_hand(self) -> None:
        gs = self._game_state
        gs.new_hand()
        self._current_actions = []
        self._hand_started_at = _now_iso()
        self._stack_start = gs.get_stacks()
        self._board_cards = []
        self._board_positions = {}
        self._board_source = ""
        self._hole_cards = {}
        self._hand_needs_review = False
        logger.info("New hand started: hand_id=%d", gs.hand_id)

    def _finalize_hand(self, winner_seat: int) -> None:
        gs = self._game_state
        gs.end_hand(winner_seat)

        stacks_end = gs.get_stacks()
        players_info = []
        for seat in sorted(stacks_end.keys()):
            hole = self._hole_cards.get(seat, [])
            players_info.append({
                "seat":              seat,
                "name":              gs.get_player_name(seat),
                "hole_cards":        list(hole) if hole else None,
                "hole_cards_source": "rfid" if hole else "",
                "stack_start":       self._stack_start.get(seat, 0),
                "stack_end":         stacks_end[seat],
                "result":            stacks_end[seat] - self._stack_start.get(seat, 0),
            })

        if self._hole_cards:
            logger.info(
                "Hand %d: hole_cards from RFID — %s",
                gs.hand_id,
                {s: cards for s, cards in self._hole_cards.items()},
            )

        summary = HandSummary(
            hand_id=gs.hand_id,
            session_id=self._json_writer._session_id,
            started_at=self._hand_started_at,
            ended_at=_now_iso(),
            blinds={"sb": gs._sb, "bb": gs._bb},  # noqa: SLF001
            board=list(self._board_cards),
            board_source=self._board_source,
            players=players_info,
            pot_total=sum(
                a.amount for a in self._current_actions
                if a.action in ("bet", "raise", "call", "allin")
            ),
            winner_seat=winner_seat,
            actions=list(self._current_actions),
            review_required=(
                self._hand_needs_review
                or any(a.needs_review for a in self._current_actions)
            ),
        )

        self._json_writer.append_hand_summary(summary)
        logger.info("Hand %d finalized. Winner: seat %d", gs.hand_id, winner_seat)
        self._current_actions = []
        # _current_actions と対称にリセットし、stale フラグが次のサマリーへ
        # 漏れない（new_hand を挟まない再 finalize でも残らない）ようにする。
        self._hand_needs_review = False


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
