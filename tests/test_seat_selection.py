"""tests/test_seat_selection.py

Phase E3 (ISSUE-0006): seat 選択ダイアログの純ロジック(customtkinter 非依存)。

ダイアログ本体は customtkinter(GUI)依存のため CI(GUI 非導入・skip 0)では import しない。
seat_player_map の構築・重複検証・初期選択 carry-forward の純関数のみを検証する。
"""
from __future__ import annotations

from gui.seat_selection import (
    build_result_map,
    find_duplicate_player,
    resolve_initial_selections,
)


class TestResolveInitialSelections:
    def test_carry_forward_maps_known_seats(self):
        sel = resolve_initial_selections([1, 2, 3], {1: "pa", 3: "pc"})
        assert sel == {1: "pa", 2: None, 3: "pc"}

    def test_none_initial_all_empty(self):
        assert resolve_initial_selections([1, 2], None) == {1: None, 2: None}

    def test_ignores_seats_not_in_table(self):
        sel = resolve_initial_selections([1, 2], {1: "pa", 5: "pe"})
        assert sel == {1: "pa", 2: None}


class TestFindDuplicatePlayer:
    def test_detects_same_player_two_seats(self):
        assert find_duplicate_player({1: "pa", 2: "pa", 3: None}) == "pa"

    def test_none_when_unique(self):
        assert find_duplicate_player({1: "pa", 2: "pb", 3: None}) is None

    def test_empty_seats_ignored(self):
        assert find_duplicate_player({1: None, 2: None}) is None


class TestBuildResultMap:
    def test_drops_empty_seats(self):
        assert build_result_map({1: "pa", 2: None, 3: "pc"}) == {1: "pa", 3: "pc"}

    def test_all_empty_yields_empty(self):
        assert build_result_map({1: None, 2: None}) == {}

    def test_keeps_only_assigned(self):
        assert build_result_map({1: "pa", 2: "pb"}) == {1: "pa", 2: "pb"}
