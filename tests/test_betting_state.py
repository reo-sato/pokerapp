"""tests/test_betting_state.py

BettingState の lifecycle (ハンド開始時の自動 SB/BB post、ストリート遷移、
アクション適用後の actor 進行、round completion) を検証する。
"""
from __future__ import annotations

from integration.betting_state import BettingState


class TestHandStart:
    def test_blinds_are_auto_posted(self) -> None:
        bs = BettingState()
        bs.start_hand(button_seat=1, active_seats=[1, 2, 3, 4, 5, 6],
                      sb_amount=100, bb_amount=200)
        assert bs.is_initialized is True
        assert bs.sb_seat == 2
        assert bs.bb_seat == 3
        assert bs.current_bet == 200
        assert bs.is_opened is True
        assert bs.actor_seat == 4  # UTG
        # action_history に SB_POST / BB_POST が含まれる
        actions = [(a["seat"], a["action"], a["amount"]) for a in bs.action_history]
        assert (2, "SB_POST", 100) in actions
        assert (3, "BB_POST", 200) in actions
        # contrib に反映
        assert bs.player_contrib_this_street[2] == 100
        assert bs.player_contrib_this_street[3] == 200
        assert bs.player_contrib_hand[2] == 100
        assert bs.player_contrib_hand[3] == 200

    def test_heads_up_button_is_sb(self) -> None:
        bs = BettingState()
        bs.start_hand(button_seat=3, active_seats=[3, 7],
                      sb_amount=100, bb_amount=200)
        assert bs.sb_seat == 3
        assert bs.bb_seat == 7
        # preflop は SB/BTN が最初
        assert bs.actor_seat == 3

    def test_unresolved_button_marks_uninitialized(self) -> None:
        bs = BettingState()
        bs.start_hand(button_seat=99, active_seats=[1, 2, 3],
                      sb_amount=100, bb_amount=200)
        assert bs.is_initialized is False


class TestApplyAction:
    def test_call_matches_current_bet(self) -> None:
        bs = BettingState()
        bs.start_hand(button_seat=1, active_seats=[1, 2, 3, 4],
                      sb_amount=100, bb_amount=200)
        # actor=4 (UTG, button=1)
        assert bs.actor_seat == 4
        bs.apply_action(4, "CALL", 200)
        assert bs.player_contrib_this_street[4] == 200
        assert bs.actor_seat == 1  # button (BTN) が次

    def test_raise_updates_current_bet_and_aggressor(self) -> None:
        bs = BettingState()
        bs.start_hand(button_seat=1, active_seats=[1, 2, 3, 4, 5, 6],
                      sb_amount=100, bb_amount=200)
        bs.apply_action(4, "RAISE", 600)
        assert bs.current_bet == 600
        assert bs.last_aggressor == 4
        assert bs.is_opened is True

    def test_fold_adds_to_folded(self) -> None:
        bs = BettingState()
        bs.start_hand(button_seat=1, active_seats=[1, 2, 3, 4],
                      sb_amount=100, bb_amount=200)
        bs.apply_action(4, "FOLD")
        assert 4 in bs.folded_seats
        # 次は folded をスキップ
        assert bs.actor_seat == 1


class TestStreetReset:
    def test_flop_resets_contrib_and_actor(self) -> None:
        bs = BettingState()
        bs.start_hand(button_seat=1, active_seats=[1, 2, 3, 4, 5, 6],
                      sb_amount=100, bb_amount=200)
        bs.reset_for_new_street("flop")
        assert bs.current_bet == 0
        assert bs.is_opened is False
        assert bs.actor_seat == 2  # SB が最初
        assert all(v == 0 for v in bs.player_contrib_this_street.values())

    def test_flop_skips_folded_first_actor(self) -> None:
        bs = BettingState()
        bs.start_hand(button_seat=1, active_seats=[1, 2, 3, 4, 5, 6],
                      sb_amount=100, bb_amount=200)
        bs.folded_seats.add(2)
        bs.reset_for_new_street("flop")
        assert bs.actor_seat == 3


class TestRoundCompletion:
    def test_round_not_complete_after_partial_call(self) -> None:
        bs = BettingState()
        bs.start_hand(button_seat=1, active_seats=[1, 2, 3],
                      sb_amount=100, bb_amount=200)
        bs.reset_for_new_street("flop")
        bs.apply_action(2, "BET", 500)
        assert bs.is_round_complete() is False

    def test_round_complete_when_all_matched(self) -> None:
        bs = BettingState()
        bs.start_hand(button_seat=1, active_seats=[1, 2, 3],
                      sb_amount=100, bb_amount=200)
        bs.reset_for_new_street("flop")
        bs.apply_action(2, "CHECK")
        bs.apply_action(3, "CHECK")
        bs.apply_action(1, "CHECK")
        assert bs.is_round_complete() is True

    def test_round_complete_with_fold(self) -> None:
        bs = BettingState()
        bs.start_hand(button_seat=1, active_seats=[1, 2, 3],
                      sb_amount=100, bb_amount=200)
        bs.reset_for_new_street("flop")
        bs.apply_action(2, "BET", 300)
        bs.apply_action(3, "FOLD")
        bs.apply_action(1, "CALL", 300)
        assert bs.is_round_complete() is True
