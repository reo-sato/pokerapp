"""tests/test_integration.py

Phase 3: IntegrationThread の ±2秒マッチングと confidence スコアのテスト。
Phase 4: estimate_actor / detect_contradictions / process_event のテスト。
"""
from __future__ import annotations

import queue
import threading
import time
from pathlib import Path

import pytest

from core.event_queue import make_audio_queue, make_camera_queue
from core.events import AudioEvent, CameraEvent
from core.game_state import GameState, GameStateManager
from core.hand_log import ActionRecord, PlayerState
from integration.engine import (
    MATCH_WINDOW,
    IntegrationThread,
    PhaseRecord,
    _CONF_AUDIO_CAMERA,
    _CONF_AUDIO_ONLY,
    detect_contradictions,
    estimate_actor,
    process_event,
)
from output.json_writer import JsonWriter


# ――― フィクスチャ ―――

def _make_game() -> GameStateManager:
    players = [
        PlayerState(seat=1, name="Alice", stack=10000),
        PlayerState(seat=2, name="Bob",   stack=10000),
    ]
    gs = GameStateManager(players=players, sb=100, bb=200)
    gs.new_hand()
    return gs


def _run_thread(thread: IntegrationThread, stop: threading.Event, join_timeout: float = 2.0) -> None:
    thread.start()
    time.sleep(0.3)  # イベント処理を待つ
    stop.set()
    thread.join(timeout=join_timeout)


# ――― テストケース ―――

class TestAudioOnlyConfidence:
    """camera_queue なし（または同席カメライベントなし）の場合、confidence = _CONF_AUDIO_ONLY。"""

    def test_audio_only_no_camera_queue(self, tmp_path: Path) -> None:
        gs = _make_game()
        audio_q = make_audio_queue()
        writer = JsonWriter(log_dir=tmp_path, session_id="s1")
        stop = threading.Event()
        captured: list[ActionRecord] = []

        thread = IntegrationThread(
            audio_queue=audio_q,
            game_state=gs,
            json_writer=writer,
            camera_queue=None,
            on_action=captured.append,
            stop_event=stop,
        )

        audio_q.put(AudioEvent(action="bet", amount=500, timestamp=time.time(), raw_text="ベット500"))
        _run_thread(thread, stop)

        assert len(captured) == 1
        rec = captured[0]
        assert rec.source == {"camera": False, "audio": True, "rfid": False}
        assert rec.confidence == _CONF_AUDIO_ONLY

    def test_audio_only_with_camera_queue_but_no_events(self, tmp_path: Path) -> None:
        gs = _make_game()
        audio_q = make_audio_queue()
        camera_q = make_camera_queue()
        writer = JsonWriter(log_dir=tmp_path, session_id="s2")
        stop = threading.Event()
        captured: list[ActionRecord] = []

        thread = IntegrationThread(
            audio_queue=audio_q,
            game_state=gs,
            json_writer=writer,
            camera_queue=camera_q,
            on_action=captured.append,
            stop_event=stop,
        )

        audio_q.put(AudioEvent(action="check", amount=0, timestamp=time.time(), raw_text="チェック"))
        _run_thread(thread, stop)

        assert len(captured) == 1
        assert captured[0].confidence == _CONF_AUDIO_ONLY
        assert captured[0].source["camera"] is False


@pytest.mark.skip(reason="camera deprecated in spec v4.0")
class TestCameraCorroboration:
    """同席・±MATCH_WINDOW 秒以内の CameraEvent がある場合、confidence = _CONF_AUDIO_CAMERA。"""

    def test_camera_before_audio_within_window(self, tmp_path: Path) -> None:
        gs = _make_game()
        audio_q = make_audio_queue()
        camera_q = make_camera_queue()
        writer = JsonWriter(log_dir=tmp_path, session_id="s3")
        stop = threading.Event()
        captured: list[ActionRecord] = []

        thread = IntegrationThread(
            audio_queue=audio_q,
            game_state=gs,
            json_writer=writer,
            camera_queue=camera_q,
            on_action=captured.append,
            stop_event=stop,
        )

        now = time.time()
        # カメライベントを 1秒前のタイムスタンプで投入（ウィンドウ内）
        camera_q.put(CameraEvent(seat=1, timestamp=now - 1.0))
        audio_q.put(AudioEvent(action="raise", amount=800, timestamp=now, raw_text="レイズ800"))

        _run_thread(thread, stop)

        assert len(captured) == 1
        rec = captured[0]
        assert rec.source == {"camera": True, "audio": True, "rfid": False}
        assert rec.confidence == _CONF_AUDIO_CAMERA

    def test_camera_after_audio_within_window(self, tmp_path: Path) -> None:
        """AudioEvent より後のカメライベントも ±MATCH_WINDOW 内なのでマッチする。"""
        gs = _make_game()
        audio_q = make_audio_queue()
        camera_q = make_camera_queue()
        writer = JsonWriter(log_dir=tmp_path, session_id="s4")
        stop = threading.Event()
        captured: list[ActionRecord] = []

        thread = IntegrationThread(
            audio_queue=audio_q,
            game_state=gs,
            json_writer=writer,
            camera_queue=camera_q,
            on_action=captured.append,
            stop_event=stop,
        )

        now = time.time()
        # カメライベントを 1秒後のタイムスタンプで投入（ウィンドウ内）
        camera_q.put(CameraEvent(seat=1, timestamp=now + 1.0))
        audio_q.put(AudioEvent(action="call", amount=300, timestamp=now, raw_text="コール300"))

        _run_thread(thread, stop)

        assert len(captured) == 1
        rec = captured[0]
        assert rec.source["camera"] is True
        assert rec.confidence == _CONF_AUDIO_CAMERA


@pytest.mark.skip(reason="camera deprecated in spec v4.0")
class TestCameraWindowBoundary:
    """ウィンドウ外のカメライベントはマッチしない。"""

    def test_camera_outside_window_no_match(self, tmp_path: Path) -> None:
        gs = _make_game()
        audio_q = make_audio_queue()
        camera_q = make_camera_queue()
        writer = JsonWriter(log_dir=tmp_path, session_id="s5")
        stop = threading.Event()
        captured: list[ActionRecord] = []

        thread = IntegrationThread(
            audio_queue=audio_q,
            game_state=gs,
            json_writer=writer,
            camera_queue=camera_q,
            on_action=captured.append,
            stop_event=stop,
        )

        now = time.time()
        # ウィンドウ外（MATCH_WINDOW + 0.1 秒前）
        camera_q.put(CameraEvent(seat=1, timestamp=now - MATCH_WINDOW - 0.1))
        audio_q.put(AudioEvent(action="bet", amount=200, timestamp=now, raw_text="ベット200"))

        _run_thread(thread, stop)

        assert len(captured) == 1
        rec = captured[0]
        assert rec.source["camera"] is False
        assert rec.confidence == _CONF_AUDIO_ONLY

    def test_wrong_seat_camera_event_no_match(self, tmp_path: Path) -> None:
        """席番号が異なるカメライベントはマッチしない。"""
        gs = _make_game()
        audio_q = make_audio_queue()
        camera_q = make_camera_queue()
        writer = JsonWriter(log_dir=tmp_path, session_id="s6")
        stop = threading.Event()
        captured: list[ActionRecord] = []

        thread = IntegrationThread(
            audio_queue=audio_q,
            game_state=gs,
            json_writer=writer,
            camera_queue=camera_q,
            on_action=captured.append,
            stop_event=stop,
        )

        now = time.time()
        # 席2のカメライベントだが、現在のターンは席1
        camera_q.put(CameraEvent(seat=2, timestamp=now))
        audio_q.put(AudioEvent(action="fold", amount=0, timestamp=now, raw_text="フォールド"))

        _run_thread(thread, stop)

        assert len(captured) == 1
        assert captured[0].source["camera"] is False
        assert captured[0].confidence == _CONF_AUDIO_ONLY


@pytest.mark.skip(reason="camera deprecated in spec v4.0")
class TestCameraBufferExpiry:
    """古いカメライベントはバッファから破棄される。"""

    def test_expired_camera_event_is_not_matched(self, tmp_path: Path) -> None:
        from integration.engine import CAMERA_BUFFER_TTL
        gs = _make_game()
        audio_q = make_audio_queue()
        camera_q = make_camera_queue()
        writer = JsonWriter(log_dir=tmp_path, session_id="s7")
        stop = threading.Event()
        captured: list[ActionRecord] = []

        thread = IntegrationThread(
            audio_queue=audio_q,
            game_state=gs,
            json_writer=writer,
            camera_queue=camera_q,
            on_action=captured.append,
            stop_event=stop,
        )

        now = time.time()
        # CAMERA_BUFFER_TTL より古いタイムスタンプ → expire で削除される
        camera_q.put(CameraEvent(seat=1, timestamp=now - CAMERA_BUFFER_TTL - 0.1))
        audio_q.put(AudioEvent(action="check", amount=0, timestamp=now, raw_text="チェック"))

        _run_thread(thread, stop)

        assert len(captured) == 1
        assert captured[0].source["camera"] is False


class TestMultipleActions:
    """複数アクション: カメライベントが使い捨てになることを確認。"""

    @pytest.mark.skip(reason="camera deprecated in spec v4.0")
    def test_camera_event_consumed_once(self, tmp_path: Path) -> None:
        """1つのカメライベントは1つのアクションにしか使われない。"""
        gs = _make_game()
        audio_q = make_audio_queue()
        camera_q = make_camera_queue()
        writer = JsonWriter(log_dir=tmp_path, session_id="s8")
        stop = threading.Event()
        captured: list[ActionRecord] = []

        thread = IntegrationThread(
            audio_queue=audio_q,
            game_state=gs,
            json_writer=writer,
            camera_queue=camera_q,
            on_action=captured.append,
            stop_event=stop,
        )

        now = time.time()
        camera_q.put(CameraEvent(seat=1, timestamp=now))
        # 2つのアクション、カメライベントは1つだけ
        audio_q.put(AudioEvent(action="bet", amount=400, timestamp=now, raw_text="ベット400"))
        audio_q.put(AudioEvent(action="call", amount=400, timestamp=now, raw_text="コール400"))

        _run_thread(thread, stop, join_timeout=3.0)

        assert len(captured) == 2
        # 最初のアクションのみカメラマッチ
        assert captured[0].source["camera"] is True
        assert captured[0].confidence == _CONF_AUDIO_CAMERA
        # 2番目はカメラなし
        assert captured[1].source["camera"] is False
        assert captured[1].confidence == _CONF_AUDIO_ONLY


# ══════════════════════════════════════════════════════════════════════════════
# Phase 4: estimate_actor / detect_contradictions / process_event
# ══════════════════════════════════════════════════════════════════════════════

def _make_gs(seats=(1, 2, 3, 4), button_seat: int = 4) -> GameState:
    """テスト用 GameState を生成して new_hand() を呼んだ状態で返す。

    デフォルト button_seat=4 で 4 人テーブルを作ると PREFLOP の
    turn_order = [3, 4, 1, 2]（UTG=seat3 先行）になる。
    """
    players = [PlayerState(seat=s, name=f"P{s}", stack=10_000) for s in seats]
    state = GameState(players=players, sb=100, bb=200, button_seat=button_seat)
    state.new_hand()
    return state


def _audio(action: str, amount=None, mentioned_seat=None, mentioned_position=None) -> AudioEvent:
    return AudioEvent(
        action=action,
        amount=amount,
        timestamp="2026-04-10T00:00:00",
        raw_text=action,
        mentioned_seat=mentioned_seat,
        mentioned_position=mentioned_position,
    )


# ── estimate_actor ────────────────────────────────────────────────────────────

class TestEstimateActor:
    def test_current_turn_seat_gets_max_probability(self):
        """current_turn_seat=3 で CALL → seat=3 が最大確率（FR-26）。"""
        state = _make_gs()
        # button=4 → turn_order=[3,4,1,2] → current=3
        assert state.current_turn_seat() == 3

        # CALL を legal にするため call_amount を設定（全席同等条件）
        state.call_amount = 200
        state.invested    = {s: 0 for s in state.all_seats}

        scores = estimate_actor(_audio("call"), state)

        assert max(scores, key=scores.get) == 3

    def test_folded_seat_has_zero_regardless_of_mention(self):
        """folded_seats に seat=2 が含まれ mentioned_seat=2 でも確率 0.0（FR-27）。"""
        state = _make_gs()
        state.folded_seats.add(2)
        if 2 in state.turn_order:
            state.turn_order.remove(2)
        state.call_amount = 200
        state.invested    = {s: 0 for s in state.all_seats}

        scores = estimate_actor(_audio("call", mentioned_seat=2), state)

        assert scores.get(2, 0.0) == 0.0

    def test_all_in_seat_has_zero(self):
        """all_in_seats に含まれる席は確率 0.0。"""
        state = _make_gs()
        state.all_in_seats.add(1)
        state.call_amount = 0

        scores = estimate_actor(_audio("check"), state)

        assert scores.get(1, 0.0) == 0.0

    def test_action_not_legal_gives_zero(self):
        """BET が legal でない席（call_diff > 0）は確率 0.0。"""
        state = _make_gs()
        # call_amount > 0 → legal = {CALL, RAISE, FOLD}; BET は含まれない
        state.call_amount = 300
        state.invested    = {s: 0 for s in state.all_seats}

        scores = estimate_actor(_audio("bet"), state)

        # BET は誰にも legal でないので全席 0.0 → 正規化後も全 0
        assert all(v == 0.0 for v in scores.values())

    def test_mentioned_seat_gets_boost(self):
        """mentioned_seat はブーストを受け、current_turn_seat 以外の席でも
        ブーストなしの席より確率が高くなる（FR-28）。"""
        state = _make_gs()
        # current=3, mentioned=1
        state.call_amount = 0
        state.invested    = {s: 0 for s in state.all_seats}

        scores = estimate_actor(_audio("check", mentioned_seat=1), state)

        # seat=1 はブースト (0.10+0.09=0.19) で seat=2,4 (0.10 each) より高い
        assert scores.get(1, 0) > scores.get(2, 0)
        assert scores.get(1, 0) > scores.get(4, 0)

    def test_position_mention_boost(self):
        """mentioned_position が position_map に一致する席がブーストを受ける（FR-28）。"""
        state = _make_gs()
        state.call_amount = 0

        # position_map から "BTN" ポジションの席を探す
        btn_seat = next(
            (s for s, p in state.position_map.items() if p == "BTN"), None
        )
        if btn_seat is None:
            pytest.skip("BTN seat not found in position_map")

        scores = estimate_actor(_audio("check", mentioned_position="BTN"), state)

        # BTN 席は他の非 current-turn 席よりも確率が高い
        other_non_current = [
            s for s in state.all_seats
            if s != btn_seat and s != state.current_turn_seat()
        ]
        if other_non_current:
            assert scores.get(btn_seat, 0) > scores.get(other_non_current[0], 0)

    def test_scores_sum_to_one(self):
        """正規化後の確率の合計が 1.0 になる（または全 0）。"""
        state = _make_gs()
        state.call_amount = 200
        state.invested    = {s: 0 for s in state.all_seats}

        scores = estimate_actor(_audio("call"), state)

        total = sum(scores.values())
        assert abs(total - 1.0) < 1e-9 or total == 0.0


# ── detect_contradictions ─────────────────────────────────────────────────────

class TestDetectContradictions:
    def test_illegal_action_detected(self):
        """legal_actions に含まれないアクション → "legal_actions" を含む矛盾（FR-23）。"""
        state = _make_gs()
        # call_amount=0 → legal = {CHECK, BET}; CALL は不正
        state.call_amount = 0

        contradictions = detect_contradictions(_audio("call"), state)

        assert any("legal_actions" in c for c in contradictions)

    def test_legal_action_no_contradiction(self):
        """legal_actions に含まれるアクション → 矛盾なし。"""
        state = _make_gs()
        state.call_amount = 0

        contradictions = detect_contradictions(_audio("check"), state)

        assert not any("legal_actions" in c for c in contradictions)

    def test_street_overflow_detected(self):
        """ストリート内音声アクション数 > アクティブ席数 → "overflow" を含む矛盾（FR-24）。"""
        state = _make_gs(seats=(1, 2, 3))  # 3 active seats
        state.call_amount = 0

        # street_action_count=4 > active_seats=3 → overflow
        contradictions = detect_contradictions(
            _audio("check"), state, street_action_count=4
        )

        assert any("overflow" in c for c in contradictions)

    def test_no_overflow_within_limit(self):
        """アクション数がアクティブ席数以内 → オーバーフロー矛盾なし。"""
        state = _make_gs(seats=(1, 2, 3))
        state.call_amount = 0

        contradictions = detect_contradictions(
            _audio("check"), state, street_action_count=3
        )

        assert not any("overflow" in c for c in contradictions)

    def test_fold_contradiction_already_folded(self):
        """すでにフォールド済みの席への FOLD 音声 → "fold_contradiction" 矛盾（FR-25）。"""
        state = _make_gs()
        state.call_amount = 200
        state.invested    = {s: 0 for s in state.all_seats}

        # best_seat=3 (current_turn) をフォールド済みにする
        state.folded_seats.add(3)
        if 3 in state.turn_order:
            state.turn_order.remove(3)

        # actor_scores で seat=3 を best にするよう手動設定
        actor_scores = {1: 0.0, 2: 0.0, 3: 1.0, 4: 0.0}
        contradictions = detect_contradictions(
            _audio("fold"), state, actor_scores=actor_scores
        )

        assert any("fold_contradiction" in c for c in contradictions)

    def test_actor_ambiguous_when_close_scores(self):
        """最大確率と次点の差が _AMBIGUITY_THRESHOLD 未満 → "actor_ambiguous" 矛盾（FR-29）。"""
        state = _make_gs()
        # 0.51 と 0.49 → 差 0.02 < 0.30
        actor_scores = {1: 0.0, 2: 0.0, 3: 0.51, 4: 0.49}

        contradictions = detect_contradictions(
            _audio("check"), state, actor_scores=actor_scores
        )

        assert any("actor_ambiguous" in c for c in contradictions)

    def test_no_ambiguity_when_scores_clear(self):
        """差が _AMBIGUITY_THRESHOLD 以上 → "actor_ambiguous" 矛盾なし。"""
        state = _make_gs()
        # 0.90 と 0.10 → 差 0.80 ≥ 0.30
        actor_scores = {1: 0.0, 2: 0.0, 3: 0.90, 4: 0.10}

        contradictions = detect_contradictions(
            _audio("check"), state, actor_scores=actor_scores
        )

        assert not any("actor_ambiguous" in c for c in contradictions)

    def test_rfid_street_mismatch(self):
        """audio ストリート=preflop, RFID ボード 3 枚 → "street_mismatch" 矛盾（FR-25）。"""
        state = _make_gs()  # street = preflop
        state.call_amount = 0

        contradictions = detect_contradictions(
            _audio("check"), state, rfid_board_count=3  # flop のはず
        )

        assert any("street_mismatch" in c for c in contradictions)


# ── process_event ─────────────────────────────────────────────────────────────

class TestProcessEvent:
    def test_action_event_returns_action_record(self):
        """type='action' → ActionRecord が返り状態が更新される。"""
        state = _make_gs()
        state.call_amount = 0

        ev = {"type": "action", "action": "check", "timestamp": "2026-04-10T00:00:00"}
        result = process_event(ev, state)

        assert isinstance(result, ActionRecord)
        assert result.action == "check"

    def test_action_needs_review_on_contradiction(self):
        """legal_actions 違反 → needs_review=True。"""
        state = _make_gs()
        state.call_amount = 0  # CHECK/BET のみ legal

        ev = {"type": "action", "action": "call"}
        result = process_event(ev, state)

        assert isinstance(result, ActionRecord)
        assert result.needs_review is True

    def test_phase_event_new_hand_returns_phase_record(self):
        """type='phase', action='new_hand' → PhaseRecord が返り hand_id がインクリメント。"""
        state = _make_gs()
        prev_hand_id = state.hand_id

        ev = {"type": "phase", "action": "new_hand"}
        result = process_event(ev, state)

        assert isinstance(result, PhaseRecord)
        assert result.action == "new_hand"
        assert state.hand_id == prev_hand_id + 1

    def test_rfid_fold_updates_state(self):
        """type='rfid', rfid_type='fold' → state.folded_seats に追加される。"""
        state = _make_gs()

        ev = {"type": "rfid", "rfid_type": "fold", "seat": 1}
        process_event(ev, state)

        assert 1 in state.folded_seats

    def test_actor_confidence_is_set(self):
        """ActionRecord.actor_confidence が 0.0 より大きい値になる。"""
        state = _make_gs()
        state.call_amount = 0

        ev = {"type": "action", "action": "check"}
        result = process_event(ev, state)

        assert isinstance(result, ActionRecord)
        assert result.actor_confidence > 0.0
