"""tests/test_action_inference.py

BettingState / InferredAction / infer_action() のユニットテスト。
"""
from __future__ import annotations

import time

import pytest

from core.events import AudioEvent
from integration.action_inference import (
    BettingState,
    InferredAction,
    infer_action,
)


# ――― ヘルパー ―――

def _event(action: str, amount: int = 0, raw_text: str = "") -> AudioEvent:
    return AudioEvent(action=action, amount=amount, timestamp=time.time(), raw_text=raw_text or action)


def _amount_only(amount: int, raw_text: str = "") -> AudioEvent:
    return AudioEvent(
        action="amount_only",
        amount=amount,
        timestamp=time.time(),
        raw_text=raw_text or str(amount),
    )


def _fresh_state(**kwargs) -> BettingState:
    s = BettingState()
    for k, v in kwargs.items():
        setattr(s, k, v)
    return s


# ――― BettingState ―――

class TestBettingState:
    def test_initial_state(self) -> None:
        s = BettingState()
        assert s.street == "preflop"
        assert s.current_bet == 0
        assert not s.is_opened
        assert s.last_raise_to == 0

    def test_get_contrib_missing(self) -> None:
        s = BettingState()
        assert s.get_contrib(1) == 0

    def test_update_bet(self) -> None:
        s = BettingState()
        s.update_after_action(1, "bet", 600)
        assert s.current_bet == 600
        assert s.is_opened
        assert s.last_raise_to == 600
        assert s.get_contrib(1) == 600

    def test_update_raise_accumulates_contrib(self) -> None:
        s = BettingState()
        s.update_after_action(1, "bet", 600)
        s.update_after_action(2, "raise", 1800)
        assert s.current_bet == 1800
        assert s.get_contrib(2) == 1800

    def test_update_call(self) -> None:
        s = BettingState()
        s.update_after_action(1, "bet", 600)
        s.update_after_action(2, "call", 600)
        assert s.get_contrib(2) == 600

    def test_update_fold(self) -> None:
        s = BettingState()
        s.update_after_action(3, "fold", 0)
        assert 3 in s.folded_seats
        s.update_after_action(3, "fold", 0)  # 二重追加しない
        assert s.folded_seats.count(3) == 1

    def test_reset_for_new_street(self) -> None:
        s = BettingState()
        s.update_after_action(1, "bet", 800)
        s.reset_for_new_street()
        assert s.current_bet == 0
        assert not s.is_opened
        assert s.last_raise_to == 0
        assert s.player_contrib_this_street == {}

    def test_reset_for_new_hand(self) -> None:
        s = BettingState()
        s.street = "river"
        s.update_after_action(1, "bet", 500)
        s.update_after_action(2, "fold", 0)
        s.reset_for_new_hand()
        assert s.street == "preflop"
        assert not s.is_opened
        assert s.folded_seats == []

    def test_update_allin_sets_opened(self) -> None:
        s = BettingState()
        s.update_after_action(1, "allin", 5000)
        assert s.is_opened
        assert s.current_bet == 5000


# ――― amount_only 推定 ―――

class TestInferAmountOnly:
    """action="amount_only" 時のゲームステート推定ロジック。"""

    def test_case_a_opening_bet(self) -> None:
        """ポットが開いていない → BET と推定。"""
        state = BettingState()  # is_opened=False
        result = infer_action(_amount_only(600, "600"), state, actor_seat=1)
        assert result.action == "bet"
        assert result.amount == 600
        assert result.reason == "amount_only_opening_bet"
        assert not result.needs_review

    def test_case_b_exact_call(self) -> None:
        """amount == current_bet かつ contrib=0 → CALL と推定。"""
        state = BettingState()
        state.update_after_action(2, "bet", 600)
        result = infer_action(_amount_only(600, "600"), state, actor_seat=1)
        assert result.action == "call"
        assert result.amount == 600
        assert result.reason == "amount_only_exact_call"
        assert not result.needs_review

    def test_case_c_raise_above_call_no_contrib(self) -> None:
        """amount > current_bet かつ contrib=0 → RAISE と推定。"""
        state = BettingState()
        state.update_after_action(2, "bet", 600)
        result = infer_action(_amount_only(1800, "1800"), state, actor_seat=1)
        assert result.action == "raise"
        assert result.amount == 1800
        assert result.reason == "amount_only_above_call"
        assert not result.needs_review

    def test_case_e_reraise_with_contrib(self) -> None:
        """amount > current_bet かつ contrib>0 → RAISE (re-raise) と推定。"""
        state = BettingState()
        state.update_after_action(1, "bet", 600)
        state.update_after_action(2, "raise", 1800)
        # seat=1 が再び行動: contrib=600, current_bet=1800
        result = infer_action(_amount_only(3600, "3600"), state, actor_seat=1)
        assert result.action == "raise"
        assert result.reason == "amount_only_reraise"
        assert not result.needs_review

    def test_case_d_below_call_needs_review(self) -> None:
        """amount < current_bet → 曖昧、needs_review=True。"""
        state = BettingState()
        state.update_after_action(2, "bet", 600)
        result = infer_action(_amount_only(300, "300"), state, actor_seat=1)
        assert result.action is None
        assert result.needs_review
        assert result.reason == "amount_only_below_call"

    def test_amount_zero_needs_review(self) -> None:
        """amount=0 の amount_only → 推定不能。"""
        state = BettingState()
        result = infer_action(_amount_only(0, ""), state, actor_seat=1)
        assert result.action is None
        assert result.needs_review
        assert result.reason == "amount_only_no_amount"

    def test_unknown_actor_seat(self) -> None:
        """actor_seat=None でも amount_only_opening_bet が推定される。"""
        state = BettingState()
        result = infer_action(_amount_only(800), state, actor_seat=None)
        assert result.action == "bet"
        assert result.seat is None

    def test_confidence_range(self) -> None:
        """信頼度は 0.0〜1.0 の範囲内。"""
        state = BettingState()
        r = infer_action(_amount_only(1000), state, actor_seat=1)
        assert 0.0 <= r.confidence <= 1.0


# ――― 明示アクションの妥当性検証 ―――

class TestValidateProvidedAction:
    """action が明示されている場合のゲームステートとの整合性チェック。"""

    def test_check_when_not_opened_accepted(self) -> None:
        state = BettingState()  # is_opened=False
        result = infer_action(_event("check", 0, "チェック"), state, actor_seat=1)
        assert result.action == "check"
        assert not result.needs_review
        assert result.reason == "action_accepted"

    def test_check_when_opened_needs_review(self) -> None:
        state = BettingState()
        state.update_after_action(2, "bet", 600)
        result = infer_action(_event("check", 0, "チェック"), state, actor_seat=1)
        assert result.action == "check"
        assert result.needs_review
        assert result.reason == "check_when_bet_open"

    def test_bet_when_not_opened_accepted(self) -> None:
        state = BettingState()
        result = infer_action(_event("bet", 800, "ベット800"), state, actor_seat=1)
        assert result.action == "bet"
        assert not result.needs_review

    def test_bet_when_already_opened_accepted(self) -> None:
        """ラウンド境界を音声だけでは確実に検出できないため、bet-after-bet は accepts される。"""
        state = BettingState()
        state.update_after_action(2, "bet", 600)
        result = infer_action(_event("bet", 400, "ベット400"), state, actor_seat=1)
        assert result.action == "bet"
        assert not result.needs_review

    def test_call_accepted_when_opened_no_contrib(self) -> None:
        state = BettingState()
        state.update_after_action(2, "bet", 600)
        result = infer_action(_event("call", 600, "コール600"), state, actor_seat=1)
        assert result.action == "call"
        assert not result.needs_review

    def test_call_accepted_even_when_already_equal(self) -> None:
        """ラウンド境界不明のため、call-when-already-equal は accepts される。"""
        state = BettingState()
        state.update_after_action(1, "bet", 600)
        state.update_after_action(2, "call", 600)
        result = infer_action(_event("call", 600, "コール600"), state, actor_seat=2)
        assert not result.needs_review

    def test_raise_accepted(self) -> None:
        state = BettingState()
        state.update_after_action(2, "bet", 600)
        result = infer_action(_event("raise", 1800, "レイズ1800"), state, actor_seat=1)
        assert result.action == "raise"
        assert not result.needs_review

    def test_fold_accepted(self) -> None:
        state = BettingState()
        result = infer_action(_event("fold", 0, "フォールド"), state, actor_seat=1)
        assert result.action == "fold"
        assert not result.needs_review

    def test_allin_accepted(self) -> None:
        state = BettingState()
        result = infer_action(_event("allin", 5000, "オールイン"), state, actor_seat=1)
        assert result.action == "allin"
        assert not result.needs_review

    def test_provided_action_confidence_is_one(self) -> None:
        """明示アクションの confidence は 1.0（センサー信頼度とは別概念）。"""
        state = BettingState()
        r = infer_action(_event("fold", 0), state, actor_seat=2)
        assert r.confidence == 1.0


# ――― infer_action ディスパッチ ―――

class TestInferActionDispatch:
    """action="amount_only" か通常アクションかで正しく振り分けられることを確認。"""

    def test_amount_only_action_dispatches(self) -> None:
        state = BettingState()
        event = AudioEvent(action="amount_only", amount=1000, timestamp=time.time(), raw_text="1000")
        result = infer_action(event, state, actor_seat=1)
        assert result.action in ("bet", "call", "raise", None)

    def test_normal_action_dispatches(self) -> None:
        state = BettingState()
        event = AudioEvent(action="fold", amount=0, timestamp=time.time(), raw_text="フォールド")
        result = infer_action(event, state, actor_seat=1)
        assert result.action == "fold"
        assert not result.needs_review

    def test_raw_text_preserved(self) -> None:
        state = BettingState()
        raw = "ベット ろくひゃく"
        event = AudioEvent(action="bet", amount=600, timestamp=time.time(), raw_text=raw)
        result = infer_action(event, state, actor_seat=1)
        assert result.raw_text == raw

    def test_seat_preserved(self) -> None:
        state = BettingState()
        result = infer_action(_amount_only(500), state, actor_seat=3)
        assert result.seat == 3


# ――― BettingState 更新の連鎖 ―――

class TestBettingStateIntegration:
    """複数アクション後の BettingState が正しく推定に反映される。"""

    def test_sequence_bet_call_raise(self) -> None:
        state = BettingState()

        # seat1 が BET 600 を宣言
        r1 = infer_action(_amount_only(600, "600"), state, actor_seat=1)
        assert r1.action == "bet"
        state.update_after_action(1, r1.action, r1.amount)

        # seat2 がコール宣言 (600 と同額)
        r2 = infer_action(_amount_only(600, "600"), state, actor_seat=2)
        assert r2.action == "call"
        state.update_after_action(2, r2.action, r2.amount)

        # seat3 がレイズ 1800 宣言
        r3 = infer_action(_amount_only(1800, "1800"), state, actor_seat=3)
        assert r3.action == "raise"
        state.update_after_action(3, r3.action, r3.amount)

        assert state.current_bet == 1800
        assert state.get_contrib(3) == 1800

    def test_new_street_resets_contrib(self) -> None:
        state = BettingState()
        state.update_after_action(1, "bet", 600)
        state.update_after_action(2, "call", 600)
        state.reset_for_new_street()
        state.street = "flop"

        # フロップで seat2 が 600 をアナウンス → 新ストリートなので BET と推定
        r = infer_action(_amount_only(600, "600"), state, actor_seat=2)
        assert r.action == "bet"
        assert r.reason == "amount_only_opening_bet"
