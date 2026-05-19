"""tests/test_hand_finalizer.py

Phase 2-B: HandFinalizer の単体テスト。

カバーする 4 シナリオ + 補強:
  1. heads-up fold win
  2. showdown single winner (settlement core 経由で sidepot_showdown を含む)
  3. showdown split (main pot 同 rank 複数 winner)
  4. incomplete (board < 5 / revealed 不足 / live_seats 0 など)
  +: winner_seat_hint と settlement の食い違いで review_required=True
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from core.hand_finalizer import HandFinalizer
from core.hand_log import ActionRecord, RevealedHand


# ────────────────────────────────────────────────────────────────────────────
# Fake BettingState (settlement core のテスト stub と同形)
# ────────────────────────────────────────────────────────────────────────────


@dataclass
class _FakeBettingState:
    active_seats: list[int] = field(default_factory=list)
    folded_seats: list[int] = field(default_factory=list)
    all_in_seats: list[int] = field(default_factory=list)
    player_contrib_hand: dict[int, int] = field(default_factory=dict)


def _revealed(*entries: tuple[int, list[str]]) -> list[RevealedHand]:
    return [RevealedHand(seat=s, cards=c, source="rfid") for s, c in entries]


def _action(seat: int, action: str, amount: int = 0, needs_review: bool = False) -> ActionRecord:
    return ActionRecord(
        hand_id=1,
        timestamp="2026-01-01T00:00:00",
        street="preflop",
        seat=seat,
        player_name=f"P{seat}",
        action=action,
        amount=amount,
        pot_after=0,
        stack_after=0,
        source={"audio": True, "camera": False, "rfid": False},
        needs_review=needs_review,
    )


def _players_info(seats: list[int], stack_start: int = 10000) -> list[dict]:
    return [
        {
            "seat": s,
            "name": f"P{s}",
            "hole_cards": None,
            "hole_cards_source": "",
            "stack_start": stack_start,
            "stack_end": stack_start,
            "result": 0,
        }
        for s in seats
    ]


def _common_kwargs(**overrides) -> dict:
    base = dict(
        hand_id=1,
        session_id="t",
        started_at="2026-01-01T00:00:00",
        ended_at="2026-01-01T00:01:00",
        blinds={"sb": 100, "bb": 200},
        actions=[],
        board_source="",
        winner_seat_hint=None,
    )
    base.update(overrides)
    return base


# ────────────────────────────────────────────────────────────────────────────
# 1. heads-up fold win
# ────────────────────────────────────────────────────────────────────────────


class TestFoldWin:
    def test_heads_up_fold_win(self) -> None:
        """seat1/2 active, seat2 fold → fold_win, payouts to seat1."""
        state = _FakeBettingState(
            active_seats=[1, 2],
            folded_seats=[2],
            all_in_seats=[],
            player_contrib_hand={1: 1000, 2: 1000},
        )
        summary = HandFinalizer().finalize(
            betting_state=state,
            board=[],
            revealed_hands=[],
            pot_total=2000,
            players_info=_players_info([1, 2]),
            **_common_kwargs(actions=[_action(2, "fold")]),
        )
        assert summary.resolution_status == "final"
        assert summary.resolution_type == "fold_win"
        assert summary.seat_payouts == {1: 2000}
        assert summary.winner_seat == 1
        assert len(summary.pots) == 1
        pot = summary.pots[0]
        assert pot.amount == 2000
        assert pot.winning_seats == [1]
        assert pot.payouts == {1: 2000}
        assert pot.pot_type == "main"
        # eligible は active 全員 (fold 含む)
        assert pot.eligible_seats == [1, 2]
        # showdown 不要
        assert summary.showdown_revealed_cards == {}

    def test_fold_win_no_pot(self) -> None:
        """pot_total=0 でも crash しないこと (退化 case)。"""
        state = _FakeBettingState(active_seats=[1, 2], folded_seats=[2])
        summary = HandFinalizer().finalize(
            betting_state=state,
            board=[],
            revealed_hands=[],
            pot_total=0,
            players_info=_players_info([1, 2]),
            **_common_kwargs(),
        )
        assert summary.resolution_status == "final"
        assert summary.resolution_type == "fold_win"
        assert summary.winner_seat == 1
        assert summary.seat_payouts == {}
        # pot は 1 件、amount=0、payouts は空
        assert len(summary.pots) == 1
        assert summary.pots[0].amount == 0


# ────────────────────────────────────────────────────────────────────────────
# 2. showdown single winner (+ side pot)
# ────────────────────────────────────────────────────────────────────────────


class TestShowdown:
    def test_single_winner_no_side_pot(self) -> None:
        """heads-up showdown, equal contributions → resolution_type='showdown'."""
        state = _FakeBettingState(
            active_seats=[1, 2],
            folded_seats=[],
            all_in_seats=[],
            player_contrib_hand={1: 1000, 2: 1000},
        )
        board = ["Kc", "Qd", "Jh", "5s", "7c"]
        revealed = _revealed(
            (1, ["Ah", "Ad"]),   # AA (winner)
            (2, ["2c", "3d"]),   # high card
        )
        summary = HandFinalizer().finalize(
            betting_state=state,
            board=board,
            revealed_hands=revealed,
            pot_total=2000,
            players_info=_players_info([1, 2]),
            **_common_kwargs(),
        )
        assert summary.resolution_type == "showdown"
        assert summary.resolution_status == "final"
        assert summary.seat_payouts == {1: 2000}
        assert summary.winner_seat == 1
        assert len(summary.pots) == 1
        assert summary.pots[0].winning_seats == [1]
        assert summary.showdown_revealed_cards == {1: ["Ah", "Ad"], 2: ["2c", "3d"]}

    def test_sidepot_showdown_three_way_all_in(self) -> None:
        """3-way all-in 1000/2000/3000、seat3 が最強 → sidepot_showdown。"""
        state = _FakeBettingState(
            active_seats=[1, 2, 3],
            folded_seats=[],
            all_in_seats=[1, 2, 3],
            player_contrib_hand={1: 1000, 2: 2000, 3: 3000},
        )
        board = ["Kc", "Qd", "Jh", "5s", "7c"]
        revealed = _revealed(
            (1, ["2c", "3d"]),   # high card
            (2, ["Tc", "Td"]),   # pair of T
            (3, ["Ah", "Ad"]),   # pair of A (winner of all pots)
        )
        summary = HandFinalizer().finalize(
            betting_state=state,
            board=board,
            revealed_hands=revealed,
            pot_total=6000,
            players_info=_players_info([1, 2, 3]),
            **_common_kwargs(),
        )
        assert summary.resolution_type == "sidepot_showdown"
        assert summary.resolution_status == "final"
        # seat3 が全 pot を獲得
        assert summary.seat_payouts == {3: 6000}
        assert summary.winner_seat == 3
        assert len(summary.pots) == 3
        # main pot
        assert summary.pots[0].amount == 3000
        assert summary.pots[0].pot_type == "main"
        assert summary.pots[0].winning_seats == [3]
        # side pots
        assert summary.pots[1].amount == 2000
        assert summary.pots[1].pot_type == "side"
        assert summary.pots[2].amount == 1000
        assert summary.pots[2].pot_type == "side"

    def test_winner_hint_mismatch_marks_review(self) -> None:
        """winner_seat_hint が settlement と食い違うと review_required=True。"""
        state = _FakeBettingState(
            active_seats=[1, 2],
            folded_seats=[],
            player_contrib_hand={1: 1000, 2: 1000},
        )
        board = ["Kc", "Qd", "Jh", "5s", "7c"]
        revealed = _revealed(
            (1, ["Ah", "Ad"]),   # AA (true winner)
            (2, ["2c", "3d"]),
        )
        summary = HandFinalizer().finalize(
            betting_state=state,
            board=board,
            revealed_hands=revealed,
            pot_total=2000,
            players_info=_players_info([1, 2]),
            **_common_kwargs(winner_seat_hint=2),  # 誤った hint
        )
        # settlement 上の winner は seat1
        assert summary.winner_seat == 1
        # hint と矛盾 → review_required
        assert summary.review_required is True


# ────────────────────────────────────────────────────────────────────────────
# 3. showdown split
# ────────────────────────────────────────────────────────────────────────────


class TestShowdownSplit:
    def test_main_split_two_aa(self) -> None:
        """2 seat とも AA + 同じキッカーで split → showdown_split。"""
        state = _FakeBettingState(
            active_seats=[1, 2],
            folded_seats=[],
            player_contrib_hand={1: 1000, 2: 1000},
        )
        board = ["Kc", "Qd", "Jh", "5s", "7c"]
        revealed = _revealed(
            (1, ["Ah", "Ad"]),
            (2, ["As", "Ac"]),
        )
        summary = HandFinalizer().finalize(
            betting_state=state,
            board=board,
            revealed_hands=revealed,
            pot_total=2000,
            players_info=_players_info([1, 2]),
            **_common_kwargs(),
        )
        assert summary.resolution_type == "showdown_split"
        assert summary.resolution_status == "final"
        assert summary.seat_payouts == {1: 1000, 2: 1000}
        # primary winner (compat): 最低 seat 番号 (tie 時)
        assert summary.winner_seat == 1
        assert len(summary.pots) == 1
        assert sorted(summary.pots[0].winning_seats) == [1, 2]


# ────────────────────────────────────────────────────────────────────────────
# 4. incomplete
# ────────────────────────────────────────────────────────────────────────────


class TestIncomplete:
    def test_board_under_5_with_multiple_live(self) -> None:
        """board 3 枚しかないのに live_seats >= 2 → incomplete。"""
        state = _FakeBettingState(
            active_seats=[1, 2],
            folded_seats=[],
            player_contrib_hand={1: 500, 2: 500},
        )
        summary = HandFinalizer().finalize(
            betting_state=state,
            board=["Kc", "Qd", "Jh"],  # flop only
            revealed_hands=_revealed((1, ["Ah", "Ad"]), (2, ["2c", "3d"])),
            pot_total=1000,
            players_info=_players_info([1, 2]),
            **_common_kwargs(),
        )
        assert summary.resolution_status == "incomplete"
        assert summary.resolution_type is None
        assert summary.pots == []
        assert summary.seat_payouts == {}
        # incomplete でも winner_seat (compat) は何らかの値を持つ
        assert summary.winner_seat in {1, 2}
        # incomplete は常に review_required
        assert summary.review_required is True

    def test_revealed_hands_missing(self) -> None:
        """showdown のはずだが seat2 の hole cards が来ていない → incomplete。"""
        state = _FakeBettingState(
            active_seats=[1, 2],
            folded_seats=[],
            player_contrib_hand={1: 1000, 2: 1000},
        )
        summary = HandFinalizer().finalize(
            betting_state=state,
            board=["Kc", "Qd", "Jh", "5s", "7c"],
            revealed_hands=_revealed((1, ["Ah", "Ad"])),  # seat2 missing
            pot_total=2000,
            players_info=_players_info([1, 2]),
            **_common_kwargs(),
        )
        assert summary.resolution_status == "incomplete"
        assert summary.resolution_type is None

    def test_no_live_seats_degenerate(self) -> None:
        """全 seat fold (退化) → incomplete。"""
        state = _FakeBettingState(
            active_seats=[1, 2],
            folded_seats=[1, 2],
            player_contrib_hand={1: 100, 2: 200},
        )
        summary = HandFinalizer().finalize(
            betting_state=state,
            board=[],
            revealed_hands=[],
            pot_total=300,
            players_info=_players_info([1, 2]),
            **_common_kwargs(),
        )
        assert summary.resolution_status == "incomplete"
        assert summary.resolution_type is None

    def test_incomplete_preserves_winner_hint_in_compat_field(self) -> None:
        """incomplete でも winner_seat_hint が compat field に乗る。"""
        state = _FakeBettingState(
            active_seats=[1, 2],
            folded_seats=[],
            player_contrib_hand={1: 500, 2: 500},
        )
        summary = HandFinalizer().finalize(
            betting_state=state,
            board=["Kc", "Qd"],   # board 不足
            revealed_hands=[],
            pot_total=1000,
            players_info=_players_info([1, 2]),
            **_common_kwargs(winner_seat_hint=2),
        )
        assert summary.resolution_status == "incomplete"
        assert summary.winner_seat == 2   # hint が compat field に乗る


# ────────────────────────────────────────────────────────────────────────────
# 5. needs_review propagation
# ────────────────────────────────────────────────────────────────────────────


class TestReviewPropagation:
    def test_existing_action_needs_review_propagates(self) -> None:
        """元 ActionRecord に needs_review=True があると HandSummary.review_required=True。"""
        state = _FakeBettingState(active_seats=[1, 2], folded_seats=[2],
                                   player_contrib_hand={1: 200, 2: 100})
        summary = HandFinalizer().finalize(
            betting_state=state,
            board=[],
            revealed_hands=[],
            pot_total=300,
            players_info=_players_info([1, 2]),
            **_common_kwargs(actions=[_action(2, "fold", needs_review=True)]),
        )
        assert summary.resolution_type == "fold_win"
        assert summary.review_required is True
