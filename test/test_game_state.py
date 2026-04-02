# tests/test_game_state.py
from core.game_state import GameStateManager, PlayerState, Street

def make_game():
    players = [
        PlayerState(seat=1, name="Sato", stack=10000),
        PlayerState(seat=2, name="Yamada", stack=10000),
        PlayerState(seat=3, name="Tanaka", stack=10000),
    ]
    return GameStateManager(players=players, sb=100, bb=200)

def test_new_hand_resets_state():
    game = make_game()
    hand_id = game.new_hand()
    assert hand_id == 1
    assert game.pot == 0
    assert game.street == Street.PREFLOP.value
    assert set(game.get_active_seats()) == {1, 2, 3}

def test_apply_action_bet_and_pot():
    game = make_game()
    game.new_hand()
    seat = game.get_current_player()
    game.apply_action(seat=seat, action="bet", amount=500)
    assert game.get_stack(seat) == 9500
    assert game.pot == 500

def test_fold_removes_player():
    game = make_game()
    game.new_hand()
    first = game.get_current_player()
    game.apply_action(seat=first, action="fold")
    assert first not in game.get_active_seats()
