"""デッドボタン（オーナー 2026-10-28）: SB か BB の人が抜けた（バスト・空席）ときだけ。

- SB の人が抜けた → 空いた SB の席にボタン（デッドボタン）。SB = 前の BB、BB = その次の席。
- BB の人が抜けた → ボタンは動かさない。SB = 前の SB のまま、BB = 抜けた BB の次の席。

10/26 のハンド 14 → 15: 席 2（SB）が抜けて、ボタンは空いた席 2・SB 3・BB 4（真のアクションのメモ「デッドボタン」）。
"""
from __future__ import annotations

import pytest

from core.positions import next_hand_button, position_map, seat_order_from_button

pytest.importorskip("pokerkit")

from core.game_state import PlayerState  # noqa: E402
from core.poker_engine import PokerkitGameState  # noqa: E402
from core.truth_hand import start_state  # noqa: E402

SEATS = [2, 3, 4, 5, 7, 8]


class TestRule:
    def test_normal_rotation(self):
        assert next_hand_button(SEATS, 8, SEATS) == 2

    def test_sb_busted_is_a_dead_button(self):
        # ボタン 8 → SB 2・BB 3。SB の 2 が抜けた
        assert next_hand_button(SEATS, 8, [3, 4, 5, 7, 8]) == 2

    def test_bb_busted_keeps_the_button(self):
        # ボタン 8 → SB 2・BB 3。BB の 3 が抜けた → ボタン 8 のまま（SB 2・BB 4）
        assert next_hand_button(SEATS, 8, [2, 4, 5, 7, 8]) == 8

    def test_both_or_others_rotate(self):
        assert next_hand_button(SEATS, 8, [4, 5, 7, 8]) == 4        # SB・BB とも抜けた
        assert next_hand_button(SEATS, 8, [2, 3, 4, 7, 8]) == 2     # ほかの人が抜けた
        assert next_hand_button([2, 3, 8], 8, [3, 8]) == 3           # 次が heads-up は使わない

    def test_order_and_names_with_a_dead_button(self):
        assert seat_order_from_button([3, 4, 5, 7, 8], 2) == [3, 4, 5, 7, 8]
        assert seat_order_from_button([2, 4, 5, 8], 6) == [8, 2, 4, 5]
        assert position_map([3, 4, 5, 7, 8], 2) == {3: "SB", 4: "BB", 5: "UTG", 7: "HJ", 8: "CO"}


def _engine() -> PokerkitGameState:
    gs = PokerkitGameState([PlayerState(seat=s, name=f"P{s}", stack=10000) for s in SEATS], 500, 1000,
                           stackless=True)
    gs.set_button(8)
    gs.new_hand()
    return gs


class TestEngine:
    def test_the_sb_busts_and_the_button_is_dead(self):
        gs = _engine()
        assert gs.position_map()[2] == "SB" and gs.position_map()[3] == "BB"
        gs.end_hand(3)
        gs.sit_out(2)                                   # 席 2 は空席に（スタックが 0 になって抜けた）
        gs.new_hand()
        assert gs.button_seat == 2 and 2 not in gs.position_map()
        assert gs.position_map() == {3: "SB", 4: "BB", 5: "UTG", 7: "HJ", 8: "CO"}
        assert gs.legal_context().actor_seat == 5
        gs.end_hand(4)
        gs.new_hand()                                   # その次は普通に回る（前の SB の 3 がボタン）
        assert gs.button_seat == 3 and gs.position_map()[4] == "SB"

    def test_the_bb_busts_and_the_button_stays(self):
        gs = _engine()
        gs.end_hand(2)
        gs.sit_out(3)                                   # BB の 3 が抜けた
        gs.new_hand()
        assert gs.button_seat == 8
        assert gs.position_map()[2] == "SB" and gs.position_map()[4] == "BB"


def test_the_truth_replay_takes_a_button_on_an_empty_seat():
    """真のアクションの入力で空いた席をボタンに選べる（デッドボタン）。"""
    captured = {"players": [{"seat": s, "stack_start": 10000} for s in (3, 4, 5, 7, 8)],
                "blinds": {"sb": 500, "bb": 1000}, "button_seat": 8}
    gs = start_state(captured, 2)
    assert gs.button_seat == 2 and gs.position_map()[3] == "SB" and gs.legal_context().actor_seat == 5
