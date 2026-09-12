"""tests/test_positions.py

ISSUE-0032 / 仕様 FR-05b〜h — ディーラーボタンの回転とポジション名。

- `core/positions.py` の純粋ロジック（並び / 名前 / 別名パース）
- `PokerkitGameState` が **ハンドごとにボタンを 1 つ回す**こと（= ターン順 prior が毎ハンド変わる）
- ポジション名が **実際にブラインドを出す席**と一致すること（名前だけの飾りでないことの固定）
"""
from __future__ import annotations

import pytest

from core.game_state import GameStateManager, PlayerState
from core.hand_log import ActionRecord, HandSummary
from core.positions import (
    next_button,
    parse_position,
    position_map,
    position_names,
    seat_for_position,
    seat_order_from_button,
)


def _players(n: int, stack: int = 10000) -> list[PlayerState]:
    return [PlayerState(seat=i + 1, name=f"P{i + 1}", stack=stack) for i in range(n)]


# ――― 純粋ロジック ―――

class TestSeatOrder:
    def test_button_last_sb_first(self):
        assert seat_order_from_button([1, 2, 3, 4], 4) == [1, 2, 3, 4]
        assert seat_order_from_button([1, 2, 3, 4], 1) == [2, 3, 4, 1]
        assert seat_order_from_button([1, 2, 3, 4], 3) == [4, 1, 2, 3]

    def test_unknown_button_falls_back_to_last_seat(self):
        """卓に無いボタン席は末尾席とみなす（並びは sorted(seats) のまま = 導入前と同じ）。"""
        assert seat_order_from_button([2, 5, 7], 9) == [2, 5, 7]

    def test_non_contiguous_seats(self):
        assert seat_order_from_button([2, 5, 7], 5) == [7, 2, 5]

    def test_empty(self):
        assert seat_order_from_button([], 1) == []


class TestNextButton:
    def test_initial_button_is_last_seat(self):
        """初回（None）は最大席番号 → 1 ハンド目の並びがボタン導入前と一致する（golden 不変）。"""
        assert next_button([1, 2, 3], None) == 3
        assert next_button([2, 5, 7], None) == 7

    def test_rotates_and_wraps(self):
        seats = [1, 2, 3]
        assert next_button(seats, 3) == 1
        assert next_button(seats, 1) == 2
        assert next_button(seats, 2) == 3

    def test_unknown_button_restarts_from_last_seat(self):
        assert next_button([1, 2, 3], 9) == 3

    def test_full_cycle_visits_every_seat(self):
        seats = [1, 2, 3, 4, 5]
        b = None
        seen = []
        for _ in range(len(seats)):
            b = next_button(seats, b)
            seen.append(b)
        assert sorted(seen) == seats

    def test_empty_seats_raises(self):
        with pytest.raises(ValueError):
            next_button([], None)


class TestPositionNames:
    def test_heads_up_button_is_the_small_blind(self):
        """HU は **ボタンが SB**（pokerkit も index 0=BB / 末尾=ボタン=SB に post する）。"""
        assert position_names(2) == ["BB", "BTN"]

    @pytest.mark.parametrize(
        "n,expected",
        [
            (3, ["SB", "BB", "BTN"]),
            (4, ["SB", "BB", "UTG", "BTN"]),
            (6, ["SB", "BB", "UTG", "HJ", "CO", "BTN"]),
            (9, ["SB", "BB", "UTG", "UTG+1", "UTG+2", "MP", "HJ", "CO", "BTN"]),
        ],
    )
    def test_ladder(self, n, expected):
        assert position_names(n) == expected

    def test_over_nine_is_padded_not_crashing(self):
        names = position_names(11)
        assert len(names) == 11
        assert names[0] == "SB" and names[-1] == "BTN" and names[-2] == "CO"

    def test_degenerate(self):
        assert position_names(0) == []
        assert position_names(1) == ["BTN"]


class TestPositionMap:
    def test_map_follows_the_button(self):
        assert position_map([1, 2, 3], 3) == {1: "SB", 2: "BB", 3: "BTN"}
        assert position_map([1, 2, 3], 1) == {2: "SB", 3: "BB", 1: "BTN"}

    def test_seat_for_position_round_trip(self):
        seats, button = [1, 2, 3, 4, 5, 6], 2
        for seat, name in position_map(seats, button).items():
            assert seat_for_position(seats, button, name) == seat

    def test_seat_for_position_accepts_aliases(self):
        seats, button = [1, 2, 3], 3
        assert seat_for_position(seats, button, "ボタン") == 3
        assert seat_for_position(seats, button, "bb") == 2

    def test_seat_for_position_absent_returns_none(self):
        assert seat_for_position([1, 2, 3], 3, "UTG+2") is None


class TestParsePosition:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("BTN、コール", "BTN"),
            ("btn call", "BTN"),
            ("ボタン コール", "BTN"),
            ("ビッグブラインド チェック", "BB"),
            ("スモール フォールド", "SB"),
            ("UTG レイズ 600", "UTG"),
            ("utg+1 コール", "UTG+1"),
            ("utg1 コール", "UTG+1"),
            ("CO ベット 800", "CO"),
            ("カットオフ ベット", "CO"),
            ("ハイジャック フォールド", "HJ"),
        ],
    )
    def test_extracts_canonical_name(self, text, expected):
        assert parse_position(text) == expected

    def test_longest_alias_wins_at_same_position(self):
        """"utg+1" が "utg" に負けない（最長一致優先）。"""
        assert parse_position("UTG+2 オールイン") == "UTG+2"

    @pytest.mark.parametrize("text", ["チェック", "コール 500", "シート3 レイズ", ""])
    def test_no_position_returns_none(self, text):
        assert parse_position(text) is None

    @pytest.mark.parametrize("text", ["scoop", "combo", "abbreviate", "mpeg", "hjkl"])
    def test_latin_aliases_need_word_boundaries(self, text):
        """英字別名は語中で当たらない（"co"/"bb"/"mp"/"hj" の偽陽性を防ぐ）。"""
        assert parse_position(text) is None


# ――― engine（pokerkit）―――

def _pk(n: int, button_seat=None):
    pytest.importorskip("pokerkit")
    from core.poker_engine import PokerkitGameState

    return PokerkitGameState(_players(n), 100, 200, button_seat=button_seat)


class TestButtonRotation:
    def test_first_hand_matches_pre_button_ordering(self):
        """既定（button_seat=None）の 1 ハンド目は導入前と同じ並び = 既存 golden が不変。"""
        gs = _pk(3)
        gs.new_hand()
        assert gs.button_seat == 3
        assert gs.get_current_player() == 3          # 3-handed preflop は BTN が先頭
        assert gs.position_map() == {1: "SB", 2: "BB", 3: "BTN"}

    def test_button_and_first_actor_move_every_hand(self):
        gs = _pk(6)
        buttons, actors = [], []
        for _ in range(6):
            gs.new_hand()
            buttons.append(gs.button_seat)
            actors.append(gs.get_current_player())
            gs.end_hand(gs.get_current_player())
        assert buttons == [6, 1, 2, 3, 4, 5]
        assert sorted(actors) == [1, 2, 3, 4, 5, 6]   # UTG も 1 周する
        assert actors[0] == 3                          # ボタン 6 → SB=1, BB=2, UTG=3

    def test_explicit_initial_button(self):
        """`button_seat` は「1 ハンド目のボタンの 1 つ手前」。2 を渡すと 1 ハンド目は 3。"""
        gs = _pk(4, button_seat=2)
        gs.new_hand()
        assert gs.button_seat == 3

    @pytest.mark.parametrize("n", [2, 3, 6, 9])
    def test_position_names_match_who_posts_blinds(self, n):
        """ポジション名は飾りではない: SB/BB と名付けた席が実際にブラインドを出している。

        HU では **ボタンが SB**（`position_names(2) == ["BB","BTN"]`）なので、BTN の commit が
        SB 額・BB の commit が BB 額になる。この対応が壊れると actor 推定の prior が狂う。
        """
        gs = _pk(n)
        gs.new_hand()
        pmap = gs.position_map()
        committed = {seat: gs.committed(seat) for seat in pmap}
        by_name = {name: committed[seat] for seat, name in pmap.items()}
        assert by_name["BB"] == 200
        assert by_name["SB" if n > 2 else "BTN"] == 100
        others = [v for name, v in by_name.items() if name not in ("SB", "BB", "BTN")]
        assert all(v == 0 for v in others)

    def test_rotation_survives_a_full_orbit(self):
        gs = _pk(5)
        seen = set()
        for _ in range(5):
            gs.new_hand()
            seen.add(gs.button_seat)
            gs.end_hand(1)
        assert seen == {1, 2, 3, 4, 5}


class TestLegacyHasNoButton:
    """legacy backend は単純ラウンドロビンのまま（rollback path の挙動不変, ISSUE-0032）。"""

    def test_button_seat_is_none(self):
        gs = GameStateManager(_players(3), 100, 200)
        gs.new_hand()
        assert gs.button_seat is None
        assert gs.position_map() == {}

    def test_actor_order_is_unchanged_across_hands(self):
        gs = GameStateManager(_players(3), 100, 200)
        gs.new_hand()
        assert gs.get_current_player() == 1
        gs.new_hand()
        assert gs.get_current_player() == 1


# ――― 記録（additive フィールド）―――

class TestRecordedPositionFields:
    def test_action_record_defaults_to_empty_position(self):
        rec = ActionRecord(
            hand_id=1, timestamp="t", street="preflop", seat=1, player_name="P1",
            action="call", amount=200, pot_after=400, stack_after=9800,
            source={"camera": False, "audio": True, "rfid": False}, needs_review=False,
        )
        assert rec.to_dict()["position"] == ""

    def test_hand_summary_serializes_button_and_map(self):
        hs = HandSummary(
            hand_id=1, session_id="s", started_at="a", ended_at="b",
            blinds={"sb": 100, "bb": 200}, board=[], board_source="", players=[],
            pot_total=0, winner_seat=1, actions=[], review_required=False,
            button_seat=3, position_map={1: "SB", 2: "BB", 3: "BTN"},
        )
        d = hs.to_dict()
        assert d["button_seat"] == 3
        # JSON キーは文字列化する（schema の propertyNames と一致させる）
        assert d["position_map"] == {"1": "SB", "2": "BB", "3": "BTN"}

    def test_hand_summary_defaults_are_absent_button(self):
        hs = HandSummary(
            hand_id=1, session_id="s", started_at="a", ended_at="b",
            blinds={"sb": 100, "bb": 200}, board=[], board_source="", players=[],
            pot_total=0, winner_seat=1, actions=[], review_required=False,
        )
        d = hs.to_dict()
        assert d["button_seat"] is None
        assert d["position_map"] == {}
