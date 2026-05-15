"""tests/test_betting_state_hand.py

BettingState の start_hand() / button / blind 関連の lifecycle テスト。
(既存の tests/test_action_inference.py には触れず、独立して追加する)
"""
from __future__ import annotations

from integration.action_inference import BettingState


class TestStartHand:
    def test_6max_button_1(self) -> None:
        bs = BettingState()
        bs.start_hand(button_seat=1, active_seats=[1, 2, 3, 4, 5, 6],
                      sb_amount=100, bb_amount=200)
        assert bs.is_initialized
        assert bs.button_seat == 1
        assert bs.sb_seat == 2
        assert bs.bb_seat == 3
        assert bs.actor_seat == 4  # UTG
        assert bs.current_bet == 200
        assert bs.is_opened is True
        # action history
        labels = [(a["seat"], a["action"], a["amount"]) for a in bs.action_history]
        assert (2, "SB_POST", 100) in labels
        assert (3, "BB_POST", 200) in labels
        # contributions
        assert bs.player_contrib_this_street[2] == 100
        assert bs.player_contrib_this_street[3] == 200
        assert bs.player_contrib_hand[2] == 100
        assert bs.player_contrib_hand[3] == 200

    def test_sparse_seats(self) -> None:
        bs = BettingState()
        bs.start_hand(button_seat=8, active_seats=[2, 4, 6, 8],
                      sb_amount=100, bb_amount=200)
        assert bs.sb_seat == 2
        assert bs.bb_seat == 4
        assert bs.actor_seat == 6

    def test_heads_up_button_is_sb(self) -> None:
        bs = BettingState()
        bs.start_hand(button_seat=3, active_seats=[3, 7],
                      sb_amount=100, bb_amount=200)
        assert bs.sb_seat == 3
        assert bs.bb_seat == 7
        # preflop は BTN(=SB) が最初
        assert bs.actor_seat == 3

    def test_button_not_active_is_uninitialized(self) -> None:
        bs = BettingState()
        bs.start_hand(button_seat=9, active_seats=[1, 2, 3],
                      sb_amount=100, bb_amount=200)
        assert bs.is_initialized is False


class TestActorAdvance:
    def test_update_after_action_advances_actor(self) -> None:
        bs = BettingState()
        bs.start_hand(button_seat=1, active_seats=[1, 2, 3, 4, 5, 6],
                      sb_amount=100, bb_amount=200)
        assert bs.actor_seat == 4
        bs.update_after_action(4, "call", 200)
        assert bs.actor_seat == 5
        bs.update_after_action(5, "fold", 0)
        assert bs.actor_seat == 6
        # seat 5 が folded リストに入る
        assert 5 in bs.folded_seats

    def test_raise_updates_current_bet_and_aggressor(self) -> None:
        bs = BettingState()
        bs.start_hand(button_seat=1, active_seats=[1, 2, 3, 4, 5, 6],
                      sb_amount=100, bb_amount=200)
        bs.update_after_action(4, "raise", 600)
        assert bs.current_bet == 600
        assert bs.last_aggressor == 4
        assert bs.player_contrib_this_street[4] == 600


class TestStreetReset:
    def test_flop_resets_and_first_actor_is_sb(self) -> None:
        bs = BettingState()
        bs.start_hand(button_seat=1, active_seats=[1, 2, 3, 4, 5, 6],
                      sb_amount=100, bb_amount=200)
        bs.reset_for_new_street()
        bs.street = "flop"
        # reset_for_new_street() が button_seat を使って postflop first actor を再計算する
        bs.reset_for_new_street()
        assert bs.current_bet == 0
        assert bs.is_opened is False
        assert bs.actor_seat == 2  # SB が live で最初

    def test_flop_skips_folded_first_actor(self) -> None:
        bs = BettingState()
        bs.start_hand(button_seat=1, active_seats=[1, 2, 3, 4, 5, 6],
                      sb_amount=100, bb_amount=200)
        bs.folded_seats.append(2)
        bs.reset_for_new_street()
        assert bs.actor_seat == 3
