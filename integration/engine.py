"""integration/engine.py

audio / RFID (ESP32 HTTP) の 2 ソースを統合し、アクター推定・矛盾検知・
confidence スコアを算出する。

ソース優先度: RFID > audio > camera（camera は spec v4.0 で廃止）

Confidence 行列:
  RFID + audio + camera : 1.00
  RFID + audio          : 0.95
  RFID + camera         : 0.85
  RFID のみ             : 0.70
  audio + camera        : 0.80
  audio のみ            : 0.50
  camera のみ           : 0.30
  なし                  : 0.00

アクター推定 (spec.md FR-26–29):
  legal_actions に基づき各席の事前確率を算出し、
  current_turn_seat / mentioned_seat / mentioned_position でブースト。

矛盾検知 (spec.md FR-22–25):
  legal_actions 違反 / アクター不確実 / フォールド矛盾 /
  ストリート超過 / ストリート不一致 を検査。
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional, Union

from core.event_queue import EventQueue
from core.events import AudioEvent, CameraEvent, RFIDEvent
from core.game_state import GameState, GameStateManager
from core.hand_log import ActionRecord, HandSummary

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

# アクター推定定数
_SCORE_CURRENT_TURN: float = 0.90   # current_turn_seat のスコア
_SCORE_BASE:        float = 0.10   # その他アクティブ席の基本スコア
_SCORE_BOOST:       float = 0.09   # mentioned_seat / position 一致時の加算
_AMBIGUITY_THRESHOLD: float = 0.30  # 最大 - 次点 < この値で「不確実」と判定

# board_index → street 推移しきい値 (1-indexed, ≥N 枚でその street)
_BOARD_STREET_THRESHOLDS = {3: "flop", 4: "turn", 5: "river"}


# ――― PhaseRecord ―――

@dataclass
class PhaseRecord:
    """ゲーム進行イベント（ハンド開始・ストリート遷移・ウィナー宣言）を表す。"""
    hand_id:    int
    timestamp:  str
    street:     str
    action:     str              # "new_hand" | "showdown" | "winner"
    winner_seat: Optional[int] = None


# ――― confidence 算出 ―――

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


# ――― アクター推定 (FR-26–29) ―――

def estimate_actor(audio_event: AudioEvent, state: GameState) -> dict[int, float]:
    """FR-26: 各席のアクター推定確率 {seat: prob} を返す。

    spec.md 5.4節の疑似コード:
      1. folded / all_in 席 → 0.0
      2. legal_actions にアクション種別が含まれない席 → 0.0
      3. 残りの席に _SCORE_BASE を付与
      4. current_turn_seat → _SCORE_CURRENT_TURN（基本スコアを上書き）
      5. mentioned_seat または position_map 一致席 → +_SCORE_BOOST（上限 1.0）
      6. 正規化して返す

    Returns:
        {seat: normalized_probability} — 非アクティブ席は 0.0 だが必ずキーに含まれる。
    """
    from core.rule_engine import ActionType, legal_actions as _legal

    # アクション種別の解析（winner / new_hand など非 ActionType は None）
    action_type: Optional[ActionType] = None
    try:
        action_type = ActionType(audio_event.action)
    except ValueError:
        pass

    scores: dict[int, float] = {}

    # 現在の手番席を取得
    current_seat: Optional[int] = None
    try:
        current_seat = state.current_turn_seat()
    except (RuntimeError, AttributeError):
        pass

    active_seats = [s for s in state.all_seats if s not in state.busted_seats]

    for seat in active_seats:
        # ステップ 1: folded / all_in → 0.0
        if seat in state.folded_seats or seat in state.all_in_seats:
            scores[seat] = 0.0
            continue

        # ステップ 2: legal_actions にアクション不在 → 0.0
        if action_type is not None:
            allowed = _legal(seat, state)
            if action_type not in allowed:
                scores[seat] = 0.0
                continue

        # ステップ 3: 基本スコア
        scores[seat] = _SCORE_BASE

    # ステップ 4: current_turn_seat を上書き
    if (current_seat is not None
            and current_seat in scores
            and scores[current_seat] > 0.0):
        scores[current_seat] = _SCORE_CURRENT_TURN

    # ステップ 5: mentioned_seat / mentioned_position ブースト（どちらか一方）
    ms = getattr(audio_event, "mentioned_seat", None)
    mp = getattr(audio_event, "mentioned_position", None)
    if ms is not None:
        if ms in scores and scores[ms] > 0.0:
            scores[ms] = min(1.0, scores[ms] + _SCORE_BOOST)
    elif mp is not None:
        for seat, pos in state.position_map.items():
            if pos == mp and seat in scores and scores[seat] > 0.0:
                scores[seat] = min(1.0, scores[seat] + _SCORE_BOOST)

    # ステップ 6: 正規化
    total = sum(scores.values())
    if total > 0.0:
        return {s: v / total for s, v in scores.items()}
    return scores


# ――― 矛盾検知 (FR-22–25) ―――

def detect_contradictions(
    audio_event: AudioEvent,
    state: GameState,
    actor_scores: Optional[dict[int, float]] = None,
    street_action_count: int = 0,
    rfid_board_count: int = -1,
) -> list[str]:
    """spec.md 9節の全矛盾条件を検査し、該当する説明文字列のリストを返す。

    検査項目:
        1. 音声アクションが best_seat の legal_actions に含まれない
        2. アクター推定: 最大確率 - 次点 < _AMBIGUITY_THRESHOLD
        3. 音声 FOLD なのに best_seat がすでに folded_seats に属する
        4. ストリート内音声アクション数 > アクティブ席数（all_in 除く）
        5. audio 推定ストリートと RFID ボード枚数が示すストリートが不一致

    Returns:
        矛盾の説明文字列リスト。空リストなら矛盾なし。
    """
    from core.rule_engine import (
        ActionType, validate_action,
        detect_street_overflow, detect_fold_contradiction,
    )

    contradictions: list[str] = []

    # best_seat を決定（actor_scores がなければ current_turn_seat で代替）
    best_seat: Optional[int] = None
    if actor_scores:
        non_zero = {s: v for s, v in actor_scores.items() if v > 0.0}
        if non_zero:
            best_seat = max(non_zero, key=non_zero.__getitem__)
    if best_seat is None:
        try:
            best_seat = state.current_turn_seat()
        except (RuntimeError, AttributeError):
            pass

    # アクション種別を解析
    action_type: Optional[ActionType] = None
    try:
        action_type = ActionType(audio_event.action)
    except ValueError:
        pass

    # ① legal_actions チェック
    if action_type is not None and best_seat is not None:
        ok, reason = validate_action(best_seat, action_type, state)
        if not ok:
            contradictions.append(f"legal_actions: {reason}")

    # ② アクター推定の不確実性
    if actor_scores:
        sorted_vals = sorted(
            (v for v in actor_scores.values() if v > 0.0), reverse=True
        )
        if len(sorted_vals) >= 2:
            diff = sorted_vals[0] - sorted_vals[1]
            if diff < _AMBIGUITY_THRESHOLD:
                contradictions.append(
                    f"actor_ambiguous: top={sorted_vals[0]:.3f} "
                    f"2nd={sorted_vals[1]:.3f} diff={diff:.3f}"
                )

    # ③ フォールド矛盾
    if action_type == ActionType.FOLD and best_seat is not None:
        if detect_fold_contradiction(best_seat, "fold", state):
            contradictions.append(
                f"fold_contradiction: seat {best_seat} is already in folded_seats"
            )

    # ④ ストリート内アクション数超過
    active_count = len(
        [s for s in state.get_active_seats() if s not in state.all_in_seats]
    )
    if detect_street_overflow(street_action_count, active_count):
        contradictions.append(
            f"street_overflow: action_count={street_action_count} "
            f"active_seats={active_count}"
        )

    # ⑤ ストリート不一致（RFID ボード枚数 vs audio 現在ストリート）
    if rfid_board_count >= 0:
        _rfid_street_map: dict[int, str] = {
            0: "preflop", 3: "flop", 4: "turn", 5: "river"
        }
        expected = _rfid_street_map.get(rfid_board_count)
        if expected and expected != state.street:
            contradictions.append(
                f"street_mismatch: audio_street={state.street} "
                f"rfid_board_count={rfid_board_count} expected={expected}"
            )

    return contradictions


# ――― process_event ―――

def process_event(
    event: dict,
    state: GameState,
    *,
    street_action_count: int = 0,
    rfid_board_count: int = -1,
) -> "ActionRecord | PhaseRecord | None":
    """統合イベントディクトを処理し、記録オブジェクトを返す（または None）。

    event["type"] の値:
        "action" : 音声アクションイベント
                   → estimate_actor → detect_contradictions → ActionRecord
        "phase"  : ゲーム進行イベント (new_hand / showdown / winner)
                   → state 更新 → PhaseRecord
        "rfid"   : RFID 折り畳みイベント・ボード更新
                   → state 更新のみ → None
        その他   : 無視 → None
    """
    ts         = event.get("timestamp") or _now_iso()
    event_type = event.get("type", "")

    # ── action ──────────────────────────────────────────────────────────────
    if event_type == "action":
        audio_ev = AudioEvent(
            action=event.get("action", ""),
            amount=event.get("amount"),
            timestamp=ts,
            raw_text=event.get("raw_text", ""),
            mentioned_seat=event.get("mentioned_seat"),
            mentioned_position=event.get("mentioned_position"),
        )

        actor_scores = estimate_actor(audio_ev, state)
        contradictions = detect_contradictions(
            audio_ev, state, actor_scores,
            street_action_count=street_action_count,
            rfid_board_count=rfid_board_count,
        )

        # アクター確定
        non_zero = {s: v for s, v in actor_scores.items() if v > 0.0}
        if non_zero:
            best_seat  = max(non_zero, key=non_zero.__getitem__)
            actor_conf = non_zero[best_seat]
        else:
            try:
                best_seat  = state.current_turn_seat()
                actor_conf = 0.0
            except (RuntimeError, AttributeError):
                logger.warning("process_event: no active seats")
                return None

        position = state.position_map.get(best_seat, "")
        amount   = audio_ev.amount if audio_ev.amount is not None else 0

        try:
            state.apply_action(best_seat, audio_ev.action, amount)
        except (ValueError, KeyError) as exc:
            logger.warning("process_event apply_action failed: %s", exc)
            contradictions.append(f"apply_failed: {exc}")

        return ActionRecord(
            hand_id=state.hand_id,
            timestamp=ts,
            street=state.street,
            seat=best_seat,
            player_name="",
            action=audio_ev.action,
            amount=amount,
            pot_after=state.pot,
            stack_after=state.get_stack(best_seat),
            source={"audio": True, "rfid": False},
            needs_review=bool(contradictions),
            confidence=_CONF_AUDIO_ONLY,
            position=position,
            actor_confidence=actor_conf,
        )

    # ── phase ────────────────────────────────────────────────────────────────
    elif event_type == "phase":
        from core.constants import Street as _Street

        action      = event.get("action", "")
        winner_seat = event.get("winner_seat")

        if action == "new_hand":
            state.new_hand()
        elif action == "showdown":
            try:
                state.advance_street(_Street.SHOWDOWN)
            except ValueError:
                pass
        elif action == "winner" and winner_seat is not None:
            try:
                state.end_hand(winner_seat)
            except (ValueError, KeyError):
                pass

        return PhaseRecord(
            hand_id=state.hand_id,
            timestamp=ts,
            street=state.street,
            action=action,
            winner_seat=winner_seat,
        )

    # ── rfid (generic wrapper) ───────────────────────────────────────────────
    elif event_type == "rfid":
        rfid_subtype = event.get("rfid_type", "")
        seat         = event.get("seat")
        card         = event.get("card_code", "")

        if rfid_subtype == "fold" and seat is not None:
            try:
                state.apply_action(seat, "fold")
            except (ValueError, KeyError):
                pass
        elif rfid_subtype == "board_card" and card:
            if card not in state.board_cards:
                state.board_cards.append(card)

        return None

    # ── bridge direct types (fold / board_card / street / hole_card) ─────────
    elif event_type == "fold":
        seat = event.get("seat")
        if seat is not None:
            try:
                state.apply_action(seat, "fold")
            except (ValueError, KeyError):
                pass

    elif event_type == "board_card":
        card = event.get("card_code", "")
        if card and card not in state.board_cards:
            state.board_cards.append(card)

    return None


# ――― IntegrationThread ―――

class IntegrationThread(threading.Thread):
    """audio / camera / RFID の 3 キューを消費してゲーム状態を更新する。

    game_state には GameStateManager (後方互換) または GameState を渡せる。
    GameState を渡した場合は estimate_actor / detect_contradictions による
    アクター推定と矛盾検知が有効になる（spec.md FR-26–29、FR-22–25）。
    """

    def __init__(
        self,
        audio_queue: EventQueue,
        game_state: Union[GameStateManager, GameState],
        json_writer,
        camera_queue: Optional[EventQueue] = None,
        rfid_queue: Optional[EventQueue] = None,
        on_action: Optional[Callable[[ActionRecord], None]] = None,
        on_rfid_card: Optional[Callable[[RFIDEvent], None]] = None,
        stop_event: Optional[threading.Event] = None,
    ) -> None:
        super().__init__(daemon=True, name="IntegrationThread")
        self._audio_queue  = audio_queue
        self._camera_queue = camera_queue
        self._rfid_queue   = rfid_queue
        self._game_state   = game_state
        self._json_writer  = json_writer
        self._on_action    = on_action
        self._on_rfid_card = on_rfid_card
        self._stop_event   = stop_event or threading.Event()

        # センサーイベントのバッファ
        self._camera_buffer:    list[CameraEvent] = []
        self._rfid_seat_buffer: list[RFIDEvent]   = []

        # ハンド内の一時バッファ
        self._current_actions:   list[ActionRecord] = []
        self._hand_started_at:   str  = _now_iso()
        self._stack_start:       dict[int, int] = {}
        self._street_action_count: int = 0  # FR-24 用ストリート内アクション数

        # RFID カード情報
        self._board_cards:     list[str]           = []
        self._board_positions: dict[int, str]      = {}
        self._board_source:    str                 = ""
        self._hole_cards:      dict[int, list[str]] = {}

    def stop(self) -> None:
        self._stop_event.set()

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
                self._process_rfid_event(ev)
            except queue.Empty:
                break

    def _process_rfid_event(self, ev: RFIDEvent) -> None:
        if ev.role == "board":
            self._handle_board_rfid(ev)
        else:
            self._handle_seat_rfid(ev)

    def _handle_board_rfid(self, ev: RFIDEvent) -> None:
        if not ev.card:
            logger.warning(
                "Board RFID event has no card (tag=%s reader=%s) — needs_review",
                ev.tag_id, ev.reader_id,
            )
            return

        if ev.board_index is not None:
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
        self._rfid_seat_buffer.append(ev)

    def _try_advance_street_from_rfid(self) -> None:
        from core.constants import Street
        n = len(self._board_positions)
        target_street = _BOARD_STREET_THRESHOLDS.get(n)
        if target_street is None:
            return
        street_enum = {"flop": Street.FLOP, "turn": Street.TURN, "river": Street.RIVER}.get(
            target_street
        )
        if street_enum is None:
            return
        gs = self._game_state
        if gs.street == street_enum.value:
            return
        try:
            gs.advance_street(street_enum)
            self._street_action_count = 0  # ストリート変化でリセット
            logger.info(
                "Street auto-advanced to %s by RFID (%d cards)", target_street, n
            )
        except ValueError:
            logger.debug("RFID street advance to %s skipped (current=%s)", target_street, gs.street)

    def _expire_buffers(self) -> None:
        cutoff = time.time() - CAMERA_BUFFER_TTL
        self._camera_buffer    = [e for e in self._camera_buffer    if e.timestamp >= cutoff]
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
        candidates = [
            e for e in self._rfid_seat_buffer
            if e.seat == seat and abs(e.timestamp - ts) <= MATCH_WINDOW
        ]
        if not candidates:
            return None
        best = min(candidates, key=lambda e: abs(e.timestamp - ts))
        self._rfid_seat_buffer.remove(best)
        return best

    # ――― ヘルパー: GameState / GameStateManager 差異を吸収 ―――

    def _get_current_player_seat(self) -> int:
        """現在手番の席番号を返す（両方のゲーム状態型に対応）。"""
        gs = self._game_state
        if isinstance(gs, GameState):
            return gs.current_turn_seat()
        return gs.get_current_player()

    def _get_player_name(self, seat: int) -> str:
        """席番号からプレイヤー名を返す（GameState は名前を持たないため空文字）。"""
        gs = self._game_state
        if isinstance(gs, GameState):
            return ""
        return gs.get_player_name(seat)

    # ――― イベントハンドラ ―――

    def _handle_audio_event(self, event: AudioEvent) -> None:
        from core.constants import Street
        action = event.action
        gs     = self._game_state

        if action == "new_hand":
            self._start_new_hand()
            return

        if action == "showdown":
            gs.advance_street(Street.SHOWDOWN)
            self._street_action_count = 0
            return

        if action == "winner":
            winner_seat = _extract_seat_from_text(event.raw_text)
            if winner_seat is None:
                try:
                    winner_seat = self._get_current_player_seat()
                except (RuntimeError, AttributeError):
                    logger.warning("Could not determine winner seat from %r", event.raw_text)
                    return
                logger.warning(
                    "Could not extract winner seat from %r, using current player seat=%d",
                    event.raw_text, winner_seat,
                )
            self._finalize_hand(winner_seat)
            return

        # ── アクションイベント処理 ──────────────────────────────────────────

        if isinstance(gs, GameState):
            # FR-26–29: estimate_actor でアクター推定
            actor_scores = estimate_actor(event, gs)
            non_zero     = {s: v for s, v in actor_scores.items() if v > 0.0}
            if non_zero:
                seat       = max(non_zero, key=non_zero.__getitem__)
                actor_conf = non_zero[seat]
            else:
                try:
                    seat = gs.current_turn_seat()
                except (RuntimeError, AttributeError):
                    logger.warning("No active seats in GameState")
                    return
                actor_conf = 0.0

            self._street_action_count += 1

            # FR-22–25: 矛盾検知
            contradictions = detect_contradictions(
                event, gs, actor_scores,
                street_action_count=self._street_action_count,
            )
            needs_review = bool(contradictions)
            position     = gs.position_map.get(seat, "")
            player_name  = ""

        else:
            # 後方互換: GameStateManager パス
            seat         = gs.get_current_player()
            actor_conf   = 0.0
            needs_review = False
            position     = ""
            player_name  = gs.get_player_name(seat)

        try:
            gs.apply_action(seat, action, event.amount or 0)
        except ValueError:
            logger.exception("apply_action failed (seat=%d, action=%s)", seat, action)
            needs_review = True

        cam_event  = self._pop_matching_camera_event(seat, event.timestamp)
        rfid_event = self._pop_matching_rfid_event(seat, event.timestamp)

        has_camera = cam_event  is not None
        has_rfid   = rfid_event is not None

        source     = {"camera": has_camera, "audio": True, "rfid": has_rfid}
        confidence = calc_confidence(has_rfid=has_rfid, has_audio=True, has_camera=has_camera)

        record = ActionRecord(
            hand_id=gs.hand_id,
            timestamp=_now_iso(),
            street=gs.street,
            seat=seat,
            player_name=player_name,
            action=action,
            amount=event.amount if event.amount is not None else 0,
            pot_after=gs.pot,
            stack_after=gs.get_stack(seat),
            source=source,
            needs_review=needs_review,
            confidence=confidence,
            position=position,
            actor_confidence=actor_conf,
        )
        self._current_actions.append(record)

        if self._on_action:
            self._on_action(record)

        logger.debug("ActionRecord: %s", record)

    # ――― ハンド開始 / 終了 ―――

    def _start_new_hand(self) -> None:
        gs = self._game_state
        gs.new_hand()
        self._current_actions     = []
        self._hand_started_at     = _now_iso()
        self._stack_start         = gs.get_stacks()
        self._board_cards         = []
        self._board_positions     = {}
        self._board_source        = ""
        self._hole_cards          = {}
        self._street_action_count = 0
        logger.info("New hand started: hand_id=%d", gs.hand_id)

    def _finalize_hand(self, winner_seat: int) -> None:
        gs = self._game_state
        gs.end_hand(winner_seat)

        stacks_end   = gs.get_stacks()
        players_info = []

        if isinstance(gs, GameState):
            all_seats = gs.all_seats
        else:
            all_seats = sorted(stacks_end.keys())

        for seat in all_seats:
            hole = self._hole_cards.get(seat, [])
            players_info.append({
                "seat":              seat,
                "name":              self._get_player_name(seat),
                "hole_cards":        list(hole) if hole else None,
                "hole_cards_source": "rfid" if hole else "",
                "stack_start":       self._stack_start.get(seat, 0),
                "stack_end":         stacks_end.get(seat, 0),
                "result":            stacks_end.get(seat, 0) - self._stack_start.get(seat, 0),
            })

        if isinstance(gs, GameState):
            sb_val = gs._sb   # noqa: SLF001
            bb_val = gs._bb   # noqa: SLF001
            btn_seat = gs.button_seat
            pos_map  = dict(gs.position_map)
        else:
            sb_val   = gs._sb   # noqa: SLF001
            bb_val   = gs._bb   # noqa: SLF001
            btn_seat = 0
            pos_map  = {}

        summary = HandSummary(
            hand_id=gs.hand_id,
            session_id=self._json_writer._session_id,   # noqa: SLF001
            started_at=self._hand_started_at,
            ended_at=_now_iso(),
            blinds={"sb": sb_val, "bb": bb_val},
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
            button_seat=btn_seat,
            position_map=pos_map,
        )

        self._json_writer.append_hand_summary(summary)
        logger.info("Hand %d finalized. Winner: seat %d", gs.hand_id, winner_seat)
        self._current_actions     = []
        self._street_action_count = 0


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
