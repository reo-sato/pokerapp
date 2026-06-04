"""integration/engine.py

audio / RFID (ESP32 HTTP) の 2 ソースを統合し、confidence スコアを算出する。
（カメラ入力は sprc_v4.docx で廃止済み。RFID + 音声の 2 ソース構成。）

ソース優先度: RFID > audio

Confidence 行列:
  RFID + audio : 0.95
  RFID のみ    : 0.70
  audio のみ   : 0.50
  なし         : 0.00

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
from core.events import AudioEvent, RFIDEvent
from core.game_state import GameStateManager, Street
from core.hand_log import ActionRecord, HandSummary
from core.session_repository import SessionError, SessionRepository
from output.json_writer import JsonWriter

logger = logging.getLogger(__name__)

MATCH_WINDOW = 2.0
BUFFER_TTL = MATCH_WINDOW * 2

# ――― Confidence スコア定数（RFID + 音声の 2 ソース）―――
_CONF_RFID_AUDIO = 0.95
_CONF_RFID_ONLY  = 0.70
_CONF_AUDIO_ONLY = 0.50

# board_index → street 推移しきい値 (1-indexed, ≥N 枚でその street)
_BOARD_STREET_THRESHOLDS = {3: "flop", 4: "turn", 5: "river"}


def calc_confidence(has_rfid: bool, has_audio: bool) -> float:
    """センサー組み合わせ（RFID + 音声）から confidence スコアを返す。"""
    if has_rfid and has_audio:
        return _CONF_RFID_AUDIO
    if has_rfid:
        return _CONF_RFID_ONLY
    if has_audio:
        return _CONF_AUDIO_ONLY
    return 0.0


class IntegrationThread(threading.Thread):
    """audio / RFID の 2 キューを消費してゲーム状態を更新する。"""

    def __init__(
        self,
        audio_queue: EventQueue,
        game_state: GameStateManager,
        json_writer: JsonWriter,
        rfid_queue: Optional[EventQueue] = None,
        on_action: Optional[Callable[[ActionRecord], None]] = None,
        on_rfid_card: Optional[Callable[[RFIDEvent], None]] = None,
        stop_event: Optional[threading.Event] = None,
        session_repo: Optional[SessionRepository] = None,
        session_id: Optional[str] = None,
        seating: Optional[dict[int, str]] = None,
    ) -> None:
        """
        Args:
            on_rfid_card: カード検出時のコールバック (GUI スレッドには渡さず
                          _update_queue 経由で処理すること)。スレッド安全に設計すること。
            session_repo: S2 session レイヤ (Phase 2.2, write-through 接続)。指定すると
                          hand 開始時に seat→player_id を ``assign_seat`` で記録し、
                          HandSummary に session レイヤの ``session_id`` / ``player_id`` を
                          additive に流す（ADR-0008 Pattern A）。None なら従来の
                          hand logger 単独動作（rollback path）。
            session_id:   session レイヤが採番した UUID4 hex（``session_repo`` と対で渡す）。
            seating:      ``seat_no -> player_id`` の初期 seating。実行中は GUI から
                          ``update_seating`` で差し替えられる（Phase 2.3 seat selection UX）。
        """
        super().__init__(daemon=True, name="IntegrationThread")
        self._audio_queue = audio_queue
        self._rfid_queue = rfid_queue
        self._game_state = game_state
        self._json_writer = json_writer
        self._on_action = on_action
        self._on_rfid_card = on_rfid_card
        self._stop_event = stop_event or threading.Event()

        # ――― S2 session レイヤ接続 (Phase 2.2, ADR-0008) ―――
        self._session_repo = session_repo
        self._session_id = session_id
        # seating は GUI スレッド (update_seating) と IntegrationThread
        # (_assign_seats_for_hand) の双方から触られるため lock で保護する (Phase 2.3)。
        self._seating_lock = threading.Lock()
        self._seating: dict[int, str] = dict(seating) if seating else {}
        # session_repo と session_id が揃って初めて write-through が有効
        self._session_layer_enabled = (
            session_repo is not None and session_id is not None
        )

        # センサーイベントのバッファ
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

    def stop(self) -> None:
        self._stop_event.set()

    # ――― seating 更新 API (Phase 2.3, GUI から呼ばれる; スレッド安全) ―――

    def update_seating(self, seating: dict[int, str]) -> None:
        """次以降の hand に適用する seat→player_id マッピングを差し替える。

        GUI スレッド（seat selection UI）から呼ばれる。実際の write-through は
        次の ``_start_new_hand`` → ``_assign_seats_for_hand`` で行われる。session レイヤ
        無効時に呼ばれても害はない（write は no-op になる）。
        """
        with self._seating_lock:
            self._seating = dict(seating)
        logger.info("Seating updated: %d seat(s) assigned", len(seating))

    def get_seating(self) -> dict[int, str]:
        """現在の seat→player_id マッピングのコピーを返す（carry-forward 初期値用）。"""
        with self._seating_lock:
            return dict(self._seating)

    def run(self) -> None:
        logger.info("IntegrationThread started")
        while not self._stop_event.is_set():
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

        logger.info("IntegrationThread stopped")

    # ――― バッファ管理 ―――

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
        cutoff = time.time() - BUFFER_TTL
        self._rfid_seat_buffer = [e for e in self._rfid_seat_buffer if e.timestamp >= cutoff]

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

        seat = gs.get_current_player()

        try:
            gs.apply_action(seat, action, event.amount)
        except ValueError:
            logger.exception("apply_action failed (seat=%d, action=%s)", seat, action)
            needs_review = True
        else:
            needs_review = False

        rfid_event = self._pop_matching_rfid_event(seat, event.timestamp)

        has_rfid = rfid_event is not None

        source = {"audio": True, "rfid": has_rfid}
        confidence = calc_confidence(has_rfid=has_rfid, has_audio=True)

        if has_rfid:
            logger.debug(
                "RFID corroboration: seat=%d tag=%s card=%r Δ=%.3fs",
                seat, rfid_event.tag_id, rfid_event.card,
                abs(rfid_event.timestamp - event.timestamp),
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
        self._board_positions = {}
        self._board_source = ""
        self._hole_cards = {}
        self._assign_seats_for_hand(gs.hand_id)
        logger.info("New hand started: hand_id=%d", gs.hand_id)

    def _assign_seats_for_hand(self, hand_id: int) -> None:
        """hand 開始時に現在の seating を session レイヤへ write-through する (ADR-0008)。

        session レイヤ無効時 / seating 未設定時は no-op（従来の hand logger 単独動作）。
        ``assign_seat`` 失敗（unknown_player / session_closed / player_already_seated 等）は
        hand logger の進行を止めず、warning に留める（degraded; UX を壊さない）。
        """
        if not self._session_layer_enabled:
            return
        seating = self.get_seating()
        if not seating:
            return
        for seat_no, player_id in sorted(seating.items()):
            try:
                self._session_repo.assign_seat(
                    self._session_id, hand_id, seat_no, player_id
                )
            except SessionError:
                logger.exception(
                    "assign_seat failed (session=%s hand=%d seat=%d player=%s)",
                    self._session_id, hand_id, seat_no, player_id,
                )

    def _finalize_hand(self, winner_seat: int) -> None:
        gs = self._game_state
        gs.end_hand(winner_seat)

        stacks_end = gs.get_stacks()
        # 同一 hand 内で seating の一貫した snapshot を使う（GUI が途中で
        # update_seating しても finalize は開始時の write-through と同じ map を見る）。
        seating = self.get_seating() if self._session_layer_enabled else {}
        players_info = []
        for seat in sorted(stacks_end.keys()):
            hole = self._hole_cards.get(seat, [])
            entry = {
                "seat":              seat,
                "name":              gs.get_player_name(seat),
                "hole_cards":        list(hole) if hole else None,
                "hole_cards_source": "rfid" if hole else "",
                "stack_start":       self._stack_start.get(seat, 0),
                "stack_end":         stacks_end[seat],
                "result":            stacks_end[seat] - self._stack_start.get(seat, 0),
            }
            # Phase 2.2: session レイヤ有効時のみ player_id を additive 追加 (ADR-0008)。
            # legacy / fallback ではキーごと省略し、旧 reader を壊さない。
            if self._session_layer_enabled:
                player_id = seating.get(seat)
                if player_id is not None:
                    entry["player_id"] = player_id
            players_info.append(entry)

        if self._hole_cards:
            logger.info(
                "Hand %d: hole_cards from RFID — %s",
                gs.hand_id,
                {s: cards for s, cards in self._hole_cards.items()},
            )

        # session レイヤ有効時は canonical な UUID4 hex を使う（ADR-0007/0008）。
        # 無効時は従来どおり JsonWriter の (timestamp) session_id にフォールバック。
        session_id_for_summary = (
            self._session_id
            if self._session_layer_enabled
            else self._json_writer._session_id  # noqa: SLF001
        )

        summary = HandSummary(
            hand_id=gs.hand_id,
            session_id=session_id_for_summary,
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
