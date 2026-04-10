# tests/test_game_state.py
from core.game_state import GameState, GameStateManager, PlayerState, Street


# ――― GameStateManager（後方互換） ―――

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


# ――― GameState（spec.md v4.0 FR-05b–05h, FR-30–35） ―――

def _make_6p(button_seat: int = 3) -> GameState:
    players = [PlayerState(seat=i, name=f"P{i}", stack=10000) for i in range(1, 7)]
    return GameState(players=players, sb=100, bb=200, button_seat=button_seat)


class TestGameState:
    def test_position_map_6p(self):
        """6人テーブルで button_seat=3 のとき正しいポジションマップが返る（FR-05c）。"""
        gs = _make_6p(button_seat=3)
        assert gs.build_position_map() == {
            3: "BTN", 4: "SB", 5: "BB", 6: "UTG", 1: "HJ", 2: "CO"
        }

    def test_advance_button(self):
        """advance_button() で button_seat が次席に進む（FR-05b）。"""
        gs = _make_6p(button_seat=3)
        gs.advance_button()
        assert gs.button_seat == 4

    def test_advance_button_skips_busted(self):
        """バストアウト席をスキップして次のアクティブ席に進む（FR-05e）。"""
        gs = _make_6p(button_seat=3)
        gs.bust_out(4)
        gs.advance_button()
        assert gs.button_seat == 5

    def test_position_map_hu(self):
        """ヘッズアップ（2人）では button_seat が BTN/SB になる（FR-05c）。"""
        players = [
            PlayerState(seat=1, name="Alice", stack=10000),
            PlayerState(seat=2, name="Bob", stack=10000),
        ]
        gs = GameState(players=players, sb=100, bb=200, button_seat=1)
        assert gs.build_position_map() == {1: "BTN/SB", 2: "BB"}

    def test_turn_order_preflop(self):
        """プリフロップは UTG（ボタン左2席）から行動順が始まる（FR-05d）。"""
        gs = _make_6p(button_seat=3)
        # BTN=3, SB=4, BB=5, UTG=6 → UTG-first: [6,1,2,3,4,5]
        assert gs.build_turn_order(Street.PREFLOP) == [6, 1, 2, 3, 4, 5]

    def test_turn_order_flop(self):
        """ポストフロップは SB（ボタン左1席）から行動順が始まる（FR-05d）。"""
        gs = _make_6p(button_seat=3)
        # BTN=3, SB=4 → SB-first: [4,5,6,1,2,3]
        assert gs.build_turn_order(Street.FLOP) == [4, 5, 6, 1, 2, 3]
