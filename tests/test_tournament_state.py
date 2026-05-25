"""tests/test_tournament_state.py — TournamentState + helper の単体テスト。"""
from __future__ import annotations

from core.tournament_state import (
    TournamentState,
    compute_avg_stack,
    compute_total_chips,
)
from core.tournament_timer import BlindLevel, TournamentStructure


def _structure(starting_stack: int = 20000, addon_chips: int = 10000) -> TournamentStructure:
    return TournamentStructure(
        name="t",
        starting_stack=starting_stack,
        addon_chips=addon_chips,
        late_reg_closes_after_level=0,
        levels=[BlindLevel(
            level=1, label="Level 1", sb=100, bb=200,
            ante=0, duration_sec=1200, is_break=False,
        )],
    )


class TestTournamentStateBasic:
    def test_default_zero(self):
        s = TournamentState()
        assert s.entries == 0 and s.busts == 0 and s.addons == 0
        assert s.players_remaining == 0

    def test_add_entry_bust_addon(self):
        s = TournamentState()
        s.add_entry(5)
        s.add_bust(2)
        s.add_addon(3)
        assert s.entries == 5
        assert s.busts == 2
        assert s.addons == 3
        assert s.players_remaining == 3

    def test_players_remaining_clamps_to_zero(self):
        s = TournamentState(entries=3, busts=10)
        assert s.players_remaining == 0

    def test_add_negative_does_not_go_below_zero(self):
        s = TournamentState(entries=2, busts=1, addons=1)
        s.add_entry(-5)
        s.add_bust(-5)
        s.add_addon(-5)
        assert s.entries == 0
        assert s.busts == 0
        assert s.addons == 0


class TestComputeHelpers:
    def test_total_chips(self):
        s = TournamentState(entries=10, addons=2)
        st = _structure(starting_stack=20000, addon_chips=10000)
        assert compute_total_chips(s, st) == 10 * 20000 + 2 * 10000

    def test_avg_stack(self):
        s = TournamentState(entries=10, busts=4, addons=0)
        st = _structure(starting_stack=20000, addon_chips=10000)
        # remaining = 6, total = 200000 → avg = 33333
        assert compute_avg_stack(s, st) == 200000 // 6

    def test_avg_stack_zero_remaining(self):
        s = TournamentState(entries=5, busts=5)
        st = _structure()
        assert compute_avg_stack(s, st) == 0

    def test_avg_stack_no_entries(self):
        s = TournamentState()
        st = _structure()
        assert compute_avg_stack(s, st) == 0
