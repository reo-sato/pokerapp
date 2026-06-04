"""tests/test_phh_exporter.py

Phase 5: PHH エクスポーターのテスト。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core.hand_log import ActionRecord, HandSummary
from output.phh_exporter import (
    PHHExporter,
    _build_blinds_list,
    _build_phh_actions,
    _record_to_phh,
    _streets_between,
    _to_toml,
    _toml_value,
)


# ――― フィクスチャ ―――

def _make_action(action: str, amount: int = 0, street: str = "preflop", seat: int = 1) -> ActionRecord:
    return ActionRecord(
        hand_id=1,
        timestamp="2026-04-02T12:00:00",
        street=street,
        seat=seat,
        player_name="Alice",
        action=action,
        amount=amount,
        pot_after=amount,
        stack_after=10000 - amount,
        source={"audio": True, "rfid": False},
        needs_review=False,
        confidence=0.5,
    )


def _make_summary(actions: list[ActionRecord], board: list[str] | None = None) -> HandSummary:
    return HandSummary(
        hand_id=1,
        session_id="test_session",
        started_at="2026-04-02T12:00:00",
        ended_at="2026-04-02T12:01:00",
        blinds={"sb": 100, "bb": 200},
        board=board or [],
        board_source="rfid" if board else "",
        players=[
            {"seat": 1, "name": "Alice", "stack_start": 10000, "stack_end": 9500, "result": -500},
            {"seat": 2, "name": "Bob",   "stack_start": 10000, "stack_end": 10500, "result": 500},
        ],
        pot_total=500,
        winner_seat=2,
        actions=actions,
        review_required=False,
    )


# ――― _record_to_phh ―――

class TestRecordToPhh:
    def test_bet(self):
        assert _record_to_phh(_make_action("bet", 500), 0) == "p0 cbr 500"

    def test_raise(self):
        assert _record_to_phh(_make_action("raise", 800), 1) == "p1 cbr 800"

    def test_allin(self):
        assert _record_to_phh(_make_action("allin", 9500), 2) == "p2 cbr 9500"

    def test_call(self):
        assert _record_to_phh(_make_action("call", 200), 0) == "p0 cc"

    def test_check(self):
        assert _record_to_phh(_make_action("check"), 0) == "p0 cc"

    def test_fold(self):
        assert _record_to_phh(_make_action("fold"), 1) == "p1 f"

    def test_unknown_returns_none(self):
        assert _record_to_phh(_make_action("winner"), 0) is None


# ――― _streets_between ―――

class TestStreetsBetween:
    def test_preflop_to_flop(self):
        assert _streets_between("preflop", "flop") == ["flop"]

    def test_preflop_to_river(self):
        assert _streets_between("preflop", "river") == ["flop", "turn", "river"]

    def test_flop_to_turn(self):
        assert _streets_between("flop", "turn") == ["turn"]

    def test_same_street(self):
        assert _streets_between("preflop", "preflop") == []

    def test_unknown_street(self):
        assert _streets_between("preflop", "unknown") == []


# ――― _build_blinds_list ―――

class TestBuildBlindsList:
    def test_two_players(self):
        assert _build_blinds_list({"sb": 100, "bb": 200}, 2) == [100, 200]

    def test_six_players(self):
        result = _build_blinds_list({"sb": 100, "bb": 200}, 6)
        assert result == [100, 200, 0, 0, 0, 0]

    def test_single_player(self):
        result = _build_blinds_list({"sb": 100, "bb": 200}, 1)
        assert result == [100]


# ――― _build_phh_actions ―――

class TestBuildPhhActions:
    def test_preflop_only_deals_hole_cards(self):
        actions = [_make_action("bet", 500, street="preflop", seat=1)]
        summary = _make_summary(actions)
        result = _build_phh_actions(summary)

        # 最初の 2 エントリはホールカードのデール
        assert result[0] == "d dh p0 ????"
        assert result[1] == "d dh p1 ????"
        # その後にベットアクション
        assert "p0 cbr 500" in result

    def test_street_transition_inserts_board_deal(self):
        actions = [
            _make_action("bet",  500, street="preflop", seat=1),
            _make_action("call", 500, street="preflop", seat=2),
            _make_action("check", 0,  street="flop",   seat=1),
        ]
        summary = _make_summary(actions)
        result = _build_phh_actions(summary)

        # フロップに遷移したときにボードカードがデールされる
        assert "d db ??????" in result

        # ボードデールはフロップアクションの前にある
        flop_deal_idx = result.index("d db ??????")
        check_idx = next(i for i, a in enumerate(result) if a == "p0 cc" and i > flop_deal_idx)
        assert flop_deal_idx < check_idx

    def test_known_board_cards_used(self):
        actions = [
            _make_action("check", 0, street="flop", seat=1),
        ]
        summary = _make_summary(actions, board=["Ah", "Kd", "Qh"])
        result = _build_phh_actions(summary)

        assert "d db AhKdQh" in result

    def test_partial_board_falls_back_to_unknown(self):
        """ボードにカードが 2 枚しかない場合は未知扱い。"""
        actions = [_make_action("check", 0, street="flop", seat=1)]
        summary = _make_summary(actions, board=["Ah", "Kd"])  # 2枚だけ
        result = _build_phh_actions(summary)

        assert "d db ??????" in result  # 未知にフォールバック

    def test_full_hand_street_sequence(self):
        actions = [
            _make_action("bet",   500, "preflop", 1),
            _make_action("call",  500, "preflop", 2),
            _make_action("bet",   300, "flop",    1),
            _make_action("call",  300, "flop",    2),
            _make_action("bet",   200, "turn",    1),
            _make_action("fold",    0, "turn",    2),
        ]
        summary = _make_summary(actions)
        result = _build_phh_actions(summary)

        # ボードデールが 2 回（flop + turn）
        board_deals = [a for a in result if a.startswith("d db")]
        assert len(board_deals) == 2
        assert board_deals[0] == "d db ??????"  # flop: 3枚
        assert board_deals[1] == "d db ??"       # turn: 1枚

    def test_empty_players_returns_empty(self):
        summary = HandSummary(
            hand_id=1, session_id="s", started_at="", ended_at="",
            blinds={"sb": 100, "bb": 200}, board=[], board_source="",
            players=[], pot_total=0, winner_seat=0, actions=[], review_required=False,
        )
        assert _build_phh_actions(summary) == []


# ――― _toml_value ―――

class TestTomlValue:
    def test_bool_true(self):
        assert _toml_value(True) == "true"

    def test_bool_false(self):
        assert _toml_value(False) == "false"

    def test_int(self):
        assert _toml_value(42) == "42"

    def test_string(self):
        assert _toml_value("hello") == '"hello"'

    def test_string_with_quotes_escaped(self):
        assert _toml_value('say "hi"') == '"say \\"hi\\""'

    def test_empty_list(self):
        assert _toml_value([]) == "[]"

    def test_int_list(self):
        assert _toml_value([1, 2, 3]) == "[1, 2, 3]"

    def test_string_list_multiline(self):
        result = _toml_value(["p0 cbr 500", "p1 cc"])
        assert result.startswith("[\n")
        assert "p0 cbr 500" in result
        assert "p1 cc" in result

    def test_unsupported_type_raises(self):
        with pytest.raises(TypeError):
            _toml_value({"key": "val"})


# ――― PHHExporter ―――

class TestPHHExporter:
    def test_export_contains_required_fields(self):
        actions = [_make_action("bet", 500, seat=1)]
        summary = _make_summary(actions)
        exporter = PHHExporter()
        content = exporter.export(summary)

        assert 'variant = "NT"' in content
        assert "ante_trimming_status = true" in content
        assert "starting_stacks" in content
        assert "actions" in content
        assert "blinds_or_straddles" in content

    def test_export_contains_player_names(self):
        actions = [_make_action("fold", 0, seat=1)]
        summary = _make_summary(actions)
        exporter = PHHExporter(author="TestAuthor")
        content = exporter.export(summary)

        assert "Alice" in content
        assert "Bob" in content
        assert "TestAuthor" in content

    def test_write_creates_file(self, tmp_path: Path):
        actions = [_make_action("check", 0, seat=1)]
        summary = _make_summary(actions)
        exporter = PHHExporter()
        out = tmp_path / "hand_0001.phh"
        exporter.write(summary, out)

        assert out.exists()
        content = out.read_text(encoding="utf-8")
        assert 'variant = "NT"' in content

    def test_write_session_creates_multiple_files(self, tmp_path: Path):
        def _s(hand_id: int) -> HandSummary:
            a = _make_action("fold", 0, seat=1)
            s = _make_summary([a])
            s.hand_id = hand_id
            return s

        summaries = [_s(1), _s(2), _s(3)]
        exporter = PHHExporter()
        paths = exporter.write_session(summaries, tmp_path / "phh")

        assert len(paths) == 3
        assert all(p.exists() for p in paths)
        assert paths[0].name == "0001.phh"
        assert paths[2].name == "0003.phh"

    def test_review_required_adds_note(self):
        actions = [_make_action("bet", 500, seat=1)]
        summary = _make_summary(actions)
        summary.review_required = True
        exporter = PHHExporter()
        content = exporter.export(summary)

        assert "note" in content
        assert "review" in content
