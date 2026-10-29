"""デッドボタン（オーナー 2026-10-28）とデッドスモール（2026-10-29）: SB か BB の人が抜けた（バスト・空席）ときだけ。

- SB の人が抜けた → 空いた SB の席にボタン（デッドボタン）。SB = 前の BB、BB = その次の席。
- BB の人が抜けた → BB は次の席へ、SB は空き（デッドスモール = シングルビッグブラインド）、ボタンは前の SB の席。
  その次のハンドは空いた SB の席にボタン（デッドボタン）。heads-up になるときはボタンを動かさない。

10/26 のハンド 14 → 15: 席 2（SB）が抜けて、ボタンは空いた席 2・SB 3・BB 4（真のアクションのメモ「デッドボタン」）。
"""
from __future__ import annotations

import pytest

from core.positions import next_hand_button, next_hand_seating, position_map, seat_order_from_button

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

    def test_bb_busted_is_a_dead_small_blind(self):
        # ボタン 8 → SB 2・BB 3。BB の 3 が抜けた → ボタン 2・SB なし（空いた 3）・BB 4
        assert next_hand_seating(SEATS, 8, [2, 4, 5, 7, 8]) == (2, 3)
        # その次は空いた SB の席にボタン（デッドボタン）
        assert next_hand_seating([2, 4, 5, 7, 8], 2, [2, 4, 5, 7, 8], prev_dead_sb=3) == (3, None)

    def test_bb_busted_into_heads_up_keeps_the_button(self):
        # 10/29 ハンド 116 → 117: ボタン 5・SB 7・BB 4、BB の 4 が抜けて heads-up → ボタン 5 のまま
        assert next_hand_seating([4, 5, 7], 5, [5, 7]) == (5, None)

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

    def test_the_bb_busts_and_the_small_blind_is_dead(self):
        """10/29 ハンド 66→67→68→69（メモ「BB は single bigblind 7」）。"""
        seats = [1, 2, 4, 5, 6, 7]
        gs = PokerkitGameState([PlayerState(seat=s, name=f"P{s}", stack=100000) for s in seats], 800, 1600)
        gs.set_button(4)
        gs.new_hand()
        assert gs.position_map()[5] == "SB" and gs.position_map()[6] == "BB"
        gs.end_hand(4)
        gs.sit_out(6)                                   # BB の 6 がバスト
        gs.new_hand()                                   # ハンド 67: ボタン 5・SB なし・BB 7
        assert gs.button_seat == 5 and gs.dead_sb_seat == 6
        assert gs.position_map() == {7: "BB", 1: "UTG", 2: "HJ", 4: "CO", 5: "BTN"}
        assert gs.committed(7) == 1600 and gs.committed(5) == 0
        assert gs.legal_context().actor_seat == 1       # BB の次の 1 から
        for seat in (1, 2, 4, 5):
            assert gs.legal_context().actor_seat == seat
            gs.apply_action(seat, "call", 1600)
        assert gs.legal_context().actor_seat == 7       # BB のオプション
        gs.end_hand(1)
        gs.new_hand()                                   # ハンド 68: ボタンは空いた 6・SB 7・BB 1
        assert gs.button_seat == 6 and gs.dead_sb_seat is None
        assert gs.position_map()[7] == "SB" and gs.position_map()[1] == "BB"
        gs.end_hand(1)
        gs.new_hand()                                   # ハンド 69: ボタン 7・SB 1・BB 2
        assert gs.button_seat == 7 and gs.position_map()[1] == "SB" and gs.position_map()[2] == "BB"


def test_the_truth_replay_takes_a_button_on_an_empty_seat():
    """真のアクションの入力で空いた席をボタンに選べる（デッドボタン）。"""
    captured = {"players": [{"seat": s, "stack_start": 10000} for s in (3, 4, 5, 7, 8)],
                "blinds": {"sb": 500, "bb": 1000}, "button_seat": 8}
    gs = start_state(captured, 2)
    assert gs.button_seat == 2 and gs.position_map()[3] == "SB" and gs.legal_context().actor_seat == 5
