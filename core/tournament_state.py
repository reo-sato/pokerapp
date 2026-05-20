"""core/tournament_state.py

Tournament の運営側 state (entries / busts / addons) と派生量 helper。

設計方針:
- ``TournamentState`` は entries / busts / addons の **3 値だけ** を保持する。
  starting_stack / addon_chips は ``TournamentStructure`` 側の責務なので mixing
  しない (= total_chips / avg_stack を property として持たせない)。
- ``compute_total_chips`` / ``compute_avg_stack`` は state + structure を引数で
  受ける pure helper として置く。
- GUI thread 内のみで触る前提でスレッド安全は考慮しない。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.tournament_timer import TournamentStructure


@dataclass
class TournamentState:
    entries: int = 0
    addons: int = 0
    busts: int = 0

    @property
    def players_remaining(self) -> int:
        return max(0, self.entries - self.busts)

    def add_entry(self, n: int = 1) -> None:
        self.entries = max(0, self.entries + n)

    def add_bust(self, n: int = 1) -> None:
        self.busts = max(0, self.busts + n)

    def add_addon(self, n: int = 1) -> None:
        self.addons = max(0, self.addons + n)


def compute_total_chips(
    state: TournamentState, structure: "TournamentStructure"
) -> int:
    return state.entries * structure.starting_stack + state.addons * structure.addon_chips


def compute_avg_stack(
    state: TournamentState, structure: "TournamentStructure"
) -> int:
    remaining = state.players_remaining
    if remaining <= 0:
        return 0
    return compute_total_chips(state, structure) // remaining
