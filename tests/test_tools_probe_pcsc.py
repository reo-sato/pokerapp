"""tests/test_tools_probe_pcsc.py

tools/probe_pcsc.py（実機 RFID PC/SC bring-up 診断）のテスト。

実機 / pyscard 無しで回せるよう、純粋ロジック（match/lint/analyze/format）と、DI シーム
（bridge_factory に MockPCSCBridge を注入する probe_connect / run_watch）だけを検証する。
pyscard ゲートのかかった `_cmd_*` は環境依存なので対象外。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import tools.probe_pcsc as probe
from core.events import RFIDEvent
from rfid.bridge import MockPCSCBridge
from rfid.card_master import CardMaster
from tools.probe_pcsc import (
    analyze_uid,
    format_event,
    get_pcsc_readers,
    lint_pcsc_readers,
    load_rfid_config,
    match_readers,
    probe_connect,
    reader_label,
    run_watch,
)


def _write_config(tmp_path: Path, pcsc_readers: list[dict]) -> Path:
    p = tmp_path / "config.json"
    p.write_text(
        json.dumps({"rfid": {"transport": "pcsc", "pcsc_readers": pcsc_readers}}),
        encoding="utf-8",
    )
    return p


# ――― config ロード ―――

class TestLoadConfig:
    def test_reads_explicit_path_without_copy(self, tmp_path: Path):
        cfg = {"rfid": {"transport": "pcsc", "pcsc_readers": [{"name": "R0"}]}}
        p = tmp_path / "config.json"
        p.write_text(json.dumps(cfg), encoding="utf-8")
        rfid = load_rfid_config(p)
        assert rfid["transport"] == "pcsc"
        # 明示パスを読むだけで repo の config.json を生成しない（非破壊）。
        assert not (tmp_path / "generated.json").exists()

    def test_default_config_has_pcsc_readers(self):
        # 同梱 config_default.json には pcsc_readers サンプルがある（ADR-0034）。
        rfid = load_rfid_config()
        assert isinstance(get_pcsc_readers(rfid), list)


class TestGetPcscReaders:
    def test_prefers_list(self):
        assert get_pcsc_readers({"pcsc_readers": [{"name": "A"}]}) == [{"name": "A"}]

    def test_ignores_http_dict(self):
        # HTTP 用 readers(dict) は PC/SC では採用しない（list でないので空, §4）。
        assert get_pcsc_readers({"readers": {"seat_1": {"role": "seat"}}}) == []

    def test_empty_when_absent(self):
        assert get_pcsc_readers({}) == []


# ――― match_readers (§3-4) ―――

class TestMatchReaders:
    def test_matched_missing_unconfigured(self):
        present = ["PN5180-CCID [Interface 0]", "PN5180-CCID [Interface 1]", "Other Reader"]
        cfgs = [
            {"name": "PN5180-CCID [Interface 0]", "role": "seat", "seat": 1},
            {"name": "PN5180-CCID [Interface 9]", "role": "board", "index": 1},  # 未接続
        ]
        r = match_readers(present, cfgs)
        assert [c["seat"] for c in r.matched] == [1]
        assert [c["name"] for c in r.missing] == ["PN5180-CCID [Interface 9]"]
        assert "Other Reader" in r.unconfigured
        assert "PN5180-CCID [Interface 1]" in r.unconfigured

    def test_equality_not_prefix(self):
        # §4: 等値照合。前方一致では matched にしない。
        present = ["PN5180-CCID [Interface 0] 00 00"]
        cfgs = [{"name": "PN5180-CCID [Interface 0]", "role": "seat", "seat": 1}]
        r = match_readers(present, cfgs)
        assert r.matched == []
        assert r.missing == cfgs


# ――― lint_pcsc_readers (§4) ―――

class TestLint:
    def test_valid_passes(self):
        cfgs = [
            {"name": "R0", "role": "seat", "seat": 1},
            {"name": "R1", "role": "board", "index": 1},
            {"name": "R2", "role": "board"},  # index 任意
        ]
        assert lint_pcsc_readers(cfgs) == []

    def test_empty_flagged(self):
        assert lint_pcsc_readers([]) != []

    def test_duplicate_name(self):
        cfgs = [
            {"name": "R0", "role": "seat", "seat": 1},
            {"name": "R0", "role": "seat", "seat": 2},
        ]
        assert any("重複" in p for p in lint_pcsc_readers(cfgs))

    def test_duplicate_seat(self):
        cfgs = [
            {"name": "R0", "role": "seat", "seat": 1},
            {"name": "R1", "role": "seat", "seat": 1},
        ]
        assert any("seat 1" in p for p in lint_pcsc_readers(cfgs))

    def test_bad_role(self):
        assert any("role" in p for p in lint_pcsc_readers([{"name": "R0", "role": "x"}]))

    def test_seat_out_of_range(self):
        cfgs = [{"name": "R0", "role": "seat", "seat": 0}]
        assert any("seat 1..8" in p for p in lint_pcsc_readers(cfgs))

    def test_missing_name(self):
        cfgs = [{"role": "seat", "seat": 1}]
        assert any("name" in p for p in lint_pcsc_readers(cfgs))

    def test_board_index_out_of_range(self):
        cfgs = [{"name": "R0", "role": "board", "index": 6}]
        assert any("index" in p for p in lint_pcsc_readers(cfgs))


# ――― analyze_uid (§7) ―――

class TestAnalyzeUid:
    def test_none_and_empty(self):
        assert analyze_uid(None).byte_length == 0
        assert analyze_uid(None).contract_length is False
        assert analyze_uid("").byte_length == 0

    def test_4_byte_contract(self):
        info = analyze_uid("04:AB:CD:EF")
        assert info.byte_length == 4
        assert info.contract_length is True

    def test_8_byte_iso15693(self):
        info = analyze_uid("04abcdef12345678")
        assert info.normalized == "04:AB:CD:EF:12:34:56:78"
        assert info.byte_length == 8
        assert info.contract_length is True

    def test_7_byte_type_a(self):
        assert analyze_uid("04:11:22:33:44:55:66").contract_length is True

    def test_non_contract_length_flagged(self):
        # 6 バイトは 4/7/8 のいずれでもない → 契約非適合（warn 対象）。
        info = analyze_uid("04:AB:CD:EF:12:34")
        assert info.byte_length == 6
        assert info.contract_length is False


# ――― reader_label ―――

class TestReaderLabel:
    def test_seat(self):
        assert reader_label({"role": "seat", "seat": 3}) == "seat 3"

    def test_board_with_index(self):
        assert reader_label({"role": "board", "index": 2}) == "board 2"

    def test_board_without_index(self):
        assert reader_label({"role": "board"}) == "board"


# ――― probe_connect (§5, DI で実機不要) ―――

class TestProbeConnect:
    def test_all_connect_ok(self):
        cfgs = [
            {"name": "R0", "role": "seat", "seat": 1},
            {"name": "R1", "role": "board", "index": 1},
        ]
        results = probe_connect(cfgs, bridge_factory=lambda name: MockPCSCBridge(name))
        assert [r.connected for r in results] == [True, True]
        assert [r.cfg["name"] for r in results] == ["R0", "R1"]

    def test_connect_failure_reported(self):
        def failing_factory(name: str):
            b = MockPCSCBridge(name)
            b.connect = lambda: False  # type: ignore[assignment]
            return b

        results = probe_connect(
            [{"name": "R0", "role": "seat", "seat": 1}],
            bridge_factory=failing_factory,
        )
        assert results[0].connected is False


# ――― format_event ―――

class TestFormatEvent:
    def _ev(self, tag_id: str, *, role="seat", seat=1, card="", board_index=None) -> RFIDEvent:
        return RFIDEvent(
            tag_id=tag_id, card=card, reader_id="reader_0", role=role, seat=seat,
            timestamp=0.0, raw_tag_id=tag_id, board_index=board_index,
        )

    def test_includes_role_uid_and_card(self, tmp_path: Path):
        cm = CardMaster(tmp_path / "cards.json")
        cm.register("04:AB:CD:EF:12:34:56:78", "Ah")
        line = format_event(self._ev("04abcdef12345678", role="seat", seat=2, card="Ah"), cm)
        assert "seat 2" in line
        assert "04:AB:CD:EF:12:34:56:78" in line
        assert "Ah" in line
        assert "⚠" not in line  # 8B は契約長

    def test_unregistered_card_marked(self, tmp_path: Path):
        cm = CardMaster(tmp_path / "cards.json")
        line = format_event(self._ev("04:AB:CD:EF", role="board", seat=None, board_index=1), cm)
        assert "board 1" in line
        assert "(未登録)" in line

    def test_non_contract_length_warns(self, tmp_path: Path):
        cm = CardMaster(tmp_path / "cards.json")
        line = format_event(self._ev("04:AB:CD:EF:12:34"), cm)  # 6B
        assert "⚠" in line


# ――― run_watch (実 RFIDThread + DI bridge, 実機不要) ―――

class TestRunWatch:
    def test_observes_tap_via_real_thread(self, tmp_path: Path):
        cm = CardMaster(tmp_path / "cards.json")
        cm.register("04:DD:EE:FF", "As")
        cfgs = [{"name": "reader_A", "role": "seat", "seat": 1}]
        # None → タッチ で 1 件発火（RFIDThread のデバウンス経路）。
        seqs = {"reader_A": [None, "04:DD:EE:FF", "04:DD:EE:FF"]}

        captured: list[str] = []
        seen = run_watch(
            cfgs, cm,
            seconds=0.5,
            poll_interval_ms=10,
            bridge_factory=lambda name: MockPCSCBridge(name, uid_sequence=seqs.get(name, [])),
            sink=captured.append,
        )
        assert seen >= 1
        assert any("seat 1" in line and "As" in line for line in captured)

    def test_zero_taps_when_no_card(self, tmp_path: Path):
        cm = CardMaster(tmp_path / "cards.json")
        cfgs = [{"name": "reader_A", "role": "seat", "seat": 1}]
        seen = run_watch(
            cfgs, cm,
            seconds=0.2,
            poll_interval_ms=10,
            bridge_factory=lambda name: MockPCSCBridge(name, uid_sequence=[None, None]),
            sink=lambda _line: None,
        )
        assert seen == 0


# ――― コマンド層（pyscard ゲートを差し替えて wiring を検証） ―――

class TestCommandsWithPyscardStubbed:
    def test_list_runs_and_reports_match(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(probe, "pyscard_available", lambda: True)
        cfg = _write_config(tmp_path, [{"name": "R0", "role": "seat", "seat": 1}])
        args = argparse.Namespace(config=str(cfg))
        rc = probe._cmd_list(args, lister=lambda: ["R0", "Extra Reader"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "matched" in out and "unconfigured" in out

    def test_check_pass_when_lint_clean_and_connects(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(probe, "pyscard_available", lambda: True)
        cfg = _write_config(tmp_path, [
            {"name": "R0", "role": "seat", "seat": 1},
            {"name": "R1", "role": "board", "index": 1},
        ])
        args = argparse.Namespace(config=str(cfg))
        rc = probe._cmd_check(args, bridge_factory=lambda name: MockPCSCBridge(name))
        assert rc == 0
        assert "PASS" in capsys.readouterr().out

    def test_check_fails_on_connect_failure(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(probe, "pyscard_available", lambda: True)
        cfg = _write_config(tmp_path, [{"name": "R0", "role": "seat", "seat": 1}])
        args = argparse.Namespace(config=str(cfg))

        def failing(name: str):
            b = MockPCSCBridge(name)
            b.connect = lambda: False  # type: ignore[assignment]
            return b

        rc = probe._cmd_check(args, bridge_factory=failing)
        assert rc == 1

    def test_check_fails_on_lint_error(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(probe, "pyscard_available", lambda: True)
        # seat 重複 = lint NG → connect が通っても全体は FAIL。
        cfg = _write_config(tmp_path, [
            {"name": "R0", "role": "seat", "seat": 1},
            {"name": "R1", "role": "seat", "seat": 1},
        ])
        args = argparse.Namespace(config=str(cfg))
        rc = probe._cmd_check(args, bridge_factory=lambda name: MockPCSCBridge(name))
        assert rc == 1

    def test_commands_gate_without_pyscard(self, tmp_path, monkeypatch):
        monkeypatch.setattr(probe, "pyscard_available", lambda: False)
        args = argparse.Namespace(config=None, seconds=0.1)
        assert probe._cmd_list(args) == 2
        assert probe._cmd_check(args) == 2
        assert probe._cmd_watch(args) == 2
