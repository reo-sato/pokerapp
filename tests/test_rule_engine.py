# tests/test_rule_engine.py
"""spec.md FR-22〜25 PokerRuleEngine のテスト。"""
from __future__ import annotations

import pytest

from core.rule_engine import (
    ActionType,
    detect_fold_contradiction,
    detect_street_overflow,
    legal_actions,
    validate_action,
)


# ――― テスト用ステートスタブ ―――

class _State:
    """legal_actions / validate_action が参照するフィールドだけを持つ最小スタブ。"""

    def __init__(
        self,
        call_amount: int,
        invested: dict[int, int],
        stacks: dict[int, int],
        folded: set[int] | None = None,
        all_in: set[int] | None = None,
    ) -> None:
        self.call_amount = call_amount
        self.invested = invested
        self._stacks = stacks
        self.folded_seats = set(folded or [])
        self.all_in_seats = set(all_in or [])

    def get_stack(self, seat: int) -> int:
        return self._stacks[seat]


# ――― legal_actions ―――

class TestLegalActions:
    def test_no_bet_facing_check_options(self):
        """call_amount=0, invested=0 → {CHECK, BET}（FR-22）。"""
        state = _State(call_amount=0, invested={1: 0}, stacks={1: 5000})
        assert legal_actions(1, state) == frozenset({ActionType.CHECK, ActionType.BET})

    def test_bet_facing_with_sufficient_stack(self):
        """call_amount=800, invested=0, stack=5000 → {CALL, RAISE, FOLD}（FR-22）。"""
        state = _State(call_amount=800, invested={1: 0}, stacks={1: 5000})
        assert legal_actions(1, state) == frozenset({ActionType.CALL, ActionType.RAISE, ActionType.FOLD})

    def test_stack_too_small_to_call(self):
        """call_amount=800, invested=0, stack=600 → {ALL_IN, FOLD}（FR-22）。"""
        state = _State(call_amount=800, invested={1: 0}, stacks={1: 600})
        assert legal_actions(1, state) == frozenset({ActionType.ALL_IN, ActionType.FOLD})

    def test_exact_stack_equals_call_diff(self):
        """stack == call_diff のとき ALL_IN/FOLD（境界値）。"""
        state = _State(call_amount=500, invested={1: 0}, stacks={1: 500})
        assert legal_actions(1, state) == frozenset({ActionType.ALL_IN, ActionType.FOLD})

    def test_folded_seat_returns_empty(self):
        """フォールド済み席 → frozenset()（FR-22）。"""
        state = _State(call_amount=0, invested={1: 0}, stacks={1: 5000}, folded={1})
        assert legal_actions(1, state) == frozenset()

    def test_all_in_seat_returns_empty(self):
        """オールイン済み席 → frozenset()（FR-22）。"""
        state = _State(call_amount=800, invested={1: 800}, stacks={1: 0}, all_in={1})
        assert legal_actions(1, state) == frozenset()

    def test_partial_invested_reduces_call_diff(self):
        """既に一部投資済みの場合、call_diff は差分で計算される。"""
        # call_amount=800, invested=600 → call_diff=200, stack=5000 → CALL/RAISE/FOLD
        state = _State(call_amount=800, invested={1: 600}, stacks={1: 5000})
        assert legal_actions(1, state) == frozenset({ActionType.CALL, ActionType.RAISE, ActionType.FOLD})

    def test_fully_invested_no_bet_facing(self):
        """投資額 == call_amount → call_diff=0 → {CHECK, BET}。"""
        state = _State(call_amount=400, invested={1: 400}, stacks={1: 3000})
        assert legal_actions(1, state) == frozenset({ActionType.CHECK, ActionType.BET})


# ――― validate_action ―――

class TestValidateAction:
    def test_legal_action_returns_true(self):
        state = _State(call_amount=0, invested={1: 0}, stacks={1: 5000})
        ok, msg = validate_action(1, ActionType.CHECK, state)
        assert ok is True
        assert msg == ""

    def test_illegal_action_returns_false_with_reason(self):
        state = _State(call_amount=0, invested={1: 0}, stacks={1: 5000})
        ok, msg = validate_action(1, ActionType.CALL, state)
        assert ok is False
        assert "call" in msg
        assert msg != ""

    def test_folded_seat_any_action_illegal(self):
        state = _State(call_amount=0, invested={1: 0}, stacks={1: 5000}, folded={1})
        ok, msg = validate_action(1, ActionType.CHECK, state)
        assert ok is False


# ――― detect_street_overflow ―――

class TestDetectStreetOverflow:
    def test_overflow_detected(self):
        assert detect_street_overflow(street_action_count=7, active_count=6) is True

    def test_no_overflow_equal(self):
        assert detect_street_overflow(street_action_count=6, active_count=6) is False

    def test_no_overflow_below(self):
        assert detect_street_overflow(street_action_count=3, active_count=6) is False


# ――― detect_fold_contradiction ―――

class TestDetectFoldContradiction:
    def test_already_folded_seat_is_contradiction(self):
        """フォールド済みの席が再びアクションしようとする → True（FR-25）。"""
        state = _State(call_amount=0, invested={1: 0}, stacks={1: 0}, folded={1})
        assert detect_fold_contradiction(1, "call", state) is True

    def test_active_seat_is_not_contradiction(self):
        """アクティブな席 → False。"""
        state = _State(call_amount=0, invested={1: 0}, stacks={1: 5000})
        assert detect_fold_contradiction(1, "fold", state) is False
