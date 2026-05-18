"""tests/test_inference_equivalence.py

infer_action() の薄いアダプタ化 (M2) における後方互換性検証。

要件:
  max(infer_action_distribution(event, state, seat), key=h.log_likelihood)
    に対応する InferredAction == 既存 _infer_action_core(event, state, seat) の出力

6 つの amount_only ケース + explicit action 検証ケースを網羅する。
"""
from __future__ import annotations

import time

import pytest

from core.events import AudioEvent
from integration.action_inference import (
    BettingState,
    _infer_action_core,
    infer_action,
    infer_action_distribution,
)


def _event(action: str, amount: int = 0, raw_text: str = "") -> AudioEvent:
    return AudioEvent(action=action, amount=amount, timestamp=time.time(), raw_text=raw_text or action)


def _state(**kwargs) -> BettingState:
    s = BettingState()
    s.bb_amount = kwargs.pop("bb_amount", 200)
    s.sb_amount = kwargs.pop("sb_amount", 100)
    for k, v in kwargs.items():
        setattr(s, k, v)
    return s


def _assert_same(infer_event: AudioEvent, state: BettingState, seat=None) -> None:
    legacy = _infer_action_core(infer_event, state, seat)
    adapter = infer_action(infer_event, state, seat)
    assert adapter.action == legacy.action, f"action diff: {adapter.action} vs {legacy.action}"
    assert adapter.amount == legacy.amount, f"amount diff: {adapter.amount} vs {legacy.amount}"
    assert adapter.needs_review == legacy.needs_review
    assert adapter.reason == legacy.reason
    assert adapter.confidence == pytest.approx(legacy.confidence)
    assert adapter.raw_text == legacy.raw_text
    assert adapter.normalized_text == legacy.normalized_text
    assert adapter.seat == legacy.seat


# ── 6 amount_only cases ─────────────────────────────────────────────────────

class TestAmountOnlyEquivalence:
    """_infer_from_amount_only の 6 ケースで legacy == adapter を確認する。"""

    def test_case_a_opening_bet(self) -> None:
        # is_opened=False → BET
        _assert_same(_event("amount_only", 600, "600"), _state(is_opened=False), seat=1)

    def test_case_b_exact_call(self) -> None:
        # is_opened=True, amount == current_bet, contrib == 0 → CALL
        _assert_same(_event("amount_only", 200, "200"),
                     _state(is_opened=True, current_bet=200), seat=1)

    def test_case_c_above_call_no_contrib(self) -> None:
        # is_opened=True, amount > current_bet, contrib == 0 → RAISE
        _assert_same(_event("amount_only", 600, "600"),
                     _state(is_opened=True, current_bet=200), seat=1)

    def test_case_e_reraise(self) -> None:
        # is_opened=True, amount > current_bet, contrib > 0 → RAISE
        s = _state(is_opened=True, current_bet=600)
        s.player_contrib_this_street[1] = 200
        _assert_same(_event("amount_only", 1200, "1200"), s, seat=1)

    def test_case_d_below_call_needs_review(self) -> None:
        # amount < current_bet → needs_review
        _assert_same(_event("amount_only", 100, "100"),
                     _state(is_opened=True, current_bet=200), seat=1)

    def test_edge_no_amount(self) -> None:
        # amount == 0 → needs_review
        _assert_same(_event("amount_only", 0, ""),
                     _state(is_opened=True, current_bet=200), seat=1)


# ── explicit action validation ──────────────────────────────────────────────

class TestExplicitActionEquivalence:
    def test_explicit_bet(self) -> None:
        _assert_same(_event("bet", 400, "ベット400"), _state(is_opened=False), seat=1)

    def test_explicit_call(self) -> None:
        _assert_same(_event("call", 200, "コール"),
                     _state(is_opened=True, current_bet=200), seat=1)

    def test_explicit_check_when_opened_needs_review(self) -> None:
        # check_when_bet_open トリガを保つ
        _assert_same(_event("check", 0, "チェック"),
                     _state(is_opened=True, current_bet=200), seat=1)

    def test_explicit_check_when_no_bet(self) -> None:
        _assert_same(_event("check", 0, "チェック"),
                     _state(is_opened=False, current_bet=0), seat=1)

    def test_explicit_fold(self) -> None:
        _assert_same(_event("fold", 0, "フォールド"),
                     _state(is_opened=True, current_bet=200), seat=1)

    def test_explicit_raise(self) -> None:
        _assert_same(_event("raise", 800, "レイズ800"),
                     _state(is_opened=True, current_bet=200), seat=1)


# ── distribution structure ──────────────────────────────────────────────────

class TestDistributionStructure:
    def test_distribution_top_is_primary(self) -> None:
        """先頭が常に primary 仮説 (log_likelihood=0.0、reason は alternative_ 接頭辞なし)。"""
        dist = infer_action_distribution(
            _event("amount_only", 200), _state(is_opened=True, current_bet=200), actor_seat=1,
        )
        assert len(dist) >= 1
        top = dist[0]
        assert top.log_likelihood == 0.0
        assert not top.reason.startswith("alternative_")
        assert top.action == "call"

    def test_distribution_sorted_descending(self) -> None:
        dist = infer_action_distribution(
            _event("amount_only", 600), _state(is_opened=True, current_bet=200,
                                                button_seat=4, active_seats=[1, 2, 3, 4]),
            actor_seat=2,
        )
        for prev, curr in zip(dist, dist[1:]):
            assert prev.log_likelihood >= curr.log_likelihood

    def test_alternative_log_likelihoods_strictly_below_primary(self) -> None:
        """primary (0.0) 未満であることを保証 (= top-1 が legacy と一致)。"""
        dist = infer_action_distribution(
            _event("call", 200, "コール"),
            _state(is_opened=True, current_bet=200, button_seat=4, active_seats=[1, 2, 3, 4]),
            actor_seat=2,
        )
        primary = dist[0]
        for h in dist[1:]:
            assert h.log_likelihood < primary.log_likelihood

    def test_distribution_includes_alternatives_for_amount_only(self) -> None:
        """is_opened=True, current_bet=200, amount=200 (CALL) なら、代替候補が複数存在する。"""
        dist = infer_action_distribution(
            _event("amount_only", 200),
            _state(is_opened=True, current_bet=200,
                   button_seat=4, active_seats=[1, 2, 3, 4]),
            actor_seat=2,
        )
        alt_reasons = {h.reason for h in dist[1:]}
        # FOLD は常に legal → 代替候補に含まれる
        assert "alternative_fold" in alt_reasons
