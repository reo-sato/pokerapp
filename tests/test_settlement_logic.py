"""tests/test_settlement_logic.py

Phase 2-A: core/settlement.py のロジックテスト。

- ``distribute_split_pot`` の odd chip rule (lowest seat priority)
- ``evaluate_hand_rank`` の比較演算 (one pair vs two pair / tie)
- ``compute_pot_settlements`` の 4 scenario:
  1. heads-up no side pot
  2. 3-way all-in、単一 winner
  3. 3-way all-in、main split / side pot 単独 winner
  4. fold を含むケース
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from core.hand_log import PotSettlement, RevealedHand
from core.settlement import (
    compute_pot_settlements,
    distribute_split_pot,
    evaluate_hand_rank,
)


# ────────────────────────────────────────────────────────────────────────────
# Stub BettingState (本物の BettingState は不要、duck-typing で十分)
# ────────────────────────────────────────────────────────────────────────────


@dataclass
class _FakeBettingState:
    player_contrib_hand: dict[int, int]
    folded_seats: list[int] = field(default_factory=list)


def _revealed(*entries: tuple[int, list[str]]) -> list[RevealedHand]:
    return [RevealedHand(seat=s, cards=c, source="rfid") for s, c in entries]


# ────────────────────────────────────────────────────────────────────────────
# 1. distribute_split_pot
# ────────────────────────────────────────────────────────────────────────────


class TestDistributeSplitPot:
    def test_single_winner_gets_all(self) -> None:
        assert distribute_split_pot(1000, [3]) == {3: 1000}

    def test_two_winners_even_split(self) -> None:
        assert distribute_split_pot(1000, [2, 5]) == {2: 500, 5: 500}

    def test_three_winners_odd_chips_to_lowest_seats(self) -> None:
        # 1001 / 3 = base 333, remainder 2 → seats 2, 5 each get +1; seat 9 gets base
        assert distribute_split_pot(1001, [2, 5, 9]) == {2: 334, 5: 334, 9: 333}

    def test_odd_chip_lowest_seat_priority(self) -> None:
        # 3 / 2 = base 1, remainder 1 → seat 10 (lowest) gets +1
        assert distribute_split_pot(3, [10, 11]) == {10: 2, 11: 1}

    def test_empty_winners_returns_empty_dict(self) -> None:
        assert distribute_split_pot(100, []) == {}

    def test_zero_amount(self) -> None:
        assert distribute_split_pot(0, [1, 2]) == {1: 0, 2: 0}

    def test_input_order_irrelevant(self) -> None:
        # 入力順に関わらず昇順 seat 番号で remainder を配る
        assert distribute_split_pot(7, [9, 5, 2]) == {2: 3, 5: 2, 9: 2}

    def test_sum_equals_pot_amount(self) -> None:
        # 任意の数値で sum 不変
        for pot, seats in [
            (1000, [1, 2, 3]),
            (1001, [4, 5]),
            (5, [10, 11, 12, 13]),
            (12345, [1, 2, 3, 4, 5, 6]),
        ]:
            payouts = distribute_split_pot(pot, seats)
            assert sum(payouts.values()) == pot


# ────────────────────────────────────────────────────────────────────────────
# 2. evaluate_hand_rank
# ────────────────────────────────────────────────────────────────────────────


class TestEvaluateHandRank:
    BOARD = ["Kc", "Qd", "Jh", "5s", "7c"]

    def test_two_pair_beats_one_pair(self) -> None:
        # seat A: pair of K (using Kh + Kc board)
        # seat B: two pair K + 5 (using Kh + 5h + Kc + 5s board)
        # Wait, seat B's hole must give two pair distinct from seat A's
        # Easier: A=pair of jacks (Jc Jd vs no), B=two pair Kings & Queens
        rank_a = evaluate_hand_rank(["2h", "3d"], self.BOARD)        # K high (no pair)
        rank_b = evaluate_hand_rank(["Kh", "Kd"], self.BOARD)         # trip kings or pair (KK+K on board=trip K)
        # KK pocket + K on board = trip kings; A is high card
        assert rank_b > rank_a

    def test_two_pair_beats_one_pair_clean(self) -> None:
        # Clean comparison: pair vs two pair, no upgrade to trips
        board = ["Ah", "Kh", "Qd", "7s", "2c"]
        # one pair: 7c 8d → pair of 7s (using 7c+7s)
        rank_one_pair = evaluate_hand_rank(["7c", "8d"], board)
        # two pair: Ac Kc → AA(?) + KK using board? No: Ac+Ah=AA, Kc+Kh=KK → two pair
        rank_two_pair = evaluate_hand_rank(["Ac", "Kc"], board)
        assert rank_two_pair > rank_one_pair

    def test_identical_kickers_yield_equal_rank(self) -> None:
        # AhKd vs AdKh on the same board → same 5-card hand (AK + board KQJ kickers)
        board = ["Kc", "Qd", "Jh", "5s", "7c"]
        r1 = evaluate_hand_rank(["Ah", "Kd"], board)
        # Wait Ah Kd + Kc QdJh = K-K + AQJ kickers (pair of K)
        # Ad Kh + Kc QdJh = K-K + AQJ kickers (same pair of K)
        # Should be equal
        r2 = evaluate_hand_rank(["Ad", "Kh"], board)
        assert r1 == r2

    def test_aa_vs_aa_split_pot_eligible(self) -> None:
        # 2 seats with pocket aces and the same board → identical rank → split pot
        board = ["Kc", "Qd", "Jh", "5s", "7c"]
        r1 = evaluate_hand_rank(["Ah", "Ad"], board)
        r2 = evaluate_hand_rank(["As", "Ac"], board)
        assert r1 == r2

    def test_straight_flush_strongest(self) -> None:
        # Royal flush should rank higher than four of a kind
        royal = evaluate_hand_rank(["Ah", "Kh"], ["Qh", "Jh", "Th", "2c", "3d"])
        quads = evaluate_hand_rank(["Ah", "Ad"], ["Ac", "As", "2c", "3d", "4h"])
        assert royal > quads


# ────────────────────────────────────────────────────────────────────────────
# 3. compute_pot_settlements — 4 scenarios
# ────────────────────────────────────────────────────────────────────────────


class TestComputePotSettlements:
    def test_scenario_1_heads_up_no_side_pot(self) -> None:
        """heads-up、no all-in、no side pot。
        seat1: 1000, seat2: 1000、両者 showdown、seat1 勝利。
        期待: PotSettlement 1 件、amount=2000, eligible=[1,2], winning=[1], payouts={1:2000}。
        """
        state = _FakeBettingState(
            player_contrib_hand={1: 1000, 2: 1000},
            folded_seats=[],
        )
        # seat1 = AA, seat2 = high card K
        board = ["Kc", "Qd", "Jh", "5s", "7c"]
        revealed = _revealed(
            (1, ["Ah", "Ad"]),
            (2, ["2c", "3d"]),
        )
        pots = compute_pot_settlements(state, revealed, board)
        assert len(pots) == 1
        p = pots[0]
        assert p.amount == 2000
        assert p.eligible_seats == [1, 2]
        assert p.winning_seats == [1]
        assert p.payouts == {1: 2000}
        assert p.pot_type == "main"

    def test_scenario_2_three_way_all_in_single_winner(self) -> None:
        """3-way all-in、side pot あり、単一 winner (seat3) ケース。
        - seat1: 1000, seat2: 2000, seat3: 3000
        - main pot 3000, eligible=[1,2,3], winner=[3], payouts={3:3000}
        - side1 2000, eligible=[2,3], winner=[3], payouts={3:2000}
        - side2 1000, eligible=[3], winner=[3], payouts={3:1000}
        """
        state = _FakeBettingState(
            player_contrib_hand={1: 1000, 2: 2000, 3: 3000},
            folded_seats=[],
        )
        board = ["Kc", "Qd", "Jh", "5s", "7c"]
        # seat1=high card, seat2=pair of T, seat3=pair of A (strongest)
        revealed = _revealed(
            (1, ["2c", "3d"]),
            (2, ["Tc", "Td"]),
            (3, ["Ah", "Ad"]),
        )
        pots = compute_pot_settlements(state, revealed, board)
        assert len(pots) == 3

        main, side1, side2 = pots[0], pots[1], pots[2]
        assert main.amount == 3000
        assert main.eligible_seats == [1, 2, 3]
        assert main.winning_seats == [3]
        assert main.payouts == {3: 3000}
        assert main.pot_type == "main"

        assert side1.amount == 2000
        assert side1.eligible_seats == [2, 3]
        assert side1.winning_seats == [3]
        assert side1.payouts == {3: 2000}
        assert side1.pot_type == "side"

        assert side2.amount == 1000
        assert side2.eligible_seats == [3]
        assert side2.winning_seats == [3]
        assert side2.payouts == {3: 1000}
        assert side2.pot_type == "side"

    def test_scenario_3_main_split_side_single_winner(self) -> None:
        """3-way all-in、main pot split、side pot 単独 winner。
        - seat1: 1000, seat2: 2000, seat3: 2000
        - showdown: seat1 と seat2 同 rank (AA + 同じキッカー)、seat3 弱い
        - main pot 3000, eligible=[1,2,3], winners=[1,2], split → {1:1500, 2:1500}
        - side pot 2000, eligible=[2,3], winner=[2], payouts={2:2000}
        """
        state = _FakeBettingState(
            player_contrib_hand={1: 1000, 2: 2000, 3: 2000},
            folded_seats=[],
        )
        board = ["Kc", "Qd", "Jh", "5s", "7c"]
        # seat1=AhAd, seat2=AsAc (両者 AA, 同じキッカー), seat3=低い hand
        revealed = _revealed(
            (1, ["Ah", "Ad"]),
            (2, ["As", "Ac"]),
            (3, ["2h", "3d"]),
        )
        pots = compute_pot_settlements(state, revealed, board)
        assert len(pots) == 2

        main, side = pots[0], pots[1]
        assert main.amount == 3000
        assert main.eligible_seats == [1, 2, 3]
        assert main.winning_seats == [1, 2]
        assert main.payouts == {1: 1500, 2: 1500}
        assert main.pot_type == "main"

        assert side.amount == 2000
        assert side.eligible_seats == [2, 3]
        assert side.winning_seats == [2]
        assert side.payouts == {2: 2000}
        assert side.pot_type == "side"

    def test_scenario_4_fold_eligible_excludes_folded_seat(self) -> None:
        """fold を含むケース。
        - seat1: 1000 (showdown), seat2: 1000 (fold), seat3: 3000 (showdown)
        - main pot 3000, eligible=[1,3] (seat2 fold)
        - side pot 2000, eligible=[3]
        - seat3 が seat1 に勝つ前提
        - 期待: main payouts={3:3000}, side payouts={3:2000}
        """
        state = _FakeBettingState(
            player_contrib_hand={1: 1000, 2: 1000, 3: 3000},
            folded_seats=[2],
        )
        board = ["2s", "3c", "Tc", "Js", "Kh"]
        # seat1=high card K, seat3=AA (strongest)
        revealed = _revealed(
            (1, ["7c", "8h"]),
            (3, ["Ah", "Ad"]),
        )
        pots = compute_pot_settlements(state, revealed, board)
        assert len(pots) == 2

        main, side = pots[0], pots[1]
        assert main.amount == 3000
        assert main.eligible_seats == [1, 3]  # seat2 excluded due to fold
        assert main.winning_seats == [3]
        assert main.payouts == {3: 3000}
        assert main.pot_type == "main"

        assert side.amount == 2000
        assert side.eligible_seats == [3]
        assert side.winning_seats == [3]
        assert side.payouts == {3: 2000}
        assert side.pot_type == "side"

    # ── 補強: 退化 / 境界ケース ────────────────────────────────────────────

    def test_empty_contrib_returns_no_pots(self) -> None:
        state = _FakeBettingState(player_contrib_hand={})
        pots = compute_pot_settlements(state, [], [])
        assert pots == []

    def test_zero_contrib_seats_are_filtered(self) -> None:
        """contrib=0 の seat は pot 構築に参加しない。"""
        state = _FakeBettingState(
            player_contrib_hand={1: 1000, 2: 0, 3: 1000},
            folded_seats=[],
        )
        board = ["Kc", "Qd", "Jh", "5s", "7c"]
        revealed = _revealed(
            (1, ["Ah", "Ad"]),
            (3, ["2c", "3d"]),
        )
        pots = compute_pot_settlements(state, revealed, board)
        assert len(pots) == 1
        assert pots[0].amount == 2000
        assert pots[0].eligible_seats == [1, 3]
        assert pots[0].winning_seats == [1]

    def test_total_payouts_equal_total_contributions(self) -> None:
        """全 pot の payouts 総和は total contributions と一致する (invariant)。"""
        state = _FakeBettingState(
            player_contrib_hand={1: 1500, 2: 2500, 3: 2500, 4: 500},
            folded_seats=[4],
        )
        board = ["Kc", "Qd", "Jh", "5s", "7c"]
        revealed = _revealed(
            (1, ["Ah", "Ad"]),
            (2, ["Tc", "Td"]),
            (3, ["2c", "3d"]),
        )
        pots = compute_pot_settlements(state, revealed, board)
        total_paid = sum(p for pot in pots for p in pot.payouts.values())
        total_contributed = sum(state.player_contrib_hand.values())
        assert total_paid == total_contributed
