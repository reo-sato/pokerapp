from __future__ import annotations

import pytest

from core.game_state import GameStateManager, PlayerState, Street


def make_players(seats: list[int], stack: int = 10000) -> list[PlayerState]:
    return [PlayerState(seat=s, name=f"Player{s}", stack=stack) for s in seats]


@pytest.fixture
def gsm() -> GameStateManager:
    """3人テーブル (seat 1,2,3), SB=100, BB=200"""
    players = make_players([1, 2, 3])
    return GameStateManager(players=players, sb=100, bb=200)


class TestNewHand:
    def test_hand_id_increments(self, gsm: GameStateManager):
        assert gsm.new_hand() == 1
        assert gsm.new_hand() == 2
        assert gsm.new_hand() == 3

    def test_pot_reset(self, gsm: GameStateManager):
        gsm.new_hand()
        gsm.apply_action(1, "bet", 500)
        assert gsm.pot == 500
        gsm.new_hand()
        assert gsm.pot == 0

    def test_street_reset(self, gsm: GameStateManager):
        gsm.new_hand()
        gsm.advance_street(Street.FLOP)
        assert gsm.street == "flop"
        gsm.new_hand()
        assert gsm.street == "preflop"

    def test_all_seats_active_after_new_hand(self, gsm: GameStateManager):
        gsm.new_hand()
        gsm.apply_action(1, "fold")
        assert 1 not in gsm.get_active_seats()
        gsm.new_hand()
        assert 1 in gsm.get_active_seats()


class TestAdvanceStreet:
    def test_preflop_to_flop(self, gsm: GameStateManager):
        gsm.new_hand()
        gsm.advance_street(Street.FLOP)
        assert gsm.street == "flop"

    def test_street_sequence(self, gsm: GameStateManager):
        gsm.new_hand()
        for street in (Street.FLOP, Street.TURN, Street.RIVER, Street.SHOWDOWN):
            gsm.advance_street(street)
            assert gsm.street == street.value

    def test_backward_street_raises(self, gsm: GameStateManager):
        """逆方向のストリート遷移は ValueError を送出する。"""
        gsm.new_hand()
        gsm.advance_street(Street.TURN)
        with pytest.raises(ValueError, match="Invalid street transition"):
            gsm.advance_street(Street.FLOP)

    def test_same_street_raises(self, gsm: GameStateManager):
        """同一ストリートへの遷移は ValueError を送出する。"""
        gsm.new_hand()
        gsm.advance_street(Street.FLOP)
        with pytest.raises(ValueError, match="Invalid street transition"):
            gsm.advance_street(Street.FLOP)


class TestEndHand:
    def test_winner_receives_pot(self, gsm: GameStateManager):
        gsm.new_hand()
        gsm.apply_action(1, "bet", 500)
        gsm.apply_action(2, "call", 500)
        assert gsm.pot == 1000
        gsm.end_hand(winner_seat=1)
        assert gsm.get_stack(1) == 10500  # 10000 - 500 + 1000
        assert gsm.pot == 0

    def test_unknown_winner_raises(self, gsm: GameStateManager):
        gsm.new_hand()
        with pytest.raises(ValueError):
            gsm.end_hand(winner_seat=99)


class TestApplyAction:
    def test_bet_reduces_stack(self, gsm: GameStateManager):
        gsm.new_hand()
        gsm.apply_action(1, "bet", 300)
        assert gsm.get_stack(1) == 9700
        assert gsm.pot == 300

    def test_raise_adds_to_pot(self, gsm: GameStateManager):
        gsm.new_hand()
        gsm.apply_action(1, "bet", 200)
        gsm.apply_action(2, "raise", 600)
        assert gsm.pot == 800

    def test_call(self, gsm: GameStateManager):
        gsm.new_hand()
        gsm.apply_action(1, "bet", 400)
        gsm.apply_action(2, "call", 400)
        assert gsm.get_stack(2) == 9600
        assert gsm.pot == 800

    def test_check_no_change(self, gsm: GameStateManager):
        gsm.new_hand()
        stack_before = gsm.get_stack(1)
        gsm.apply_action(1, "check")
        assert gsm.get_stack(1) == stack_before
        assert gsm.pot == 0

    def test_fold_deactivates_seat(self, gsm: GameStateManager):
        gsm.new_hand()
        gsm.apply_action(1, "fold")
        assert 1 not in gsm.get_active_seats()

    def test_allin(self, gsm: GameStateManager):
        gsm.new_hand()
        gsm.apply_action(1, "allin", 10000)
        assert gsm.get_stack(1) == 0
        assert gsm.pot == 10000

    def test_allin_capped_at_stack(self, gsm: GameStateManager):
        """スタック超過のオールインはスタック分だけ移動する。"""
        gsm.new_hand()
        gsm.apply_action(1, "allin", 99999)
        assert gsm.get_stack(1) == 0
        assert gsm.pot == 10000

    def test_unknown_seat_raises(self, gsm: GameStateManager):
        gsm.new_hand()
        with pytest.raises(ValueError):
            gsm.apply_action(99, "bet", 100)

    def test_negative_amount_raises(self, gsm: GameStateManager):
        gsm.new_hand()
        with pytest.raises(ValueError):
            gsm.apply_action(1, "bet", -100)

    def test_unknown_action_raises(self, gsm: GameStateManager):
        gsm.new_hand()
        with pytest.raises(ValueError, match="Unknown action"):
            gsm.apply_action(1, "shove", 500)

    def test_folded_seat_cannot_act(self, gsm: GameStateManager):
        """fold 済みプレイヤーへの再アクションは ValueError を送出する。"""
        gsm.new_hand()
        gsm.apply_action(1, "fold")
        with pytest.raises(ValueError, match="already folded"):
            gsm.apply_action(1, "check")

    def test_bet_advances_turn(self, gsm: GameStateManager):
        """apply_action(bet) の後、ターンが次のプレイヤーに進む。"""
        gsm.new_hand()
        first = gsm.get_current_player()
        gsm.apply_action(first, "bet", 200)
        second = gsm.get_current_player()
        assert second != first

    def test_check_advances_turn(self, gsm: GameStateManager):
        """apply_action(check) の後、ターンが次のプレイヤーに進む。"""
        gsm.new_hand()
        first = gsm.get_current_player()
        gsm.apply_action(first, "check")
        second = gsm.get_current_player()
        assert second != first

    def test_fold_advances_turn(self, gsm: GameStateManager):
        """apply_action(fold) の後、ターンが fold した席以外のプレイヤーに進む。"""
        gsm.new_hand()
        first = gsm.get_current_player()
        gsm.apply_action(first, "fold")
        next_player = gsm.get_current_player()
        assert next_player != first


class TestTurnManagement:
    def test_get_current_player(self, gsm: GameStateManager):
        gsm.new_hand()
        seat = gsm.get_current_player()
        assert seat in [1, 2, 3]

    def test_advance_turn_cycles(self, gsm: GameStateManager):
        gsm.new_hand()
        seen = set()
        for _ in range(3):
            seen.add(gsm.get_current_player())
            gsm.advance_turn()
        assert seen == {1, 2, 3}

    def test_advance_turn_skips_folded(self, gsm: GameStateManager):
        gsm.new_hand()
        # seat 1 がフォールドしたらターン候補から外れる
        gsm.apply_action(1, "fold")
        for _ in range(6):
            seat = gsm.get_current_player()
            assert seat != 1
            gsm.advance_turn()

    def test_no_active_seats_raises(self, gsm: GameStateManager):
        gsm.new_hand()
        gsm.apply_action(1, "fold")
        gsm.apply_action(2, "fold")
        gsm.apply_action(3, "fold")
        with pytest.raises(RuntimeError):
            gsm.get_current_player()


class TestGetStacks:
    def test_returns_all_seats(self, gsm: GameStateManager):
        stacks = gsm.get_stacks()
        assert set(stacks.keys()) == {1, 2, 3}

    def test_initial_stacks(self, gsm: GameStateManager):
        stacks = gsm.get_stacks()
        assert all(v == 10000 for v in stacks.values())


class TestManualCorrections:
    def test_update_stack(self, gsm: GameStateManager):
        gsm.update_stack(1, 5000)
        assert gsm.get_stack(1) == 5000

    def test_update_stack_negative_raises(self, gsm: GameStateManager):
        with pytest.raises(ValueError):
            gsm.update_stack(1, -1)

    def test_rebuy(self, gsm: GameStateManager):
        gsm.new_hand()
        gsm.apply_action(1, "allin", 10000)
        gsm.rebuy(1, 10000)
        assert gsm.get_stack(1) == 10000

    def test_rebuy_zero_raises(self, gsm: GameStateManager):
        with pytest.raises(ValueError):
            gsm.rebuy(1, 0)


class TestEmptyPlayersRaises:
    def test_empty_players(self):
        with pytest.raises(ValueError):
            GameStateManager(players=[], sb=100, bb=200)
