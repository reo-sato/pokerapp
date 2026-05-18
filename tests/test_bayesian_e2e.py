"""tests/test_bayesian_e2e.py

ベイズ推定アクション推定レイヤ (M3) の E2E シナリオ。

scenario_winner_fold_flip:
  3-handed preflop で seat3 (BTN/UTG) が FOLD と発話 → 既存パイプラインは
  FOLD record を確定書き込み。後に WINNER=seat3 が宣言された場合、
  BeamEngine.apply_winner_filter が「winner_seat=3 が fold した粒子」を
  NEG_INF に落とし、後方修正で seat3 の ActionRecord を非 FOLD に書き換える。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from core.event_queue import EventQueue
from core.events import AudioEvent
from core.game_state import GameStateManager, PlayerState
from core.hand_log import ActionRecord
from integration.beam_search import BeamEngine
from integration.engine import IntegrationThread
from integration.action_inference import BettingState
from integration.observation_model import default_priors
from output.json_writer import JsonWriter


# ── 直接 BeamEngine の WINNER fix だけを単体検証する (mock 不要) ───────────────

class TestBeamWinnerFix:
    """integration を介さず BeamEngine 単体で fold→flip が起きることを示す。"""

    def _state_preflop_3p(self) -> BettingState:
        s = BettingState()
        s.bb_amount = 200
        s.sb_amount = 100
        s.active_seats = [1, 2, 3]
        s.button_seat = 3
        s.sb_seat = 1
        s.bb_seat = 2
        s.actor_seat = 3
        s.current_bet = 200
        s.is_opened = True
        s.last_raise_to = 200
        s.is_initialized = True
        return s

    def test_winner_filter_flips_fold_to_alternative(self) -> None:
        beam = BeamEngine(K=8)
        beam.reset_with_state(self._state_preflop_3p())

        # seat3 が「フォールド」と発話 → primary = FOLD、alternative = CALL/RAISE
        event = AudioEvent("fold", 0, time.time(), "フォールド")
        beam.step_audio(event, actor_seat=3)

        # この時点で MAP は FOLD
        top = beam.map_action()
        assert top is not None
        assert top.action == "fold"
        assert top.seat == 3

        # WINNER=seat3 を通知 → FOLD 粒子は NEG_INF、代替が浮上
        revised = beam.apply_winner_filter(winner_seat=3, final_pot=None)
        assert revised, "後方修正後の MAP が空であってはならない"
        flipped = revised[0]
        # FOLD ではなく、seat=3 のままで何らかの legal アクションに書き換わる
        assert flipped.action != "fold"
        assert flipped.seat == 3
        assert flipped.action in {"call", "raise"}


# ── IntegrationThread 経由の E2E ───────────────────────────────────────────────

class TestBackwardFixE2E:
    """IntegrationThread に AudioEvent を流し、WINNER 到着で ActionRecord が
    in-place mutate されることを確認する。"""

    def _build_thread(self, tmp_path: Path):
        players = [
            PlayerState(seat=1, name="A", stack=10000),
            PlayerState(seat=2, name="B", stack=10000),
            PlayerState(seat=3, name="C", stack=10000),
        ]
        gs = GameStateManager(players=players, sb=100, bb=200)
        audio_q = EventQueue()
        writer = JsonWriter(log_dir=tmp_path, session_id="bayes_e2e")
        stop = threading.Event()
        captured: list[ActionRecord] = []
        revised: list[ActionRecord] = []
        thread = IntegrationThread(
            audio_queue=audio_q,
            game_state=gs,
            json_writer=writer,
            on_action=captured.append,
            on_action_revised=revised.append,
            stop_event=stop,
            initial_button_seat=3,
            auto_post_blinds=True,
        )
        return thread, audio_q, stop, captured, revised, writer

    def test_winner_fold_backward_fix_mutates_record(self, tmp_path: Path) -> None:
        thread, audio_q, stop, captured, revised, _writer = self._build_thread(tmp_path)

        now = time.time()
        # 1) new_hand → SB/BB auto-post
        audio_q.put(AudioEvent("new_hand", 0, now, "ハンド開始"))
        # 2) seat3 (BTN/UTG in 3-handed) が「フォールド」発話 → 既存は FOLD と確定
        audio_q.put(AudioEvent("fold", 0, now + 0.5, "フォールド"))
        # 3) WINNER=seat3 を宣言 (= 実は fold していなかった)
        audio_q.put(AudioEvent("winner", 0, now + 1.0, "シート3 ウィナー"))

        thread.start()
        time.sleep(0.8)
        stop.set()
        thread.join(timeout=2.0)

        # captured には 3 件: SB_POST, BB_POST, seat3 のアクション
        # (in-place mutate により、検証時点では seat3 の record は既に書き換わっている)
        assert len(captured) == 3
        assert captured[0].action == "SB_POST"
        assert captured[1].action == "BB_POST"
        seat3_record = captured[2]
        assert seat3_record.seat == 3

        # WINNER 到着で beam が後方修正 → on_action_revised が発火
        assert len(revised) == 1, (
            f"WINNER backward fix で on_action_revised が呼ばれていない (revised={revised})"
        )

        rev = revised[0]
        # in-place mutate: captured[2] (元 fold record) と同一インスタンス
        assert rev is seat3_record, "in-place mutate ではなく別オブジェクトが渡された"
        # FOLD 以外の legal アクションに書き換わっている
        assert rev.action != "fold"
        assert rev.action in {"call", "raise"}
        # needs_review が立っている (M3 の方針)
        assert rev.needs_review is True
        # seat は変わらない
        assert rev.seat == 3
        # captured[2] も同じく書き換わっている (in-place の確認)
        assert seat3_record.action == rev.action
        assert seat3_record.action != "fold"


# ── 健全性: WINNER が現アクションと整合的なら backward fix は走らない ────────

class TestNoSpuriousRevision:
    def test_no_revision_when_winner_consistent(self, tmp_path: Path) -> None:
        """seat1 が CALL → WINNER=seat1。fold は無いので revise は走らない。"""
        players = [
            PlayerState(seat=1, name="A", stack=10000),
            PlayerState(seat=2, name="B", stack=10000),
            PlayerState(seat=3, name="C", stack=10000),
        ]
        gs = GameStateManager(players=players, sb=100, bb=200)
        audio_q = EventQueue()
        writer = JsonWriter(log_dir=tmp_path, session_id="bayes_e2e_clean")
        stop = threading.Event()
        captured: list[ActionRecord] = []
        revised: list[ActionRecord] = []
        thread = IntegrationThread(
            audio_queue=audio_q, game_state=gs, json_writer=writer,
            on_action=captured.append, on_action_revised=revised.append,
            stop_event=stop, initial_button_seat=3, auto_post_blinds=True,
        )

        now = time.time()
        audio_q.put(AudioEvent("new_hand", 0, now, ""))
        audio_q.put(AudioEvent("fold", 0, now + 0.5, "フォールド"))   # seat3 fold
        audio_q.put(AudioEvent("winner", 0, now + 1.0, "シート2 ウィナー"))  # seat2 wins (consistent)

        thread.start()
        time.sleep(0.8)
        stop.set()
        thread.join(timeout=2.0)

        # seat3 の fold は妥当 (winner=2 で矛盾なし) → 後方修正は走らない
        assert len(revised) == 0, f"不要な後方修正が発火している: {revised}"
        fold_records = [r for r in captured if r.action == "fold" and r.seat == 3]
        assert len(fold_records) == 1
        assert fold_records[0].action == "fold"   # 書き換わっていない
        assert fold_records[0].needs_review is False
