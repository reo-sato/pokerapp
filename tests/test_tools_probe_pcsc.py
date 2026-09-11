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
            {"name": "R1", "role": "board"},
            {"name": "R2", "role": "board"},
            {"name": "R3", "role": "board"},
        ]
        assert lint_pcsc_readers(cfgs) == []

    def test_empty_flagged(self):
        assert lint_pcsc_readers([]) != []

    def test_duplicate_name_is_allowed_when_reader_differs(self):
        """v1.2: reader 名は 1 つで物理リーダーは `reader` で選ぶので name 重複は正常。"""
        cfgs = [
            {"name": "R0", "reader": 0, "role": "seat", "seat": 1},
            {"name": "R0", "reader": 1, "role": "seat", "seat": 2},
        ]
        assert lint_pcsc_readers(cfgs) == []

    def test_duplicate_name_and_reader_pair_flagged(self):
        cfgs = [
            {"name": "R0", "reader": 2, "role": "seat", "seat": 1},
            {"name": "R0", "reader": 2, "role": "seat", "seat": 2},
        ]
        assert any("重複" in p and "reader 2" in p for p in lint_pcsc_readers(cfgs))

    def test_duplicate_name_without_reader_defaults_to_zero(self):
        # `reader` 省略は 0 扱い。省略同士 / 省略と reader:0 は重複。
        cfgs = [
            {"name": "R0", "role": "seat", "seat": 1},
            {"name": "R0", "reader": 0, "role": "seat", "seat": 2},
        ]
        assert any("重複" in p for p in lint_pcsc_readers(cfgs))

    def test_reader_index_range_and_type(self):
        for bad in (-1, 255, 300, "3", 1.5, True):
            problems = lint_pcsc_readers([{"name": "R0", "reader": bad, "role": "seat", "seat": 1}])
            assert any("reader は 0..254" in p for p in problems), bad

    def test_reader_index_valid_bounds(self):
        assert lint_pcsc_readers([{"name": "R0", "reader": 254, "role": "seat", "seat": 1}]) == []
        assert lint_pcsc_readers([{"name": "R0", "reader": 0, "role": "seat", "seat": 1}]) == []

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
        assert any("seat 1..9" in p for p in lint_pcsc_readers(cfgs))

    def test_missing_name(self):
        cfgs = [{"role": "seat", "seat": 1}]
        assert any("name" in p for p in lint_pcsc_readers(cfgs))

    def test_board_index_out_of_range(self):
        cfgs = [{"name": "R0", "role": "board", "index": 6}]
        assert any("index" in p for p in lint_pcsc_readers(cfgs))

    def test_production_11_reader_layout_passes(self):
        """本番構成: 1 reader 名 × 物理 11 台（席 8 + board 3）が lint を通る（v1.2 §4）。"""
        name = "PokerRFID PN5180-CCID 0"
        cfgs = [{"name": name, "reader": i, "role": "seat", "seat": i + 1} for i in range(8)]
        cfgs += [
            {"name": name, "reader": 8, "role": "board"},
            {"name": name, "reader": 9, "role": "board"},
            {"name": name, "reader": 10, "role": "board"},
        ]
        assert lint_pcsc_readers(cfgs) == []

    def test_shipped_default_config_passes_lint(self):
        """同梱 config_default.json の pcsc_readers サンプルが lint を通る（drift 検知）。"""
        # config.json（ローカル生成物）ではなく同梱テンプレートを明示的に読む。
        cfgs = get_pcsc_readers(load_rfid_config(probe._CONFIG_DEFAULT_JSON))
        assert len(cfgs) == 11
        assert lint_pcsc_readers(cfgs) == []
        assert {c["reader"] for c in cfgs} == set(range(11))
        assert len({c["name"] for c in cfgs}) == 1     # reader 名は 1 つだけ（ADR-0041）


class TestLintBoardGroup:
    """board reader 群の検査（契約 v1.3 §4 / ADR-0042: 位置は config に書かない）。"""

    def test_board_without_position_fields_passes(self):
        cfgs = [
            {"name": "R0", "role": "board"},
            {"name": "R1", "role": "board"},
            {"name": "R2", "role": "board"},
        ]
        assert lint_pcsc_readers(cfgs) == []

    def test_obsolete_index_flagged(self):
        problems = lint_pcsc_readers([
            {"name": "R0", "role": "board", "index": 1},
            {"name": "R1", "role": "board"},
        ])
        assert any("index" in p and "廃止" in p for p in problems)

    def test_obsolete_cards_flagged(self):
        problems = lint_pcsc_readers([
            {"name": "R0", "role": "board", "cards": 3},
            {"name": "R1", "role": "board"},
        ])
        assert any("cards" in p and "廃止" in p for p in problems)

    def test_both_obsolete_fields_reported_in_one_line(self):
        problems = [
            p for p in lint_pcsc_readers([
                {"name": "R0", "role": "board", "index": 1, "cards": 3},
                {"name": "R1", "role": "board"},
            ]) if "廃止" in p
        ]
        assert len(problems) == 1
        assert "index / cards" in problems[0]

    def test_single_board_reader_warns_about_five_cards(self):
        """board reader 1 台では 5 枚を重ねることになり給電不足で読めない可能性が高い。"""
        problems = lint_pcsc_readers([
            {"name": "R0", "role": "seat", "seat": 1},
            {"name": "R1", "role": "board"},
        ])
        assert any("board reader が 1 台" in p for p in problems)

    def test_no_board_reader_is_not_flagged(self):
        """board なし（席だけ）の構成は lint 対象外（RFID でボードを読まない運用）。"""
        assert lint_pcsc_readers([{"name": "R0", "role": "seat", "seat": 1}]) == []


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

    def test_config_board_has_no_position_in_label(self):
        # config 由来の board reader は位置を持たない（位置は検出順で決まる, v1.3 §4）。
        assert reader_label({"role": "board"}) == "board"

    def test_event_board_index_is_shown(self):
        # format_event が RFIDEvent.board_index を `index` として渡す経路。実際に割り当てられた
        # 位置を表示する（config に書く位置ではない）。
        assert reader_label({"role": "board", "index": 2}) == "board 2"
        assert reader_label({"role": "board", "index": 5, "reader": 10}) == "board 5 [r10]"

    def test_reader_index_suffix_only_when_specified(self):
        # v1.2: 物理リーダー index は `[r3]` で添える。未指定なら従来表示のまま。
        assert reader_label({"role": "seat", "seat": 1, "reader": 3}) == "seat 1 [r3]"
        assert reader_label({"role": "board", "reader": 8}) == "board [r8]"
        assert reader_label({"role": "board", "index": 3, "reader": 8}) == "board 3 [r8]"
        assert reader_label({"role": "seat", "seat": 1, "reader": 0}) == "seat 1 [r0]"
        assert reader_label({"role": "seat", "seat": 1, "reader": None}) == "seat 1"


# ――― format_uid_payload (raw, §6 v1.1) ―――

class TestFormatUidPayload:
    _A = [0xE0, 0x04, 0x00, 0x00, 0x00, 0x00, 0x00, 0x01]
    _B = [0xE0, 0x04, 0x00, 0x00, 0x00, 0x00, 0x00, 0x02]

    def test_single_uid_is_plain_hex(self):
        assert probe.format_uid_payload(self._A) == "E0 04 00 00 00 00 00 01"

    def test_two_stacked_uids_are_split(self):
        line = probe.format_uid_payload(self._A + self._B)
        assert line == "UID×2 = E0:04:00:00:00:00:00:01, E0:04:00:00:00:00:00:02"

    def test_three_stacked_uids(self):
        assert probe.format_uid_payload(self._A + self._B + self._A).startswith("UID×3 = ")

    def test_short_uid_and_empty(self):
        assert probe.format_uid_payload([0x04, 0xAB, 0xCD, 0xEF]) == "04 AB CD EF"
        assert probe.format_uid_payload([]) == ""


# ――― probe_connect (§5, DI で実機不要) ―――

class _SwBridge(MockPCSCBridge):
    """`probe()` で任意の SW を返す mock（`check` の PASS/FAIL 判定用）。"""

    def __init__(self, reader_name: str, reader_index: int = 0,
                 sw: tuple[int, int] | None = (0x6A, 0x81)):
        super().__init__(reader_name, None, reader_index)
        self._sw = sw

    def probe(self):
        return self._sw


class TestProbeConnect:
    def test_all_connect_ok(self):
        cfgs = [
            {"name": "R0", "role": "seat", "seat": 1},
            {"name": "R1", "role": "board", "index": 1},
        ]
        results = probe_connect(cfgs, bridge_factory=lambda name: MockPCSCBridge(name))
        assert [r.connected for r in results] == [True, True]
        assert [r.cfg["name"] for r in results] == ["R0", "R1"]
        assert [r.sw for r in results] == [None, None]   # probe を持たない bridge

    def test_reader_index_is_passed_to_factory(self):
        seen: list[tuple[str, int]] = []

        def factory(name: str, index: int):
            seen.append((name, index))
            return MockPCSCBridge(name, None, index)

        probe_connect(
            [{"name": "R0", "reader": 3, "role": "seat", "seat": 1},
             {"name": "R0", "role": "seat", "seat": 2}],
            bridge_factory=factory,
        )
        assert seen == [("R0", 3), ("R0", 0)]

    def test_sw_is_captured_from_probe(self):
        results = probe_connect(
            [{"name": "R0", "reader": 1, "role": "seat", "seat": 1}],
            bridge_factory=lambda name, index: _SwBridge(name, index, (0x90, 0x00)),
        )
        assert results[0].sw == (0x90, 0x00)


class TestCheckVerdict:
    """connect + Get UID の PASS/FAIL 判定（契約 v1.2 §5-6）。"""

    def _result(self, connected=True, sw=None, reader=0):
        return probe.ConnectResult(
            cfg={"name": "R0", "reader": reader, "role": "seat", "seat": 1},
            connected=connected, sw=sw,
        )

    def test_connect_failure_is_fail(self):
        ok, detail = probe.check_verdict(self._result(connected=False))
        assert ok is False and "connect" in detail

    def test_no_sw_passes_on_connect(self):
        assert probe.check_verdict(self._result(sw=None)) == (True, "")

    def test_card_present_and_absent_pass(self):
        assert probe.check_verdict(self._result(sw=(0x90, 0x00)))[0] is True
        assert probe.check_verdict(self._result(sw=(0x6A, 0x81)))[0] is True

    def test_wrong_reader_index_fails(self):
        ok, detail = probe.check_verdict(self._result(sw=(0x6A, 0x86), reader=9))
        assert ok is False and "reader 9" in detail and "6A86" in detail

    def test_unexpected_sw_fails(self):
        ok, detail = probe.check_verdict(self._result(sw=(0x6D, 0x00)))
        assert ok is False and "6D00" in detail

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

    def test_reader_index_shown_when_given(self, tmp_path: Path):
        cm = CardMaster(tmp_path / "cards.json")
        line = format_event(self._ev("04:AB:CD:EF", seat=3), cm, 7)
        assert "seat 3 [r7]" in line
        assert "[r" not in format_event(self._ev("04:AB:CD:EF", seat=3), cm)


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

    def test_labels_carry_physical_reader_index(self, tmp_path: Path):
        """v1.2: 1 つの reader 名を共有する 2 台を `[r0]` / `[r8]` で見分けられる。"""
        cm = CardMaster(tmp_path / "cards.json")
        cfgs = [
            {"name": "CCID 0", "reader": 0, "role": "seat", "seat": 1},
            {"name": "CCID 0", "reader": 8, "role": "board"},
        ]
        captured: list[str] = []
        run_watch(
            cfgs, cm,
            seconds=0.4,
            poll_interval_ms=10,
            bridge_factory=lambda name, index: MockPCSCBridge(
                name, [None, f"04:0{index}"], index),
            sink=captured.append,
        )
        assert any("seat 1 [r0]" in line for line in captured)
        assert any("board 1 [r8]" in line for line in captured)

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
        rc = probe._cmd_list(args, lister=lambda: ["R0", "Extra Reader"],
                             counter=lambda _name: None)
        out = capsys.readouterr().out
        assert rc == 0
        assert "matched" in out and "unconfigured" in out

    def test_list_shows_physical_reader_count(self, tmp_path, monkeypatch, capsys):
        """v1.2: `FF CA 00 FF 00` で得た台数 N を表示する（非対応 firmware は注記）。"""
        monkeypatch.setattr(probe, "pyscard_available", lambda: True)
        cfg = _write_config(tmp_path, [
            {"name": "R0", "reader": 0, "role": "seat", "seat": 1},
            {"name": "R0", "reader": 1, "role": "seat", "seat": 2},
        ])
        args = argparse.Namespace(config=str(cfg))
        rc = probe._cmd_list(args, lister=lambda: ["R0"], counter=lambda _n: 2)
        out = capsys.readouterr().out
        assert rc == 0
        assert "physical readers: 2" in out
        assert "[r0]" in out and "[r1]" in out
        assert "⚠" not in out

    def test_list_warns_when_reader_index_exceeds_count(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(probe, "pyscard_available", lambda: True)
        cfg = _write_config(tmp_path, [
            {"name": "R0", "reader": 0, "role": "seat", "seat": 1},
            {"name": "R0", "reader": 5, "role": "seat", "seat": 2},
        ])
        args = argparse.Namespace(config=str(cfg))
        probe._cmd_list(args, lister=lambda: ["R0"], counter=lambda _n: 2)
        out = capsys.readouterr().out
        assert "⚠ reader 5 は firmware の台数 2 を超えている" in out

    def test_list_notes_when_count_query_unsupported(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(probe, "pyscard_available", lambda: True)
        cfg = _write_config(tmp_path, [{"name": "R0", "reader": 9, "role": "seat", "seat": 1}])
        args = argparse.Namespace(config=str(cfg))
        probe._cmd_list(args, lister=lambda: ["R0"], counter=lambda _n: None)
        out = capsys.readouterr().out
        assert "台数問い合わせ非対応" in out
        assert "⚠" not in out          # 台数不明なら超過警告は出さない

    def test_check_pass_when_lint_clean_and_connects(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(probe, "pyscard_available", lambda: True)
        cfg = _write_config(tmp_path, [
            {"name": "R0", "role": "seat", "seat": 1},
            {"name": "R1", "role": "board"},
            {"name": "R2", "role": "board"},
            {"name": "R3", "role": "board"},
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

    def test_check_passes_with_card_absent_sw(self, tmp_path, monkeypatch, capsys):
        """カード無し（6A81）でも connect + Get UID が通れば PASS（§5-6, カード不要）。"""
        monkeypatch.setattr(probe, "pyscard_available", lambda: True)
        cfg = _write_config(tmp_path, [
            {"name": "R0", "reader": 0, "role": "seat", "seat": 1},
            {"name": "R0", "reader": 1, "role": "seat", "seat": 2},
        ])
        args = argparse.Namespace(config=str(cfg))
        rc = probe._cmd_check(args, bridge_factory=lambda n, k: _SwBridge(n, k, (0x6A, 0x81)))
        out = capsys.readouterr().out
        assert rc == 0 and "PASS ✅" in out and "カード無し" in out

    def test_check_fails_when_reader_index_out_of_range(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(probe, "pyscard_available", lambda: True)
        cfg = _write_config(tmp_path, [{"name": "R0", "reader": 9, "role": "seat", "seat": 1}])
        args = argparse.Namespace(config=str(cfg))
        rc = probe._cmd_check(args, bridge_factory=lambda n, k: _SwBridge(n, k, (0x6A, 0x86)))
        out = capsys.readouterr().out
        assert rc == 1 and "reader 9 は firmware の範囲外" in out

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
        args = argparse.Namespace(config=None, seconds=0.1, interval=0.1, reader=None)
        assert probe._cmd_list(args) == 2
        assert probe._cmd_check(args) == 2
        assert probe._cmd_watch(args) == 2
        assert probe._cmd_raw(args) == 2


class TestRawHelpers:
    """raw サブコマンドの純粋ヘルパ（pyscard 不要で検証できる部分）。"""

    TABLE = [("PRESENT", 0x20), ("EMPTY", 0x10), ("MUTE", 0x200), ("INUSE", 0x100)]

    def test_decode_state_joins_flag_names(self):
        assert probe.decode_reader_state(0x20 | 0x100, self.TABLE) == "PRESENT|INUSE"

    def test_decode_state_unknown_bits_fall_back_to_hex(self):
        assert probe.decode_reader_state(0x8000, self.TABLE) == "0x8000"

    def test_format_scard_error_includes_hresult_as_unsigned_hex(self):
        class Boom(Exception):
            hresult = -2146435060  # = 0x8010000C SCARD_E_NO_SMARTCARD（Windows は負値で返す）

        text = probe.format_scard_error(Boom("no card"))
        assert text.startswith("Boom: no card")
        assert "hresult=0x8010000c" in text

    def test_format_scard_error_without_hresult(self):
        assert probe.format_scard_error(ValueError("x")) == "ValueError: x"

    def test_scard_state_table_is_empty_without_pyscard(self, monkeypatch):
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *a, **kw):
            if name == "smartcard" or name.startswith("smartcard."):
                raise ImportError(name)
            return real_import(name, *a, **kw)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        assert probe.scard_state_table() == []

    def test_raw_subcommand_registered(self):
        args = probe.build_parser().parse_args(["raw", "--seconds", "5", "--interval", "0.2"])
        assert args.command == "raw" and args.seconds == 5.0 and args.interval == 0.2
        assert args.func is probe._cmd_raw
        assert args.reader == 0 and args.name is None   # 既定は物理リーダー 0 / config 先頭

    def test_raw_reader_index_and_name_options(self):
        # v1.2: `--reader` は Get UID の P2（物理リーダー index）、reader 名は `--name`。
        args = probe.build_parser().parse_args(["raw", "--reader", "7", "--name", "CCID 0"])
        assert args.reader == 7 and args.name == "CCID 0"


class TestGetUidApduP2:
    """`raw` / bridge が使う Get UID APDU の P2（契約 v1.2 §6）。"""

    def test_default_apdu_matches_v10(self):
        assert probe.GET_UID_APDU == [0xFF, 0xCA, 0x00, 0x00, 0x00]

    def test_p2_selects_physical_reader(self):
        from rfid.bridge import get_uid_apdu
        assert get_uid_apdu(10) == [0xFF, 0xCA, 0x00, 0x0A, 0x00]
