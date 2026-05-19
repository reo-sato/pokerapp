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
from core.hand_boundary import BoundaryEvent, HandBoundaryDetector
from core.hand_finalizer import HandFinalizer
from core.hand_log import RevealedHand
from core.hand_reconstructor import HandReconstructionResult, HandReconstructor
from integration.action_inference import BettingState, InferredAction, infer_action
from integration.action_order import advance_button
from integration.beam_search import BeamEngine
from integration.observation_model import ActionHypothesis, default_priors
from output.evidence_log import EvidenceLogWriter
from output.json_writer import JsonWriter
from output.replay_hand import EvidenceRecord

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
        on_action_revised: Optional[Callable[[ActionRecord], None]] = None,
        on_rfid_card: Optional[Callable[[RFIDEvent], None]] = None,
        stop_event: Optional[threading.Event] = None,
        initial_button_seat: Optional[int] = None,
        sb_amount: Optional[int] = None,
        bb_amount: Optional[int] = None,
        auto_post_blinds: bool = True,
        beam_K: int = 8,
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
        self._on_action_revised = on_action_revised
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

        # ベイズ層 (v6.0+ M3) 用: Beam Search エンジン。K=8 で並行宇宙を保持し、
        # WINNER 到着時に後方修正で過去 ActionRecord を遡及書き換えできる。
        self._beam = BeamEngine(
            K=beam_K,
            prior=default_priors(),
            enable_resample=False,                         # M3: 決定論的 Beam のみ
            sink=self._beam_sink_to_evidence_log,
        )

        # Phase 2-C: hand window 検出 + retrospective hook
        # - boundary detector が audio new_hand / winner / RFID board cleared を観測
        # - 各 event を _current_hand_events に push
        # - 終端境界で _completed_hands[hand_id] に window を確定、reconstructor を hook
        self._boundary_detector = HandBoundaryDetector()
        self._hand_reconstructor = HandReconstructor()
        self._current_hand_events: list[EvidenceRecord] = []
        self._completed_hands: dict[int, list[EvidenceRecord]] = {}
        self._last_reconstruction: Optional[HandReconstructionResult] = None

        # ボタン管理
        self._auto_post_blinds = auto_post_blinds
        self._sb_amount = sb_amount if sb_amount is not None else getattr(game_state, "_sb", 0)
        self._bb_amount = bb_amount if bb_amount is not None else getattr(game_state, "_bb", 0)
        self._next_button_seat: Optional[int] = initial_button_seat
        self._last_button_seat: Optional[int] = None

    def stop(self) -> None:
        self._stop_event.set()

    # ── Beam Search → evidence_log への hook ───────────────────────────────

    def _beam_sink_to_evidence_log(self, evidence, top3: list[dict]) -> None:
        """BeamEngine が 1 step ごとに呼ぶ。直前の audio event に top3 粒子のスナップショットを付与する。

        ここでは evidence_log への append-only 出力に top3 を載せた "beam_snapshot" 行を書く。
        """
        try:
            self._evidence_log._write_line({                                # noqa: SLF001
                "ts": evidence.t_end,
                "kind": "beam_snapshot",
                "evidence_kind": evidence.kind,
                "top_particles": top3,
            })
        except Exception:
            logger.exception("beam sink failed")

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
            # Phase 2-C: hand window への蓄積 + boundary 検出
            self._track_evidence("camera", ev)
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
        # Phase 2-C: hand window への蓄積 + boundary 検出
        self._track_evidence("rfid", ev)
        if ev.role == "board":
            self._handle_board_rfid(ev)
        else:
            self._handle_seat_rfid(ev)
        # Phase 2-C: 更新後の board/hole state snapshot を detector に渡し、
        # board cleared (board が空のまま quiet 秒経過) の検出を進める。
        self._observe_state_snapshot(ev.timestamp)

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

    # ――― Phase 2-C: hand window 管理 / boundary detection ―――

    def _track_evidence(self, kind: str, event) -> None:
        """Phase 2-C: イベントを hand window バッファに積み、boundary detector を回す。

        boundary が emit されたら ``_current_hand_events`` の境界処理を行う:
          - end のみ          : trigger event を終わる hand 側に含める → completed に確定
          - start のみ        : trigger event を新 hand の最初の record に
          - end + start (両方) : 前 hand を trigger なしで確定し、trigger を新 hand に
          - 境界なし          : 単純に append

        終端境界では ``_invoke_reconstructor_hook(hand_id)`` を呼ぶが、Phase 2-C の
        ``HandReconstructor`` は skeleton なので online 結果には影響しない。
        """
        record = EvidenceRecord(timestamp=float(event.timestamp), kind=kind, event=event)
        try:
            if kind == "audio":
                boundaries = self._boundary_detector.observe_audio_event(event)
            elif kind == "rfid":
                boundaries = self._boundary_detector.observe_rfid_event(event)
            elif kind == "camera":
                boundaries = self._boundary_detector.observe_camera_event(event)
            else:
                boundaries = []
        except Exception:
            logger.exception("HandBoundaryDetector.observe failed for kind=%s", kind)
            boundaries = []

        self._apply_boundaries(boundaries, record)

    def _observe_state_snapshot(self, now: float) -> None:
        """Phase 2-C: 現在の board/hole スナップショットを detector に渡す。

        ``_handle_board_rfid`` / ``_handle_seat_rfid`` 後に呼び、board 全消滅判定
        (BOARD_EMPTY_QUIET_SEC) と hole_cards_appeared 検出を進める。state-only
        の呼び出しなので record は積まない (event は既に _track_evidence で積み済み)。
        """
        try:
            boundaries = list(
                self._boundary_detector.observe_board_state(list(self._board_cards), now)
            )
            boundaries.extend(
                self._boundary_detector.observe_hole_state(dict(self._hole_cards), now)
            )
        except Exception:
            logger.exception("HandBoundaryDetector snapshot observation failed")
            boundaries = []
        # snapshot 起因の境界は trigger event を伴わない → record=None で処理
        self._apply_boundaries(boundaries, record=None)

    def _apply_boundaries(
        self,
        boundaries: list[BoundaryEvent],
        record: Optional[EvidenceRecord],
    ) -> None:
        has_end = any(b.kind == "end" for b in boundaries)
        has_start = any(b.kind == "start" for b in boundaries)

        if has_end and has_start:
            end_b = next(b for b in boundaries if b.kind == "end")
            # trigger event は新 hand 側に属する (例: new_hand audio)
            self._completed_hands[end_b.hand_id] = list(self._current_hand_events)
            self._invoke_reconstructor_hook(end_b.hand_id)
            self._current_hand_events = [record] if record is not None else []
        elif has_end:
            end_b = next(b for b in boundaries if b.kind == "end")
            # trigger event (例: winner audio) は終わる hand 側に含める
            if record is not None:
                self._current_hand_events.append(record)
            self._completed_hands[end_b.hand_id] = list(self._current_hand_events)
            self._invoke_reconstructor_hook(end_b.hand_id)
            self._current_hand_events = []
        elif has_start:
            self._current_hand_events = [record] if record is not None else []
        else:
            if record is not None:
                self._current_hand_events.append(record)

    def _invoke_reconstructor_hook(self, hand_id: int) -> None:
        """Phase 2-C: 終端境界後に HandReconstructor を呼ぶ hook。

        現状 reconstructor は skeleton で online 結果に介入しない。Phase 3 以降で
        beam 再生 + HandFinalizer 再呼び出しを実装した時に、ここから差分検出 /
        summary 差し替え / needs_review 立てが行われる予定。
        """
        events = self._completed_hands.get(hand_id, [])
        try:
            self._last_reconstruction = self._hand_reconstructor.reconstruct_from_events(
                events,
                initial_state=None,
            )
        except Exception:
            logger.exception("HandReconstructor.reconstruct_from_events failed")
            self._last_reconstruction = None

    # ――― イベントハンドラ ―――

    def _handle_audio_event(self, event: AudioEvent) -> None:
        # M1: raw 観測ログ。推定ロジックの前に常に書き込む (例外でも残す)。
        self._evidence_log.write_audio(event)
        # Phase 2-C: hand window への蓄積 + boundary 検出
        self._track_evidence("audio", event)

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

        # M3: Beam Engine も同じ AudioEvent で更新する。
        # 既存の inferred 出力は legacy MAP のままで動作不変、
        # beam の sequence MAP は _finalize_hand での WINNER 後方修正に使う。
        try:
            self._beam.step_audio(event, seat)
        except Exception:
            logger.exception("BeamEngine.step_audio failed")

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

        # M3: Beam Engine を最新 BettingState で再初期化 (SB/BB post 後の状態を base にする)
        self._beam.reset_with_state(self._betting_state)

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

    def _reconcile_with_beam(self, winner_seat: int) -> None:
        """WINNER 到着時に beam の sequence MAP で _current_actions を遡及修正する (M3)。

        - SB_POST / BB_POST は対象外 (auto-post なので不変)
        - 差分があった ActionRecord は in-place mutate + needs_review=True
        - ``on_action_revised`` コールバックを発火
        - 完全 replay (pot_after/stack_after 再計算) は M3 初版ではスコープ外
        """
        try:
            revised = self._beam.apply_winner_filter(winner_seat, final_pot=None)
        except Exception:
            logger.exception("BeamEngine.apply_winner_filter failed")
            return
        if not revised:
            return

        # SB/BB post を除いた player action だけを reconcile 対象にする
        player_actions = [
            r for r in self._current_actions
            if r.action not in ("SB_POST", "BB_POST")
        ]

        for record, hyp in zip(player_actions, revised):
            if hyp.action is None:
                continue
            if hyp.action == record.action and hyp.amount == record.amount:
                continue
            old_action, old_amount = record.action, record.amount
            record.action = hyp.action
            record.amount = hyp.amount
            record.needs_review = True
            logger.info(
                "WINNER backward fix: hand=%d seat=%s %s %s -> %s %s",
                record.hand_id, record.seat,
                old_action, old_amount, record.action, record.amount,
            )
            if self._on_action_revised is not None:
                try:
                    self._on_action_revised(record)
                except Exception:
                    logger.exception("on_action_revised callback raised")

    def _finalize_hand(self, winner_seat: Optional[int] = None) -> None:
        """Phase 2-B: HandFinalizer (settlement core ベース) でハンドを閉じる。

        ``winner_seat`` は音声 WINNER 観測由来の **補助情報** として扱う。
        canonical な resolution_type / seat_payouts / pots は HandFinalizer が
        BettingState + revealed hole cards + board から導く。
        winner_seat と settlement が食い違う場合は HandFinalizer 側で
        review_required=True が立つ。
        """
        gs = self._game_state

        # M3 legacy: WINNER 到着で BeamEngine の sequence MAP を再フィルタし
        # _current_actions を遡及修正 (in-place mutate)。HandFinalizer が読む
        # actions は修正後の値になる。
        if winner_seat is not None:
            self._reconcile_with_beam(winner_seat)

        # pot_total は betting_state.player_contrib_hand の総和 (SB/BB 含む全 seat の
        # hand 累積投入額)。これにより blind only + fold の hand でも正しく算出される
        # (Phase 1 の action.amount 合計方式は SB_POST/BB_POST を見落として 0 を返していた)。
        pot_total = sum((self._betting_state.player_contrib_hand or {}).values())
        if pot_total == 0:
            # fallback: betting_state 未初期化時の旧経路
            pot_total = sum(
                a.amount for a in self._current_actions
                if a.action in ("bet", "raise", "call", "allin")
            )

        # RFID 由来の hole cards を RevealedHand 集合に変換 (canonical な内部表現)。
        # Phase 2-B 時点では ShowdownTracker は未実装なので self._hole_cards から直接構築。
        revealed_hands = [
            RevealedHand(seat=s, cards=list(cards), source="rfid", observed_at=None)
            for s, cards in sorted(self._hole_cards.items())
            if cards
        ]

        # pre-payout players_info (stack_end は placeholder。HandFinalizer 通過後に更新)
        pre_stacks = gs.get_stacks()
        pre_players_info: list[dict] = []
        for seat in sorted(pre_stacks.keys()):
            hole = self._hole_cards.get(seat, [])
            pre_players_info.append({
                "seat":              seat,
                "name":              gs.get_player_name(seat),
                "hole_cards":        list(hole) if hole else None,
                "hole_cards_source": "rfid" if hole else "",
                "stack_start":       self._stack_start.get(seat, 0),
                "stack_end":         pre_stacks[seat],
                "result":            pre_stacks[seat] - self._stack_start.get(seat, 0),
            })

        if self._hole_cards:
            logger.info(
                "Hand %d: hole_cards from RFID — %s",
                gs.hand_id,
                dict(self._hole_cards),
            )

        # ── HandFinalizer で resolution_type を canonical に決定 ──────────
        finalizer = HandFinalizer()
        summary = finalizer.finalize(
            betting_state=self._betting_state,
            board=list(self._board_cards),
            revealed_hands=revealed_hands,
            pot_total=pot_total,
            players_info=pre_players_info,
            hand_id=gs.hand_id,
            session_id=self._json_writer._session_id,         # noqa: SLF001
            started_at=self._hand_started_at,
            ended_at=_now_iso(),
            blinds={"sb": gs._sb, "bb": gs._bb},                # noqa: SLF001
            actions=list(self._current_actions),
            board_source=self._board_source,
            winner_seat_hint=winner_seat,
        )

        # ── GameStateManager への bridge: seat_payouts → primary winner → end_hand ──
        self._apply_payouts_to_gamestate(summary)

        # ── post-payout stacks で players[*].stack_end / result を更新 ──
        post_stacks = gs.get_stacks()
        for player in summary.players:
            seat = player["seat"]
            if seat in post_stacks:
                player["stack_end"] = post_stacks[seat]
                player["result"] = post_stacks[seat] - player.get("stack_start", 0)

        self._json_writer.append_hand_summary(summary)
        logger.info(
            "Hand %d finalized. resolution=%s winner_seat=%s seat_payouts=%s",
            gs.hand_id, summary.resolution_type, summary.winner_seat, summary.seat_payouts,
        )
        self._current_actions = []

    def _apply_payouts_to_gamestate(self, summary: HandSummary) -> None:
        """Phase 2-B 暫定 bridge: HandFinalizer の seat_payouts から primary winner を
        選び、既存 ``GameStateManager.end_hand(winner_seat)`` を呼ぶ。

        split pot / sidepot_showdown の場合、複数 seat への分配は
        ``HandSummary.seat_payouts`` には正しく載るが、``GameStateManager`` の
        ``stack`` 更新は primary 1 人にしか反映されない (legacy API 制約)。
        Phase 2-C で ``end_hand_with_payouts(payouts: dict[int, int])`` への
        signature 拡張時にこの adapter を撤去する予定。

        ``incomplete`` の場合も pot を持ち越さないよう winner_seat (= hint or
        fallback) で end_hand を呼ぶ。
        """
        if summary.seat_payouts:
            primary = max(
                summary.seat_payouts.items(),
                key=lambda kv: (kv[1], -kv[0]),
            )[0]
        else:
            primary = summary.winner_seat
        try:
            self._game_state.end_hand(primary)
        except Exception:
            logger.exception(
                "gs.end_hand failed for hand %d, primary=%s",
                summary.hand_id, primary,
            )


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
