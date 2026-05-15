"""tests/test_action_order.py

ディーラーボタン位置から SB/BB/first actor を算出するヘルパのテスト。
"""
from __future__ import annotations

from integration.action_order import (
    advance_actor,
    advance_button,
    compute_blinds,
    compute_first_actor_postflop,
    compute_first_actor_preflop,
    get_next_active_seat,
)


class TestAdvanceButton:
    def test_dense(self) -> None:
        assert advance_button(1, [1, 2, 3, 4, 5, 6]) == 2

    def test_skip_inactive(self) -> None:
        assert advance_button(1, [1, 3, 5, 6]) == 3

    def test_wrap(self) -> None:
        assert advance_button(8, [2, 4, 6, 8]) == 2

    def test_button_not_in_active_still_advances(self) -> None:
        # 直前ボタンが離席した場合でも円環で次を返す
        assert advance_button(2, [1, 3, 5]) == 3


class TestComputeBlinds:
    def test_6max_button_1(self) -> None:
        assert compute_blinds(1, [1, 2, 3, 4, 5, 6]) == (2, 3)

    def test_sparse_seats(self) -> None:
        assert compute_blinds(8, [2, 4, 6, 8]) == (2, 4)

    def test_heads_up(self) -> None:
        assert compute_blinds(3, [3, 7]) == (3, 7)
        assert compute_blinds(7, [3, 7]) == (7, 3)

    def test_button_not_active(self) -> None:
        assert compute_blinds(9, [1, 2, 3]) == (None, None)

    def test_single_seat(self) -> None:
        assert compute_blinds(1, [1]) == (None, None)


class TestFirstActorPreflop:
    def test_6max_starts_utg(self) -> None:
        # BTN=1, SB=2, BB=3, UTG=4
        assert compute_first_actor_preflop(1, [1, 2, 3, 4, 5, 6]) == 4

    def test_sparse_button_8(self) -> None:
        # active=[2,4,6,8], button=8 → SB=2, BB=4, first_actor=6
        assert compute_first_actor_preflop(8, [2, 4, 6, 8]) == 6

    def test_heads_up_button_acts_first(self) -> None:
        assert compute_first_actor_preflop(3, [3, 7]) == 3
        assert compute_first_actor_preflop(7, [3, 7]) == 7


class TestFirstActorPostflop:
    def test_button_1_first_actor_is_sb(self) -> None:
        assert compute_first_actor_postflop(1, [1, 2, 3, 4, 5, 6]) == 2

    def test_skips_folded_sb(self) -> None:
        assert compute_first_actor_postflop(1, [1, 2, 3, 4, 5, 6], folded_seats={2}) == 3

    def test_heads_up_bb_first(self) -> None:
        assert compute_first_actor_postflop(3, [3, 7]) == 7
        assert compute_first_actor_postflop(7, [3, 7]) == 3


class TestAdvanceActor:
    def test_advance_clockwise(self) -> None:
        assert advance_actor(3, [1, 2, 3, 4, 5, 6]) == 4

    def test_wraparound(self) -> None:
        assert advance_actor(6, [1, 2, 3, 4, 5, 6]) == 1

    def test_skip_folded(self) -> None:
        assert advance_actor(2, [1, 2, 3, 4, 5, 6], folded_seats={3, 4}) == 5

    def test_returns_none_if_no_live(self) -> None:
        assert advance_actor(2, [1, 2], folded_seats={1, 2}) is None


class TestGetNextActive:
    def test_inclusive(self) -> None:
        assert get_next_active_seat(3, [1, 2, 3, 4], inclusive=True) == 3

    def test_exclusive(self) -> None:
        assert get_next_active_seat(3, [1, 2, 3, 4]) == 4

    def test_wraps(self) -> None:
        assert get_next_active_seat(9, [1, 2, 3]) == 1
