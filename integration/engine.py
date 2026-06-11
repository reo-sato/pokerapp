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
    from core.session_repository import SessionRepository

logger = logging.getLogger(__name__)

MATCH_WINDOW = 2.0
CAMERA_BUFFER_TTL = MATCH_WINDOW * 2

# silent-fold 合成で許す最大席数（ISSUE-0009）。超過は合成せず prior 維持 + needs_review。
SILENT_FOLD_CAP = 2
# 合成した silent-fold の confidence（sensor 観測なしの推定。較正は D3/F2）。常に needs_review。
SYNTH_FOLD_CONFIDENCE = 0.3

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


# ――― 派生 confidence (3 因子, ADR-0009 §6, D3) ―――
# rules-aware 経路専用。legacy は上の固定 8 行 calc_confidence のまま（挙動不変）。
# 重みは「全ソース一致・合法」で旧テーブルに近づける暫定値。最終較正は golden fixtures / F。
_CONF_W_A = 0.15            # 合意度 A の重み
_CONF_W_Q = 0.85           # ソース品質 Q の重み（w_A + w_Q = 1）
_CONF_L_PENALTY = 0.25     # pokerkit が action を受理しなかったときの合法性ゲート L
_CONF_BASE = {"rfid": 0.78, "audio": 0.50, "camera": 0.28}  # ソース base 信頼度（RFID>audio>camera）
# confidence がこの閾値未満なら needs_review（ADR-0009 §6 条件⑤）。将来 config 化。
# 音声優先運用（v1 は audio のみが必須経路）のため、良好な audio-only は閾値超え＝自動 review しない。
# camera-only / 低 whisper / 合成 fold は閾値未満＝review。最終較正は golden fixtures / F。
REVIEW_THRESHOLD = 0.40


def derive_confidence(
    *,
    apply_ok: bool,
    whisper_conf: float,
    audio_agree: bool,
    rfid_present: bool,
    rfid_agree: bool,
    camera_present: bool,
    camera_agree: bool,
) -> float:
    """3 因子（L 合法性 / A 合意度 / Q ソース品質）から confidence を導出する（ADR-0009 §6, D3）。

    - L = 1.0（pokerkit 受理）/ `_CONF_L_PENALTY`（非受理）。最重要の合法性ゲート。
    - A = 一致した存在ソース数 / 存在ソース数（audio は当該アクションにつき常に存在）。
    - Q = 一致した存在ソースの base 信頼度の noisy-OR（audio は whisper_conf でスケール）。
    confidence = clamp(L · (w_A·A + w_Q·Q), 0, 1)。
    """
    present = {"rfid": rfid_present, "audio": True, "camera": camera_present}
    agree = {"rfid": rfid_agree, "audio": audio_agree, "camera": camera_agree}

    L = 1.0 if apply_ok else _CONF_L_PENALTY
    n_present = sum(present.values())
    n_agree = sum(1 for s in present if present[s] and agree[s])
    A = (n_agree / n_present) if n_present else 0.0

    prod = 1.0
    for s in present:
        if present[s] and agree[s]:
            q = _CONF_BASE[s] * (whisper_conf if s == "audio" else 1.0)
            prod *= (1.0 - q)
    Q = 1.0 - prod

    return max(0.0, min(1.0, L * (_CONF_W_A * A + _CONF_W_Q * Q)))


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
        on_hand: Optional[Callable[["HandSummary"], None]] = None,
        clock: Optional[Callable[[], float]] = None,
        session_repo: "Optional[SessionRepository]" = None,
        seat_player_map: Optional[dict[int, str]] = None,
    ) -> None:
        """
        Args:
            on_rfid_card: カード検出時のコールバック (GUI スレッドには渡さず
                          _update_queue 経由で処理すること)。スレッド安全に設計すること。
            event_recorder: 生イベントを sidecar に記録する recorder (R1, ADR-0010)。
                            None なら記録しない (= 挙動不変)。解釈前に呼ばれる。
            on_hand: ハンド確定時に HandSummary を渡すコールバック (replay/テスト用、additive)。
                     on_rfid_card 同様 integration スレッドで発火するためスレッド安全に扱うこと。
            clock: epoch 秒を返す時計 (既定 time.time)。決定的 replay 用に注入する (F1)。
                   ActionRecord/HandSummary の timestamp と buffer 期限はこの時計に従う。
            session_repo: S2.x session レイヤ (ADR-0008 Pattern A)。`seat_player_map` と共に与えると
                          hand 開始時に assign_seat（write-through）し、HandSummary.players に player_id を
                          additive 埋め込む。None なら従来動作（session 未接続・挙動不変, rollback path）。
            seat_player_map: seat_no → player_id（registry の UUID hex）。session_repo と対で有効。
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
        self._on_hand = on_hand
        self._clock: Callable[[], float] = clock or time.time
        # S2.x session 統合（ADR-0008 Pattern A, write-through）。両方揃ったときのみ有効。
        self._session_repo = session_repo
        self._seat_player_map: dict[int, str] = dict(seat_player_map or {})
        # E3 (ISSUE-0006): seat_assign イベントで届いた変更は次ハンド開始時に適用する
        # （mid-hand の帰属/名前の揺れを防ぐ）。seat_no → player_id（"" = 割当解除）。
        self._pending_seat_changes: dict[int, str] = {}

        # センサーイベントのバッファ
        self._camera_buffer: list[CameraEvent] = []
        self._rfid_seat_buffer: list[RFIDEvent] = []

        # ハンド内の一時バッファ
        self._current_actions: list[ActionRecord] = []
        self._hand_started_at: str = self._now_iso()
        self._stack_start: dict[int, int] = {}

        # RFID カード情報
        self._board_cards: list[str] = []          # 順序付きボードカード（表示用）
        self._board_positions: dict[int, str] = {} # board_index → card
        self._board_source: str = ""
        self._hole_cards: dict[int, list[str]] = {}  # seat → [card1, card2]

        # RFID カードがマスター未解決のままハンドが進んだ場合、ハンド全体を
        # 要レビューにする（個々の ActionRecord では捕捉できないため）。
        self._hand_needs_review: bool = False

    @property
    def _session_layer_active(self) -> bool:
        # property 化 (E3): seat_assign で map が後から埋まる/空になるケースに追従する。
        return self._session_repo is not None and bool(self._seat_player_map)

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

    def _now_iso(self) -> str:
        """注入された時計 (既定 time.time) を ISO 文字列に。決定的 replay の clock 源 (F1)。"""
        return datetime.fromtimestamp(self._clock()).isoformat(timespec="milliseconds")

    def _expire_buffers(self) -> None:
        cutoff = self._clock() - CAMERA_BUFFER_TTL
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

        if action == "rebuy":
            self._handle_rebuy(event)
            return

        if action == "seat_assign":
            self._handle_seat_assign(event)
            return

        # ベッティングアクション。rules-aware backend（pokerkit）は境界で actor 推定 + 合法手
        # 射影、legacy（空 legal_context）は従来経路で挙動不変（ADR-0009 §1）。
        legal_ctx = gs.legal_context()
        if legal_ctx.legal_actions:
            self._handle_rules_aware_action(event, legal_ctx)
        else:
            self._handle_legacy_action(event)

    def _handle_rebuy(self, event: AudioEvent) -> None:
        """GUI/CLI から queue 経由で届いた rebuy を integration スレッドで適用する。

        GameStateManager / PokerkitGameState はロックを持たないため、状態変更は本スレッドに
        一元化する（規約「スレッド間通信は queue のみ」）。リバイはポーカーアクションではない
        ため HandSummary.actions には積まず、on_action への通知レコードのみ発行する
        （GUI/CLI はこれを受けてスタック表示を更新する）。
        """
        gs = self._game_state
        seat = event.seat if event.seat is not None else _extract_seat_from_text(event.raw_text)
        if seat is None:
            logger.warning("rebuy event without seat: %r", event.raw_text)
            return
        try:
            gs.rebuy(seat, event.amount)
        except ValueError:
            logger.exception("rebuy failed (seat=%s amount=%s)", seat, event.amount)
            return
        if self._on_action:
            self._on_action(ActionRecord(
                hand_id=gs.hand_id,
                timestamp=self._now_iso(),
                street=gs.street,
                seat=seat,
                player_name=gs.get_player_name(seat),
                action="rebuy",
                amount=event.amount,
                pot_after=gs.pot,
                stack_after=gs.get_stack(seat),
                source={"camera": False, "audio": False, "rfid": False},
                needs_review=False,
                confidence=1.0,
            ))

    def _handle_seat_assign(self, event: AudioEvent) -> None:
        """GUI から queue 経由で届いた seat→player 変更を受け付ける (E3, ISSUE-0006)。

        `event.seat` = 対象席、`event.raw_text` = player_id（空文字 = 割当解除）。
        mid-hand の帰属/名前の揺れを防ぐため即時適用せず、次ハンド開始時に
        `_apply_pending_seat_changes` でまとめて反映する（rebuy と同じ queue 一元化規約）。
        """
        if self._session_repo is None:
            logger.warning("seat_assign received but session layer is disabled; ignored")
            return
        if event.seat is None:
            logger.warning("seat_assign event without seat: %r", event.raw_text)
            return
        self._pending_seat_changes[event.seat] = (event.raw_text or "").strip()
        logger.info(
            "seat_assign queued (seat=%d player=%s); 次ハンドから反映",
            event.seat, self._pending_seat_changes[event.seat] or "<unassign>",
        )

    def _apply_pending_seat_changes(self) -> None:
        """保留中の seat→player 変更を seat_player_map と表示名に反映する（hand 開始時）。"""
        for seat_no, player_id in self._pending_seat_changes.items():
            if not player_id:
                self._seat_player_map.pop(seat_no, None)
                continue
            self._seat_player_map[seat_no] = player_id
            try:
                player = self._session_repo.get_player(player_id)
                self._game_state.set_player_name(seat_no, player.display_name)
            except Exception:
                # 名前解決失敗でも player_id の帰属は維持する（記録継続性を優先）
                logger.exception("seat_assign: 表示名の解決に失敗 (player_id=%s)", player_id)
        self._pending_seat_changes.clear()

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
            timestamp=self._now_iso(),
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

    def _pop_nearest_rfid_seat(self, ts: float) -> Optional[RFIDEvent]:
        """窓内で最も近い RFID seat イベントを席に関わらず 1 件取り出す（D2b actor 解決用）。

        prior と異なる席を指しうるため `_pop_matching_rfid_event`（同席限定）とは別。取り出して
        消費することで、actor 推定に使った読みが後続アクションへ滞留・連続誤検出しない。
        """
        candidates = [
            e for e in self._rfid_seat_buffer
            if e.seat is not None and abs(e.timestamp - ts) <= MATCH_WINDOW
        ]
        if not candidates:
            return None
        best = min(candidates, key=lambda e: abs(e.timestamp - ts))
        self._rfid_seat_buffer.remove(best)
        return best

    def _resolve_actor(
        self, event: AudioEvent, legal_ctx: LegalContext
    ) -> tuple[int, Optional[RFIDEvent], bool, list[int]]:
        """物理/明示証拠から actor を推定する（ADR-0009 §4, ISSUE-0009）。

        prior = engine の合法手番。優先順位 **RFID seat 読み > 明示発話席(event.seat)** で sensed を
        決め、sensed が prior と異なれば silent-fold 合成（`fold_through`, cap=SILENT_FOLD_CAP・atomic）で
        sensed まで手番を進める。合成成功なら actor=sensed、cap 超過/到達不可なら prior 維持（合成せず）。
        いずれの競合（sensed≠prior）も needs_review。actor 推定に使った RFID 読みは消費して返す
        （滞留防止 + corroboration 判定に再利用）。

        Returns: (actor, 消費した RFID seat 読み or None, conflict, 合成 fold した席列)
        """
        prior = legal_ctx.actor_seat
        rfid_ev = self._pop_nearest_rfid_seat(event.timestamp)
        sensed = rfid_ev.seat if rfid_ev is not None else event.seat

        if sensed is None or sensed == prior:
            return prior, rfid_ev, False, []

        # sensed != prior: 物理/明示証拠が別席 → silent-fold 合成を試みる（cap 内・atomic）。
        try:
            folded = self._game_state.fold_through(sensed, max_folds=SILENT_FOLD_CAP)
        except (ValueError, NotImplementedError):
            logger.warning(
                "silent-fold 合成不可: prior=%s sensed=%s (cap=%d 超過/到達不可) → prior 維持 + review",
                prior, sensed, SILENT_FOLD_CAP,
            )
            return prior, rfid_ev, True, []
        logger.info("silent-fold 合成: prior=%s → actor=%s (folded=%s)", prior, sensed, folded)
        return sensed, rfid_ev, True, folded

    def _append_synth_fold(self, seat: int) -> None:
        """合成した silent-fold を fold アクションとして記録する（推定なので常に needs_review）。"""
        gs = self._game_state
        record = ActionRecord(
            hand_id=gs.hand_id,
            timestamp=self._now_iso(),
            street=gs.street,
            seat=seat,
            player_name=gs.get_player_name(seat),
            action="fold",
            amount=0,
            pot_after=gs.pot,
            stack_after=gs.get_stack(seat),
            source={"camera": False, "audio": False, "rfid": False},
            needs_review=True,
            confidence=SYNTH_FOLD_CONFIDENCE,
        )
        self._current_actions.append(record)
        if self._on_action:
            self._on_action(record)
        logger.debug("ActionRecord (synth-fold): seat=%d (inferred silent fold)", seat)

    def _handle_rules_aware_action(self, event: AudioEvent, legal_ctx: LegalContext) -> None:
        """rules-aware backend（pokerkit）でのアクション処理（ADR-0009 §4/§5/§6）。

        D2b: 物理/明示証拠から actor を推定し（必要なら silent-fold 合成）、apply_corrections で
        合法手へ射影して適用。合成 fold は fold アクションとして記録。
        D3: 派生 confidence（3 因子）+ needs_review 5 条件。
        """
        gs = self._game_state
        actor, rfid_event, conflict, synthesized_seats = self._resolve_actor(event, legal_ctx)

        # 合成した silent-fold を先に記録（手番順: 中間席の fold → 当該 actor のアクション）。
        for fseat in synthesized_seats:
            self._append_synth_fold(fseat)

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

        cam_event = self._pop_matching_camera_event(actor, event.timestamp)
        # actor 推定に使った RFID 読みが最終 actor と一致すれば corroboration（消費済み）。
        has_rfid = rfid_event is not None and rfid_event.seat == actor
        has_camera = cam_event is not None

        source = {"camera": has_camera, "audio": True, "rfid": has_rfid}
        # D3: 3 因子の派生 confidence（ADR-0009 §6）。audio は当該アクションにつき常に存在。
        # audio が actor と一致するか（明示席がないか同席なら一致）。
        audio_agree = event.seat is None or event.seat == actor
        confidence = derive_confidence(
            apply_ok=apply_ok,
            whisper_conf=event.confidence if event.confidence is not None else 1.0,
            audio_agree=audio_agree,
            rfid_present=rfid_event is not None, rfid_agree=has_rfid,
            camera_present=has_camera, camera_agree=has_camera,
        )
        # D3: needs_review 5 条件（ADR-0009 §6）— ①非合法 ②高信頼 ASR×規則矛盾/④amount snap
        # （apply_corrections.needs_review が②④を内包）③actor 競合（prior↔sensor）⑤低 confidence。
        needs_review = (
            (not apply_ok)
            or corrected.needs_review
            or conflict
            or confidence < REVIEW_THRESHOLD
        )

        record = ActionRecord(
            hand_id=gs.hand_id,
            timestamp=self._now_iso(),
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
            "ActionRecord (rules-aware): seat=%d action=%s amount=%d synth=%s conflict=%s "
            "corrected_from=%s reason=%s review=%s",
            actor, corrected.action, corrected.amount, synthesized_seats, conflict,
            corrected.corrected_from, corrected.reason, needs_review,
        )

    # ――― ハンド開始 / 終了 ―――

    def _start_new_hand(self) -> None:
        gs = self._game_state
        if self._pending_seat_changes:
            self._apply_pending_seat_changes()
        gs.new_hand()
        self._current_actions = []
        self._hand_started_at = self._now_iso()
        self._stack_start = gs.get_stacks()
        self._board_cards = []
        self._board_positions = {}
        self._board_source = ""
        self._hole_cards = {}
        self._hand_needs_review = False
        if self._session_layer_active:
            self._assign_seats_for_hand(gs.hand_id)
        logger.info("New hand started: hand_id=%d", gs.hand_id)

    def _assign_seats_for_hand(self, hand_id: int) -> None:
        """S2.x: hand 開始時に seat→player を session レイヤへ write-through する（ADR-0008 §4）。

        個々の assign 失敗（seat/player 重複等）は当該ハンドを止めず log に留める（hand logger の
        記録継続性を優先）。session_id は JsonWriter の session_id（session レイヤ採番の UUID4 hex）。
        """
        session_id = self._json_writer._session_id  # noqa: SLF001
        for seat_no, player_id in self._seat_player_map.items():
            try:
                self._session_repo.assign_seat(session_id, hand_id, seat_no, player_id)
            except Exception:
                logger.exception(
                    "assign_seat failed (session=%s hand=%d seat=%d player=%s)",
                    session_id, hand_id, seat_no, player_id,
                )

    def _finalize_hand(self, winner_seat: int) -> None:
        gs = self._game_state
        gs.end_hand(winner_seat)

        # S2.x: 当該 hand の seat→player_id を session レイヤから解決（無効なら空 = 従来動作）。
        seat_player: dict[int, str] = {}
        if self._session_layer_active:
            try:
                seat_player = self._session_repo.resolve_seat_map_for_hand(
                    self._json_writer._session_id, gs.hand_id  # noqa: SLF001
                )
            except Exception:
                logger.exception("resolve_seat_map_for_hand failed; player_id を省略")

        stacks_end = gs.get_stacks()
        players_info = []
        for seat in sorted(stacks_end.keys()):
            hole = self._hole_cards.get(seat, [])
            info = {
                "seat":              seat,
                "name":              gs.get_player_name(seat),
                "hole_cards":        list(hole) if hole else None,
                "hole_cards_source": "rfid" if hole else "",
                "stack_start":       self._stack_start.get(seat, 0),
                "stack_end":         stacks_end[seat],
                "result":            stacks_end[seat] - self._stack_start.get(seat, 0),
            }
            if self._session_layer_active:
                # additive: session 接続時のみ player_id を載せる（未割当 seat は None）。
                info["player_id"] = seat_player.get(seat)
            players_info.append(info)

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
            ended_at=self._now_iso(),
            blinds={"sb": gs._sb, "bb": gs._bb},  # noqa: SLF001
            board=list(self._board_cards),
            board_source=self._board_source,
            players=players_info,
            pot_total=sum(
                a.amount for a in self._current_actions
                if a.action in ("bet", "raise", "call", "allin")
            ),
            pots=gs.pots(),
            winner_seat=winner_seat,
            actions=list(self._current_actions),
            review_required=(
                self._hand_needs_review
                or any(a.needs_review for a in self._current_actions)
            ),
        )

        self._json_writer.append_hand_summary(summary)
        if self._on_hand:
            self._on_hand(summary)
        logger.info("Hand %d finalized. Winner: seat %d", gs.hand_id, winner_seat)
        self._current_actions = []
        # _current_actions と対称にリセットし、stale フラグが次のサマリーへ
        # 漏れない（new_hand を挟まない再 finalize でも残らない）ようにする。
        self._hand_needs_review = False


# ――― ユーティリティ ―――

def _extract_seat_from_text(text: str) -> Optional[int]:
    """テキストから席番号を抽出する。例: "シート3 ウィナー" → 3。"""
    import re
    m = re.search(r"(?:シート|seat)\s*(\d+)", text, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return None
