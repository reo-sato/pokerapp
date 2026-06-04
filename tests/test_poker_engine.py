"""tests/test_poker_engine.py

R2 (ADR-0009): PokerkitGameState（pokerkit を live 権威にした GameStateManager 互換）と factory。
pokerkit 未導入環境では pokerkit 依存テストを skip する。
"""
from __future__ import annotations

import pytest

from core.game_state import GameStateManager, PlayerState
from core.poker_engine import create_game_state


def _players(stacks=(10000, 10000, 10000)) -> list[PlayerState]:
    return [PlayerState(seat=i + 1, name=f"P{i + 1}", stack=s) for i, s in enumerate(stacks)]


def test_factory_legacy_is_default():
    assert isinstance(create_game_state("legacy", _players(), 100, 200), GameStateManager)
    assert isinstance(create_game_state("unknown", _players(), 100, 200), GameStateManager)


@pytest.fixture
def pk():
    pytest.importorskip("pokerkit")
    from core.poker_engine import PokerkitGameState

    return PokerkitGameState(_players(), 100, 200)


def test_factory_pokerkit():
    pytest.importorskip("pokerkit")
    from core.poker_engine import PokerkitGameState

    assert isinstance(create_game_state("pokerkit", _players(), 100, 200), PokerkitGameState)


def test_actor_order_and_legal_context(pk):
    pk.new_hand()
    # 3-handed: seat1=SB, seat2=BB, seat3=UTG(button) が preflop 先頭（pokerkit のポジション順）
    assert pk.get_current_player() == 3
    ctx = pk.legal_context()
    assert ctx.actor_seat == 3
    assert ctx.amount_to_call == 200
    assert ctx.min_raise == 400
    assert "fold" in ctx.legal_actions
    assert "call" in ctx.legal_actions
    assert "raise" in ctx.legal_actions


def test_street_auto_advances_on_betting_completion(pk):
    pk.new_hand()
    assert pk.street == "preflop"
    pk.apply_action(3, "raise", 600)
    pk.apply_action(1, "call")
    pk.apply_action(2, "call")
    assert pk.street == "flop"
    assert pk.get_current_player() == 1  # SB が postflop 先頭


def test_illegal_raise_raises_value_error(pk):
    pk.new_hand()
    with pytest.raises(ValueError):
        pk.apply_action(3, "raise", 150)  # min 400 未満


def test_apply_action_by_non_actor_raises(pk):
    pk.new_hand()
    with pytest.raises(ValueError):
        pk.apply_action(1, "call")  # seat1 は手番でない（seat3 が手番）


def test_end_hand_awards_pot_to_announced_winner(pk):
    pk.new_hand()
    pk.apply_action(3, "fold")
    pk.apply_action(1, "fold")  # SB fold -> BB(seat2) が勝者
    assert pk.pot == 300        # blinds 100 + 200
    pk.end_hand(2)
    stacks = pk.get_stacks()
    assert stacks[2] == 10100   # BB: -200 +300 = +100
    assert stacks[1] == 9900    # SB: -100
    assert stacks[3] == 10000   # UTG: 不変


def test_active_seats_after_fold(pk):
    pk.new_hand()
    pk.apply_action(3, "fold")
    assert set(pk.get_active_seats()) == {1, 2}


def test_side_pots_on_unequal_allin():
    pytest.importorskip("pokerkit")
    from core.poker_engine import PokerkitGameState

    gs = PokerkitGameState(_players((300, 1000, 5000)), 100, 200)
    gs.new_hand()
    gs.apply_action(3, "allin")  # UTG 5000 all-in
    gs.apply_action(1, "call")   # SB 300 all-in
    gs.apply_action(2, "call")   # BB 1000 all-in
    gs.end_hand(2)
    pots = gs.pots()
    assert sum(p["amount"] for p in pots) == 2300  # main 900 + side 1400
    assert len(pots) == 2


def test_rebuy_between_hands(pk):
    pk.new_hand()
    pk.apply_action(3, "fold")
    pk.apply_action(1, "fold")
    pk.end_hand(2)
    pk.rebuy(3, 5000)
    assert pk.get_stacks()[3] == 15000
    pk.new_hand()
    assert pk.get_stacks()[3] == 15000  # 次ハンドに反映


def test_allin_short_stack_calls_all_in():
    """レイズできないショートスタックの 'allin' は call-all-in として処理され、state が前進する（desync しない）。"""
    pytest.importorskip("pokerkit")
    from core.poker_engine import PokerkitGameState

    # seat1(SB) はスタック 150（SB 100 を post 済で 50 behind）。大きな raise に対し call-all-in のみ可。
    gs = PokerkitGameState(_players((150, 10000, 10000)), 100, 200)
    gs.new_hand()
    gs.apply_action(3, "raise", 1000)        # UTG(seat3) raises to 1000
    assert gs.get_current_player() == 1      # 手番は SB
    ctx = gs.legal_context()
    assert "raise" not in ctx.legal_actions  # SB は raise 不可
    gs.apply_action(1, "allin")              # 旧実装ではここで ValueError → desync。修正後は call-all-in。
    assert gs.get_stacks()[1] == 0           # SB all-in
    assert gs.get_current_player() == 2      # 手番は BB（state が正しく前進）
