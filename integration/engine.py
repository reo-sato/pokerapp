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
from typing import Callable, Optional

from core.event_queue import EventQueue
from core.events import AudioEvent, CameraEvent, RFIDEvent
from core.game_state import GameStateManager, Street
from core.hand_log import ActionRecord, HandSummary
from integration.action_inference import BettingState, infer_action
from integration.action_order import advance_button
from output.evidence_log import EvidenceLogWriter
from output.json_writer import JsonWriter

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
        initial_button_seat: Optional[int] = None,
        sb_amount: Optional[int] = None,
        bb_amount: Optional[int] = None,
        auto_post_blinds: bool = True,
    ) -> None:
        """
        Args:
            on_rfid_card: カード検出時のコールバック (GUI スレッドには渡さず
                          _update_queue 経由で処理すること)。スレッド安全に設計すること。
            initial_button_seat: 第 1 ハンドの button 席。None なら BettingState は
                                 未初期化のまま (state-aware 推定はフォールバック)。
            sb_amount/bb_amount: 自動 blind post 用の金額。None なら game_state の
                                 設定値を読み取る。
            auto_post_blinds: 新ハンド開始時に SB/BB を自動 post するか。
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

        # ベッティング状態（アクション推定・妥当性検証に使用）
        self._betting_state = BettingState()

        # ベイズ層 (v6.0+ M1) 用: raw 観測イベントを JSONL に常時書き出すロガー。
        # session JSON と完全分離。推定ロジックには影響しない。
        self._evidence_log = EvidenceLogWriter(
            log_dir=self._json_writer._log_dir,           # noqa: SLF001
            session_id=self._json_writer._session_id,     # noqa: SLF001
        )

        # ボタン管理
        self._auto_post_blinds = auto_post_blinds
        self._sb_amount = sb_amount if sb_amount is not None else getattr(game_state, "_sb", 0)
        self._bb_amount = bb_amount if bb_amount is not None else getattr(game_state, "_bb", 0)
        self._next_button_seat: Optional[int] = initial_button_seat
        self._last_button_seat: Optional[int] = None

    def stop(self) -> None:
        self._stop_event.set()

    # ――― state-aware API (GUI / CLI から呼び出し) ―――

    def set_next_button_seat(self, seat: Optional[int]) -> None:
        """次のハンド開始時に使う button 席を指定する (1回限り、自動進行を上書き)。"""
        self._next_button_seat = seat
        logger.info("Next button seat set to %s", seat)

    @property
    def betting_state(self) -> BettingState:
        """現在のベッティング状態 (GUI ヘッダー表示などに使う)。"""
        return self._betting_state

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

            try:
                self._handle_audio_event(event)
            except Exception:
                logger.exception("Error handling audio event: %s", event)

            self._expire_buffers()

        # 終了処理: evidence log のファイルハンドルを閉じる
        try:
            self._evidence_log.close()
        except Exception:
            logger.exception("EvidenceLogWriter.close() raised")
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
            # M1: raw 観測ログ
            self._evidence_log.write_camera(ev)
            self._camera_buffer.append(ev)

    def _drain_rfid_queue(self) -> None:
        if self._rfid_queue is None:
            return
        while True:
            try:
                ev: RFIDEvent = self._rfid_queue.get_nowait()
                self._process_rfid_event(ev)
            except queue.Empty:
                break

    def _process_rfid_event(self, ev: RFIDEvent) -> None:
        """受信した RFIDEvent を役割に応じて振り分ける。"""
        # M1: raw 観測ログ。役割振り分けの前に書き込む。
        self._evidence_log.write_rfid(ev)
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
            self._betting_state.reset_for_new_street()
            self._betting_state.street = target_street
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
        # M1: raw 観測ログ。推定ロジックの前に常に書き込む (例外でも残す)。
        self._evidence_log.write_audio(event)

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

        # BettingState が初期化済みなら actor_seat を優先 (button/blind から確定済み)
        if self._betting_state.is_initialized and self._betting_state.actor_seat is not None:
            seat = self._betting_state.actor_seat
        else:
            seat = gs.get_current_player()

        inferred = infer_action(event, self._betting_state, seat)

        if inferred.action is None:
            needs_review = True
            logged_action = event.action or "amount_only"
            logged_amount = inferred.amount
        else:
            try:
                gs.apply_action(seat, inferred.action, inferred.amount)
            except ValueError:
                logger.exception(
                    "apply_action failed (seat=%d, action=%s, amount=%d)",
                    seat, inferred.action, inferred.amount,
                )
                needs_review = True
            else:
                needs_review = inferred.needs_review
                self._betting_state.update_after_action(seat, inferred.action, inferred.amount)
            logged_action = inferred.action
            logged_amount = inferred.amount

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
            action=logged_action,
            amount=logged_amount,
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
        self._board_positions = {}
        self._board_source = ""
        self._hole_cards = {}
        self._betting_state.reset_for_new_hand()
        logger.info("New hand started: hand_id=%d", gs.hand_id)

        # button を確定 (手動 override → 自動回転 → 未設定)
        button = self._resolve_button_seat()
        if button is None:
            logger.warning(
                "Hand %d: button seat is unknown — BettingState not initialized. "
                "state-aware inference will fall back to actor heuristic.",
                gs.hand_id,
            )
            return

        active = self._compute_active_seats()
        if len(active) < 2:
            logger.warning(
                "Hand %d: only %d active seat(s) — cannot start hand state machine",
                gs.hand_id, len(active),
            )
            return

        self._betting_state.start_hand(
            button_seat=button,
            active_seats=active,
            sb_amount=self._sb_amount,
            bb_amount=self._bb_amount,
        )
        self._last_button_seat = button

        if self._auto_post_blinds and self._betting_state.is_initialized:
            self._post_blinds_to_records()

    def _resolve_button_seat(self) -> Optional[int]:
        """次ハンドの button 席を決定する。

        優先順位:
          1. set_next_button_seat() で明示された seat (一度きりの手動 override)
          2. 前ハンドの button から左隣の active seat へ自動回転 (advance_button)
          3. 一度も button が設定されていなければ None
        """
        active = self._compute_active_seats()

        if self._next_button_seat is not None:
            seat = self._next_button_seat
            self._next_button_seat = None
            if self._last_button_seat is None:
                logger.info("Button seat initialized to %s (manual)", seat)
            else:
                logger.info(
                    "Button seat overridden: %s -> %s (manual correction)",
                    self._last_button_seat, seat,
                )
            return seat

        if self._last_button_seat is not None:
            next_seat = advance_button(self._last_button_seat, active)
            if next_seat is not None:
                logger.info(
                    "Button advance: previous=%d next=%d active=%s",
                    self._last_button_seat, next_seat, active,
                )
            else:
                logger.warning(
                    "Button could not be advanced from %d (active=%s)",
                    self._last_button_seat, active,
                )
            return next_seat

        return None

    def _compute_active_seats(self) -> list[int]:
        """ハンド開始時の active seat (= 着席かつ stack > 0)。"""
        stacks = self._game_state.get_stacks()
        return sorted(s for s, st in stacks.items() if st > 0)

    def _post_blinds_to_records(self) -> None:
        """BettingState が post した SB/BB を ActionRecord と GameStateManager に反映する。"""
        gs = self._game_state
        bs = self._betting_state
        for entry in bs.action_history:
            seat = entry["seat"]
            amount = entry["amount"]
            action_label = entry["action"]
            if action_label not in ("SB_POST", "BB_POST"):
                continue
            try:
                gs.apply_action(seat, "bet", amount)
            except ValueError:
                logger.exception("apply_action failed during blind post (seat=%d)", seat)
            record = ActionRecord(
                hand_id=gs.hand_id,
                timestamp=_now_iso(),
                street="preflop",
                seat=seat,
                player_name=gs.get_player_name(seat),
                action=action_label,
                amount=amount,
                pot_after=gs.pot,
                stack_after=gs.get_stack(seat),
                source={"camera": False, "audio": False, "rfid": False},
                needs_review=False,
                confidence=1.0,
            )
            self._current_actions.append(record)
            if self._on_action:
                try:
                    self._on_action(record)
                except Exception:
                    logger.exception("on_action callback raised")

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
            review_required=any(a.needs_review for a in self._current_actions),
            folded_seats=[a.seat for a in self._current_actions if a.action == "fold"],
            all_in_seats=[a.seat for a in self._current_actions if a.action == "allin"],
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
