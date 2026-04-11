"""tests/test_output.py

output/json_writer.py と output/phh_exporter.py の新機能テスト。
- JsonWriter.append_action (FR-36)
- JsonWriter.write_hand_summary (FR-39)
- PHHExporter.export_hand with PokerKit (FR-37)
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.hand_log import ActionRecord, HandSummary
from output.json_writer import JsonWriter, _append_jsonl
from output.phh_exporter import PHHExporter


# ――― フィクスチャ ―――

def _make_action(
    action: str = "raise",
    amount: int = 800,
    street: str = "preflop",
    seat: int = 1,
    hand_id: int = 1,
) -> ActionRecord:
    return ActionRecord(
        hand_id=hand_id,
        timestamp="2026-04-11T10:00:00",
        street=street,
        seat=seat,
        player_name="Sato",
        action=action,
        amount=amount,
        pot_after=amount,
        stack_after=10000 - amount,
        source={"audio": True, "rfid": False},
        needs_review=False,
        confidence=0.9,
        position="BTN",
        actor_confidence=0.85,
    )


def _make_summary(hand_id: int = 1, actions: list[ActionRecord] | None = None) -> HandSummary:
    if actions is None:
        actions = [_make_action(hand_id=hand_id)]
    return HandSummary(
        hand_id=hand_id,
        session_id="test_session",
        started_at="2026-04-11T10:00:00",
        ended_at="2026-04-11T10:01:00",
        blinds={"sb": 100, "bb": 200},
        board=["Ah", "Kd", "Qh"],
        board_source="rfid",
        players=[
            {"seat": 1, "name": "Sato",  "stack_start": 10000, "stack_end": 9200, "result": -800},
            {"seat": 2, "name": "Tanaka", "stack_start": 10000, "stack_end": 10800, "result": 800},
        ],
        pot_total=800,
        winner_seat=2,
        actions=actions,
        review_required=False,
        button_seat=1,
        position_map={1: "BTN", 2: "SB"},
    )


# ――― _append_jsonl (helper) ―――

class TestAppendJsonl:
    def test_creates_file_and_appends(self, tmp_path: Path):
        path = tmp_path / "test.jsonl"
        _append_jsonl(path, {"key": "value1"})
        _append_jsonl(path, {"key": "value2"})

        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["key"] == "value1"
        assert json.loads(lines[1])["key"] == "value2"

    def test_creates_parent_dirs(self, tmp_path: Path):
        path = tmp_path / "subdir" / "nested" / "data.jsonl"
        _append_jsonl(path, {"x": 1})
        assert path.exists()

    def test_each_line_is_valid_json(self, tmp_path: Path):
        path = tmp_path / "data.jsonl"
        for i in range(5):
            _append_jsonl(path, {"index": i, "val": f"item_{i}"})

        for line in path.read_text(encoding="utf-8").splitlines():
            obj = json.loads(line)
            assert "index" in obj


# ――― JsonWriter.append_action ―――

class TestAppendAction:
    def test_creates_actions_jsonl(self, tmp_path: Path):
        writer = JsonWriter(log_dir=tmp_path / "logs", session_id="sess1")
        action = _make_action()
        writer.append_action(action)

        assert writer.actions_path.exists()
        lines = writer.actions_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        obj = json.loads(lines[0])
        assert obj["action"] == "raise"
        assert obj["amount"] == 800
        assert obj["position"] == "BTN"
        assert obj["actor_confidence"] == pytest.approx(0.85)

    def test_multiple_actions_appended_in_order(self, tmp_path: Path):
        writer = JsonWriter(log_dir=tmp_path / "logs", session_id="sess1")
        for i, act in enumerate(["check", "bet", "fold"]):
            writer.append_action(_make_action(action=act, amount=i * 100))

        lines = writer.actions_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 3
        assert json.loads(lines[0])["action"] == "check"
        assert json.loads(lines[1])["action"] == "bet"
        assert json.loads(lines[2])["action"] == "fold"

    def test_does_not_affect_main_json(self, tmp_path: Path):
        """append_action は whole-JSON ファイルを変更しない。"""
        writer = JsonWriter(log_dir=tmp_path / "logs", session_id="sess1")
        writer.append_action(_make_action())

        # main .json ファイルは存在しないか空の hands
        if writer.path.exists():
            data = json.loads(writer.path.read_text(encoding="utf-8"))
            assert data["hands"] == []

    def test_actions_path_property(self, tmp_path: Path):
        writer = JsonWriter(log_dir=tmp_path / "logs", session_id="mysess")
        assert writer.actions_path == tmp_path / "logs" / "mysess_actions.jsonl"

    def test_immediate_flush_survives_second_writer(self, tmp_path: Path):
        """JSON Lines は即時フラッシュされるため、別インスタンスでも読み取れる。"""
        log_dir = tmp_path / "logs"
        writer1 = JsonWriter(log_dir=log_dir, session_id="sess1")
        writer1.append_action(_make_action())

        writer2 = JsonWriter(log_dir=log_dir, session_id="sess1")
        lines = writer2.actions_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1


# ――― JsonWriter.write_hand_summary ―――

class TestWriteHandSummary:
    def test_creates_hands_jsonl(self, tmp_path: Path):
        writer = JsonWriter(log_dir=tmp_path / "logs", session_id="sess1")
        summary = _make_summary(hand_id=1)
        writer.write_hand_summary(summary)

        assert writer.hands_path.exists()
        lines = writer.hands_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        obj = json.loads(lines[0])
        assert obj["hand_id"] == 1
        assert obj["winner_seat"] == 2
        assert obj["button_seat"] == 1

    def test_multiple_hands_appended(self, tmp_path: Path):
        writer = JsonWriter(log_dir=tmp_path / "logs", session_id="sess1")
        for i in range(1, 4):
            writer.write_hand_summary(_make_summary(hand_id=i))

        lines = writer.hands_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 3
        for i, line in enumerate(lines):
            assert json.loads(line)["hand_id"] == i + 1

    def test_hands_path_property(self, tmp_path: Path):
        writer = JsonWriter(log_dir=tmp_path / "logs", session_id="mysess")
        assert writer.hands_path == tmp_path / "logs" / "mysess_hands.jsonl"

    def test_actions_included_in_summary(self, tmp_path: Path):
        writer = JsonWriter(log_dir=tmp_path / "logs", session_id="sess1")
        summary = _make_summary(actions=[_make_action("call", 200)])
        writer.write_hand_summary(summary)

        obj = json.loads(writer.hands_path.read_text(encoding="utf-8").splitlines()[0])
        assert len(obj["actions"]) == 1
        assert obj["actions"][0]["action"] == "call"

    def test_does_not_affect_main_json(self, tmp_path: Path):
        """write_hand_summary は whole-JSON ファイルを変更しない。"""
        writer = JsonWriter(log_dir=tmp_path / "logs", session_id="sess1")
        writer.write_hand_summary(_make_summary())

        if writer.path.exists():
            data = json.loads(writer.path.read_text(encoding="utf-8"))
            assert data["hands"] == []

    def test_coexists_with_append_hand_summary(self, tmp_path: Path):
        """新旧メソッドが同一セッションで共存できる。"""
        writer = JsonWriter(log_dir=tmp_path / "logs", session_id="sess1")
        summary = _make_summary(hand_id=5)
        writer.append_hand_summary(summary)   # whole-JSON
        writer.write_hand_summary(summary)    # JSON Lines

        # whole-JSON ファイル
        whole = json.loads(writer.path.read_text(encoding="utf-8"))
        assert len(whole["hands"]) == 1

        # JSON Lines ファイル
        lines = writer.hands_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["hand_id"] == 5


# ――― PHHExporter.export_hand ―――

class TestExportHand:
    def test_returns_string(self):
        summary = _make_summary()
        exporter = PHHExporter()
        result = exporter.export_hand(summary)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_contains_variant_nt(self):
        summary = _make_summary()
        exporter = PHHExporter()
        result = exporter.export_hand(summary)
        assert "NT" in result

    def test_contains_player_names(self):
        summary = _make_summary()
        exporter = PHHExporter()
        result = exporter.export_hand(summary)
        assert "Sato" in result
        assert "Tanaka" in result

    def test_contains_blinds(self):
        summary = _make_summary()
        exporter = PHHExporter()
        result = exporter.export_hand(summary)
        # SB=100, BB=200 が入っている
        assert "100" in result
        assert "200" in result

    def test_contains_starting_stacks(self):
        summary = _make_summary()
        exporter = PHHExporter()
        result = exporter.export_hand(summary)
        assert "10000" in result

    def test_contains_actions(self):
        summary = _make_summary(actions=[_make_action("raise", 800)])
        exporter = PHHExporter()
        result = exporter.export_hand(summary)
        # PHH: raise → cbr
        assert "cbr" in result

    def test_fold_action(self):
        summary = _make_summary(actions=[_make_action("fold")])
        exporter = PHHExporter()
        result = exporter.export_hand(summary)
        assert " f" in result or "'p0 f'" in result or '"p0 f"' in result

    def test_empty_players_falls_back(self):
        """players が空の場合もクラッシュしない。"""
        summary = HandSummary(
            hand_id=1, session_id="s", started_at="", ended_at="",
            blinds={"sb": 100, "bb": 200}, board=[], board_source="",
            players=[], pot_total=0, winner_seat=0, actions=[], review_required=False,
        )
        exporter = PHHExporter()
        result = exporter.export_hand(summary)
        assert isinstance(result, str)

    def test_export_hand_vs_export_consistency(self):
        """export_hand と export は同じ必須フィールドを含む。"""
        summary = _make_summary()
        exporter = PHHExporter()
        hand_result = exporter.export_hand(summary)
        old_result = exporter.export(summary)

        # 両方に必須フィールドが含まれている
        for field in ("NT", "starting_stacks", "actions"):
            assert field in hand_result, f"export_hand missing: {field}"
            assert field in old_result, f"export missing: {field}"

    def test_hole_cards_unknown(self):
        """ホールカードは常に不明 ('????') として扱う。"""
        summary = _make_summary()
        exporter = PHHExporter()
        result = exporter.export_hand(summary)
        assert "????" in result

    def test_review_note_included(self):
        summary = _make_summary()
        summary.review_required = True
        exporter = PHHExporter()
        result = exporter.export_hand(summary)
        assert "review" in result.lower() or "note" in result.lower()

    def test_author_included(self):
        summary = _make_summary()
        exporter = PHHExporter(author="TestBot")
        result = exporter.export_hand(summary)
        assert "TestBot" in result
