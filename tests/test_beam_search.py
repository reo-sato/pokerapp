"""tests/test_beam_search.py

BeamEngine の単体テスト (M3)。
- 剪定 (top-K の維持)
- 決定論性 (同じ入力で同じ出力)
- enable_resample=False (M3 初版)
- map_action / map_sequence
- apply_winner_filter (folded winner 粒子の不整合)
"""
from __future__ import annotations

import time
from typing import Optional

import pytest

from core.events import ASRAlternative, AudioEvent
from integration.action_inference import BettingState
from integration.beam_search import BeamEngine, BeamParticle
from integration.observation_model import ActionHypothesis, NEG_INF, default_priors


def _state(**kwargs) -> BettingState:
    s = BettingState()
    s.bb_amount = kwargs.pop("bb_amount", 200)
    s.sb_amount = kwargs.pop("sb_amount", 100)
    s.active_seats = kwargs.pop("active_seats", [1, 2, 3, 4])
    s.button_seat = kwargs.pop("button_seat", 4)
    s.is_initialized = True
    for k, v in kwargs.items():
        setattr(s, k, v)
    return s


def _audio(action: str = "call", amount: int = 200, raw: str = "コール") -> AudioEvent:
    return AudioEvent(action=action, amount=amount, timestamp=time.time(), raw_text=raw)


# ── 構造 ─────────────────────────────────────────────────────────────────────

class TestStructure:
    def test_initial_one_particle(self) -> None:
        beam = BeamEngine(K=8)
        assert beam.particle_count() == 1
        assert beam.map_action() is None  # まだ何も step してない

    def test_reset_with_state_collapses_to_one(self) -> None:
        beam = BeamEngine(K=8)
        beam.reset_with_state(_state(current_bet=200, is_opened=True))
        assert beam.particle_count() == 1
        assert beam.map_action() is None

    def test_default_K_is_positive(self) -> None:
        assert BeamEngine(K=8).K == 8
        assert BeamEngine(K=0).K == 1   # min clamp


# ── 1 step ───────────────────────────────────────────────────────────────────

class TestStepAudio:
    def test_step_records_action(self) -> None:
        beam = BeamEngine(K=8)
        beam.reset_with_state(_state(current_bet=200, is_opened=True))
        beam.step_audio(_audio("call", 200, "コール 200"), actor_seat=1)
        top = beam.map_action()
        assert top is not None
        assert top.action == "call"
        assert top.amount == 200

    def test_step_keeps_at_most_K_particles(self) -> None:
        beam = BeamEngine(K=3)
        beam.reset_with_state(_state(current_bet=200, is_opened=True))
        beam.step_audio(_audio("call", 200, "コール"), actor_seat=1)
        assert beam.particle_count() <= 3

    def test_top_particle_is_primary(self) -> None:
        """primary (log_likelihood=0.0) が常に top 粒子に来る。"""
        beam = BeamEngine(K=8)
        beam.reset_with_state(_state(current_bet=200, is_opened=True))
        beam.step_audio(_audio("call", 200, "コール"), actor_seat=1)
        top = beam.map_action()
        # primary は alternative_ 接頭辞ではない
        assert not top.reason.startswith("alternative_")

    def test_deterministic_replay(self) -> None:
        """同じ入力なら出力も同じ (再現性)。"""
        def _run() -> Optional[ActionHypothesis]:
            beam = BeamEngine(K=8)
            beam.reset_with_state(_state(current_bet=200, is_opened=True))
            beam.step_audio(_audio("call", 200), actor_seat=1)
            beam.step_audio(_audio("fold", 0, "フォールド"), actor_seat=2)
            return beam.map_action()

        a = _run()
        b = _run()
        assert a is not None and b is not None
        assert a.action == b.action and a.amount == b.amount


# ── map_sequence ─────────────────────────────────────────────────────────────

class TestMapSequence:
    def test_sequence_length_matches_steps(self) -> None:
        beam = BeamEngine(K=8)
        beam.reset_with_state(_state(current_bet=200, is_opened=True))
        beam.step_audio(_audio("call", 200), actor_seat=1)
        beam.step_audio(_audio("fold", 0, "フォールド"), actor_seat=2)
        seq = beam.map_sequence()
        assert len(seq) == 2
        assert seq[0].action == "call"
        assert seq[1].action == "fold"

    def test_seat_attached_to_each_action(self) -> None:
        beam = BeamEngine(K=8)
        beam.reset_with_state(_state(current_bet=200, is_opened=True))
        beam.step_audio(_audio("call", 200), actor_seat=3)
        seq = beam.map_sequence()
        assert seq[0].seat == 3


# ── apply_winner_filter ──────────────────────────────────────────────────────

class TestWinnerFilter:
    def test_folded_winner_particle_eliminated(self) -> None:
        """winner_seat が fold した粒子は NEG_INF に落ち、別の MAP が選ばれる。"""
        beam = BeamEngine(K=8)
        state = _state(current_bet=200, is_opened=True)
        beam.reset_with_state(state)
        # seat 3 が一見 fold したように見える状況
        beam.step_audio(_audio("fold", 0, "フォールド"), actor_seat=3)
        # 後で WINNER=3 と判明 (= seat 3 は実際 fold していなかった)
        revised = beam.apply_winner_filter(winner_seat=3, final_pot=None)
        # primary は (s3 = fold) のままだが、その粒子は NEG_INF にされている
        # 別粒子 (alternative の中で fold でないもの) が top に来るか、空が返る
        if revised:
            assert revised[0].action != "fold" or revised[0].seat != 3

    def test_winner_filter_no_fold_returns_unchanged(self) -> None:
        beam = BeamEngine(K=8)
        beam.reset_with_state(_state(current_bet=200, is_opened=True))
        beam.step_audio(_audio("call", 200), actor_seat=1)
        # seat 1 が勝者で、fold していないので filter は無効
        before = beam.map_sequence()
        revised = beam.apply_winner_filter(winner_seat=1, final_pot=None)
        assert len(revised) == len(before)
        assert revised[0].action == before[0].action

    def test_empty_beam_returns_empty(self) -> None:
        beam = BeamEngine(K=8)
        # step もせず winner_filter
        revised = beam.apply_winner_filter(winner_seat=1)
        assert revised == []


# ── snapshot ─────────────────────────────────────────────────────────────────

class TestSnapshot:
    def test_snapshot_top_returns_dicts(self) -> None:
        beam = BeamEngine(K=8)
        beam.reset_with_state(_state(current_bet=200, is_opened=True))
        beam.step_audio(_audio("call", 200), actor_seat=1)
        snap = beam.snapshot_top(3)
        assert len(snap) <= 3
        assert all("log_weight" in s for s in snap)
        assert all("actions" in s for s in snap)
        # top の最新アクションを覗いて action フィールドが入っている
        top_actions = snap[0]["actions"]
        if top_actions:
            assert "action" in top_actions[-1]
            assert "seat" in top_actions[-1]


# ── enable_resample (M3 は False のみ) ───────────────────────────────────────

class TestResampleFlag:
    def test_default_false(self) -> None:
        assert BeamEngine(K=8).enable_resample is False

    def test_can_be_set_true_but_not_used_in_m3(self) -> None:
        # フラグは受け取れるが、M3 では決定論的剪定のみ。
        # (確率的リサンプリングは v6.0+ B4 で実装)
        beam = BeamEngine(K=8, enable_resample=True)
        assert beam.enable_resample is True
        beam.reset_with_state(_state(current_bet=200, is_opened=True))
        beam.step_audio(_audio("call", 200), actor_seat=1)
        # 動作は正常 (決定論的にステップ)
        assert beam.map_action() is not None
