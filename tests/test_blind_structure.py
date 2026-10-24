"""core/blind_structure.py: ストラクチャーの検証・保存と、トーナメントの時計（ADR-0071）。"""
from __future__ import annotations

import json

import pytest

from core.blind_structure import (
    BlindStructureStore,
    StructureValidationError,
    TournamentClock,
    parse_structure,
)

RAW = {
    "name": "テスト",
    "starting_stack": 20000,
    "levels": [
        {"sb": 100, "bb": 200, "ante": 0, "minutes": 20},
        {"sb": 200, "bb": 400, "ante": 400, "minutes": 20},
        {"break": True, "minutes": 10},
        {"sb": 300, "bb": 600, "ante": 500, "minutes": 15},
    ],
}


class TestParse:
    def test_levels_are_numbered_without_breaks(self):
        s = parse_structure(RAW)
        assert [lv.level for lv in s.levels] == [1, 2, None, 3]
        assert s.levels[3].ante == 500 and s.levels[3].bb == 600     # アンテの額は BB と別
        assert s.levels[2].is_break and s.levels[2].duration_sec == 600
        assert s.playing_index(3) == 3 and s.playing_index(4) is None

    def test_round_trip(self):
        assert parse_structure(parse_structure(RAW).to_dict()) == parse_structure(RAW)

    @pytest.mark.parametrize("change, message", [
        ({"name": "  "}, "名前"),
        ({"levels": []}, "1 行以上"),
        ({"levels": [{"break": True, "minutes": 5}]}, "通常のレベル"),
        ({"levels": [{"sb": 300, "bb": 200, "ante": 0, "minutes": 20}]}, "SB"),
        ({"levels": [{"sb": 100, "bb": 200, "ante": -1, "minutes": 20}]}, "ante"),
        ({"levels": [{"sb": 100, "bb": 200, "ante": 0, "minutes": 0}]}, "minutes"),
        ({"levels": [{"sb": 100, "bb": 200, "minutes": 20}, {"sb": 50, "bb": 100, "minutes": 20}]}, "前のレベル"),
        ({"starting_stack": -5}, "持ち点"),
    ])
    def test_rejects(self, change, message):
        with pytest.raises(StructureValidationError, match=message):
            parse_structure({**RAW, **change})

    def test_break_uses_previous_blinds(self):
        s = parse_structure(RAW)
        assert s.blinds_at(2).level == 2


class TestStore:
    def test_falls_back_to_sample_and_saves_to_own_file(self, tmp_path):
        sample = tmp_path / "sample.json"
        sample.write_text(json.dumps({"structures": [RAW]}), encoding="utf-8")
        store = BlindStructureStore(tmp_path / "mine.json", sample=sample)
        assert [s.name for s in store.list_structures()] == ["テスト"]
        store.save({**RAW, "name": "別"})
        data = json.loads((tmp_path / "mine.json").read_text(encoding="utf-8"))
        assert [s["name"] for s in data["structures"]] == ["テスト", "別"]
        assert json.loads(sample.read_text(encoding="utf-8"))["structures"][0]["name"] == "テスト"

    def test_replace_rename_delete(self, tmp_path):
        store = BlindStructureStore(tmp_path / "s.json")
        store.save(RAW)
        store.save({**RAW, "name": "新しい名前"}, previous_name="テスト")
        assert [s.name for s in store.list_structures()] == ["新しい名前"]
        assert store.delete("新しい名前") and not store.delete("無い")
        assert store.list_structures() == []

    def test_invalid_save_leaves_file(self, tmp_path):
        store = BlindStructureStore(tmp_path / "s.json")
        store.save(RAW)
        with pytest.raises(StructureValidationError):
            store.save({**RAW, "levels": []})
        assert len(store.list_structures()) == 1

    def test_reload_when_other_process_writes(self, tmp_path):
        path = tmp_path / "s.json"
        store = BlindStructureStore(path)
        other = BlindStructureStore(path)
        other.save(RAW)
        assert [s.name for s in store.list_structures()] == ["テスト"]

    def test_skips_broken_entries(self, tmp_path):
        path = tmp_path / "s.json"
        path.write_text(json.dumps({"structures": [RAW, {"name": "壊れた", "levels": []}]}), encoding="utf-8")
        assert [s.name for s in BlindStructureStore(path).list_structures()] == ["テスト"]

    def test_committed_sample_is_valid(self):
        from core.blind_structure import _SAMPLE_FILE

        data = json.loads(_SAMPLE_FILE.read_text(encoding="utf-8"))
        assert [parse_structure(s).name for s in data["structures"]]


class TestClock:
    def setup_method(self):
        self.s = parse_structure(RAW)

    def test_advances_at_deadline_and_across_levels(self):
        c = TournamentClock.start(self.s, 1000.0)
        assert c.index == 0 and c.remaining(1000.0) == 1200
        assert not c.tick(2199.0)
        assert c.tick(2200.0) and c.index == 1
        # 2 つ先まで一度に（20 分 + ブレイク 10 分）
        assert c.tick(2200.0 + 1200 + 600) and c.index == 3 and c.current.level == 3
        assert c.level_started_at == 2200.0 + 1800

    def test_stays_at_last_level(self):
        c = TournamentClock.start(self.s, 0.0, level=3)
        assert not c.tick(10_000.0) and c.is_last and c.remaining(10_000.0) == 0

    def test_start_mid_level(self):
        c = TournamentClock.start(self.s, 5000.0, level=2, elapsed_sec=300)
        assert c.index == 1 and c.remaining(5000.0) == 900
        # 経過がレベルの時間を越えていれば次へ
        c = TournamentClock.start(self.s, 5000.0, level=2, elapsed_sec=1300)
        assert c.index == 2 and c.current.is_break and c.remaining(5000.0) == 500

    def test_pause_and_resume(self):
        c = TournamentClock.start(self.s, 0.0)
        assert c.pause(600.0) and c.paused and c.ends_at() is None
        assert not c.tick(5000.0) and c.remaining(5000.0) == 600
        assert c.resume(5000.0) and c.ends_at() == 5600.0
        assert c.tick(5600.0) and c.index == 1

    def test_goto_keeps_pause(self):
        c = TournamentClock.start(self.s, 0.0)
        c.pause(100.0)
        c.goto(1, 200.0)
        assert c.paused and c.remaining(9999.0) == 1200

    def test_break_status_shows_previous_blinds_and_next_level(self):
        c = TournamentClock.start(self.s, 0.0, level=2, elapsed_sec=1200)
        st = c.status(0.0)
        assert st["is_break"] and (st["sb"], st["bb"], st["ante"]) == (200, 400, 400)
        assert st["next"] == {"level": 3, "sb": 300, "bb": 600, "ante": 500}

    def test_next_break(self):
        c = TournamentClock.start(self.s, 0.0)
        assert c.seconds_to_next_break(0.0) == 2400
        assert c.status(0.0)["next_break_at"] == 2400

    def test_round_trip(self):
        c = TournamentClock.start(self.s, 0.0, level=2, elapsed_sec=60)
        c.pause(100.0)
        back = TournamentClock.from_dict(json.loads(json.dumps(c.to_dict())))
        assert (back.index, back.paused_elapsed, back.structure) == (c.index, c.paused_elapsed, c.structure)
