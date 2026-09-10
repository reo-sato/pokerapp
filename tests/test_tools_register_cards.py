"""tests/test_tools_register_cards.py

tools/register_cards.py（実機カード UID のタップ駆動登録）のテスト。
pyscard / 実機なしで回せるよう、純粋ロジック（順序・未登録判定・表示・reader 選択）と、
MockPCSCBridge を注入した登録ループ（run_registration）を検証する。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import tools.register_cards as reg
from rfid.bridge import MockPCSCBridge
from rfid.card_master import CardMaster

# v1.2（ADR-0041）: reader 名は 1 つで、物理リーダーは `reader`（Get UID の P2）で選ぶ。
_READERS = [
    {"name": "CCID 0", "reader": 0, "role": "seat", "seat": 1},
    {"name": "CCID 0", "reader": 1, "role": "seat", "seat": 2},
    {"name": "CCID 0", "reader": 8, "role": "board", "index": 1, "cards": 3},
]


def _write_config(tmp_path: Path, pcsc_readers: list[dict] | None = None) -> Path:
    p = tmp_path / "config.json"
    p.write_text(
        json.dumps({"rfid": {"transport": "pcsc",
                             "pcsc_readers": pcsc_readers if pcsc_readers is not None else _READERS}}),
        encoding="utf-8",
    )
    return p


def _master(tmp_path: Path, entries: dict[str, str] | None = None) -> CardMaster:
    cm = CardMaster(tmp_path / "cards.json")
    for uid, code in (entries or {}).items():
        cm.register(uid, code)
    return cm


class TestDeckOrder:
    def test_suit_rank_is_54_unique_spades_first(self):
        order = reg.deck_order("suit-rank", jokers=2)
        assert len(order) == 54 and len(set(order)) == 54
        assert order[:3] == ["As", "2s", "3s"] and order[12] == "Ks" and order[13] == "Ah"
        assert order[-2:] == ["Jk", "JK"]

    def test_rank_suit_interleaves_suits(self):
        order = reg.deck_order("rank-suit", jokers=0)
        assert len(order) == 52 and order[:4] == ["As", "Ah", "Ad", "Ac"] and order[4] == "2s"

    def test_all_codes_are_valid_for_card_master(self):
        from rfid.card_master import VALID_CARDS
        assert set(reg.deck_order("suit-rank", 2)) <= VALID_CARDS

    def test_invalid_preset_or_jokers(self):
        with pytest.raises(ValueError):
            reg.deck_order("random")
        with pytest.raises(ValueError):
            reg.deck_order("suit-rank", jokers=3)


class TestPendingAndStatus:
    def test_pending_respects_deck_count(self):
        entries = {"U1": "Ah", "U2": "Ah", "U3": "Kd"}
        order = ["Ah", "Kd", "Qs"]
        assert reg.pending_codes(entries, order, deck=1) == ["Qs"]
        assert reg.pending_codes(entries, order, deck=2) == ["Kd", "Qs"]
        assert reg.pending_codes(entries, order, deck=3) == ["Ah", "Kd", "Qs"]

    def test_pending_rejects_deck_zero(self):
        with pytest.raises(ValueError):
            reg.pending_codes({}, ["Ah"], deck=0)

    def test_parse_codes_validates_and_dedups(self):
        assert reg.parse_codes("Ah, Kd,Ah,Jk") == ["Ah", "Kd", "Jk"]
        with pytest.raises(ValueError):
            reg.parse_codes("Ah,Zz")

    def test_format_status_reports_progress_and_extras(self):
        entries = {"U1": "Ah", "U2": "Ah", "U3": "Ah", "U4": "Kd", "U9": "Jk"}
        lines = reg.format_status(entries, ["Ah", "Kd", "Qs"], deck=2)
        assert lines[0].startswith("登録 UID 数: 5")
        assert "deck 1: 2/3 済" in lines[1] and "不足: Qs" in lines[1]
        assert "deck 2: 1/3 済" in lines[2] and "不足: Kd Qs" in lines[2]
        assert any("Ah×3" in ln for ln in lines)          # deck 数より多い
        assert any("順序外" in ln and "Jk" in ln for ln in lines)


class TestSelectReader:
    """`--reader` は config.rfid.pcsc_readers の要素を選ぶ（契約 v1.2 §4 / ADR-0041）。"""

    def test_default_is_first_element(self):
        assert reg.select_reader(_READERS) is _READERS[0]
        assert reg.select_reader(_READERS, None) is _READERS[0]
        assert reg.select_reader(_READERS, "  ") is _READERS[0]

    def test_select_by_index(self):
        assert reg.select_reader(_READERS, "1") is _READERS[1]
        assert reg.select_reader(_READERS, "2") is _READERS[2]

    def test_select_by_role_label(self):
        assert reg.select_reader(_READERS, "seat 2") is _READERS[1]
        assert reg.select_reader(_READERS, "SEAT  2") is _READERS[1]
        assert reg.select_reader(_READERS, "board 1") is _READERS[2]       # cards>1 の先頭位置
        assert reg.select_reader(_READERS, "board 1-3") is _READERS[2]
        assert reg.select_reader(_READERS, "seat 1 [r0]") is _READERS[0]

    def test_unknown_selector_lists_choices(self):
        with pytest.raises(ValueError) as e:
            reg.select_reader(_READERS, "seat 9")
        assert "seat 1" in str(e.value) and "board 1-3" in str(e.value)

    def test_index_out_of_range(self):
        with pytest.raises(ValueError):
            reg.select_reader(_READERS, "3")

    def test_empty_config_raises(self):
        with pytest.raises(ValueError):
            reg.select_reader([], None)


class TestRunRegistration:
    def test_registers_in_order_and_waits_for_release(self, tmp_path):
        cm = _master(tmp_path)
        # AA を置く→（乗ったまま 2 回読む）→離す→BB を置く→離す
        bridge = MockPCSCBridge("R", [None, "AA", "AA", "AA", None, "BB", None])
        out: list[str] = []
        res = reg.run_registration(bridge, cm, ["Ah", "Kd"], deck=1,
                                   sink=out.append, sleep=lambda _s: None, max_polls=20)
        assert res.registered == 2 and res.remaining == []
        assert cm.lookup("AA") == "Ah" and cm.lookup("BB") == "Kd"
        assert sum("✓" in ln for ln in out) == 2          # 置きっぱなしで二重登録しない

    def test_card_held_without_release_registers_once(self, tmp_path):
        cm = _master(tmp_path)
        bridge = MockPCSCBridge("R", ["AA", "AA", "AA"])   # 一度も離さない
        res = reg.run_registration(bridge, cm, ["Ah", "Kd"], deck=1,
                                   sink=lambda _l: None, sleep=lambda _s: None, max_polls=10)
        assert res.registered == 1 and res.remaining == ["Kd"]
        assert cm.lookup("AA") == "Ah"

    def test_uid_registered_to_other_code_is_rejected(self, tmp_path):
        cm = _master(tmp_path, {"AA": "Qs"})
        bridge = MockPCSCBridge("R", ["AA", None, "BB", None])
        out: list[str] = []
        res = reg.run_registration(bridge, cm, ["Ah"], deck=1,
                                   sink=out.append, sleep=lambda _s: None, max_polls=10)
        assert res.rejected == 1 and res.registered == 1 and res.remaining == []
        assert cm.lookup("AA") == "Qs" and cm.lookup("BB") == "Ah"
        assert any("⚠" in ln and "'Qs'" in ln for ln in out)

    def test_uid_already_registered_to_expected_code_is_idempotent(self, tmp_path):
        cm = _master(tmp_path, {"AA": "Ah"})
        bridge = MockPCSCBridge("R", ["AA", None])
        res = reg.run_registration(bridge, cm, ["Ah"], deck=1,
                                   sink=lambda _l: None, sleep=lambda _s: None, max_polls=5)
        assert res.skipped_done == 1 and res.registered == 0 and res.remaining == []
        assert len(cm) == 1

    def test_second_deck_maps_new_uid_to_same_code(self, tmp_path):
        cm = _master(tmp_path, {"AA": "Ah"})
        codes = reg.pending_codes(cm.all_entries(), ["Ah"], deck=2)
        assert codes == ["Ah"]
        bridge = MockPCSCBridge("R", ["BB", None])
        res = reg.run_registration(bridge, cm, codes, deck=2,
                                   sink=lambda _l: None, sleep=lambda _s: None, max_polls=5)
        assert res.registered == 1
        assert cm.lookup("AA") == "Ah" and cm.lookup("BB") == "Ah"
        assert reg.pending_codes(cm.all_entries(), ["Ah"], deck=2) == []

    def test_multiple_cards_are_not_registered_until_single(self, tmp_path):
        """重ね置き（契約 v1.1 §6）の間は登録せず警告し、1 枚になったら従来どおり登録する。"""
        cm = _master(tmp_path)
        bridge = MockPCSCBridge("R", [["AA", "BB"], ["AA", "BB"], "AA", None])
        out: list[str] = []
        res = reg.run_registration(bridge, cm, ["Ah"], deck=1,
                                   sink=out.append, sleep=lambda _s: None, max_polls=10)
        assert res.registered == 1 and res.remaining == []
        assert cm.lookup("AA") == "Ah" and cm.lookup("BB") == ""
        warns = [ln for ln in out if "枚検出" in ln]
        assert len(warns) == 1 and "2 枚検出" in warns[0]   # 状態が変わったときだけ 1 回

    def test_multiple_cards_only_never_registers(self, tmp_path):
        cm = _master(tmp_path)
        bridge = MockPCSCBridge("R", [["AA", "BB", "CC"]])
        res = reg.run_registration(bridge, cm, ["Ah"], deck=1,
                                   sink=lambda _l: None, sleep=lambda _s: None, max_polls=5)
        assert res.registered == 0 and res.remaining == ["Ah"] and len(cm) == 0

    def test_max_polls_stops_loop_with_remaining(self, tmp_path):
        cm = _master(tmp_path)
        bridge = MockPCSCBridge("R", [None])
        res = reg.run_registration(bridge, cm, ["Ah", "Kd"], deck=1,
                                   sink=lambda _l: None, sleep=lambda _s: None, max_polls=3)
        assert res.registered == 0 and res.remaining == ["Ah", "Kd"]


class TestCli:
    def test_parser_subcommands(self):
        p = reg.build_parser()
        a = p.parse_args(["run", "--deck", "2", "--order", "rank-suit", "--only", "Ah,Kd"])
        assert a.func is reg._cmd_run and a.deck == 2 and a.order == "rank-suit" and a.only == "Ah,Kd"
        assert p.parse_args(["list"]).func is reg._cmd_list
        assert p.parse_args(["unregister", "e0:04"]).func is reg._cmd_unregister

    def test_resolve_order_start_at_and_only(self):
        a = reg.build_parser().parse_args(["list", "--start-at", "Ks", "--jokers", "0"])
        order = reg._resolve_order(a)
        assert order[0] == "Ks" and order[1] == "Ah" and len(order) == 52 - 12
        b = reg.build_parser().parse_args(["list", "--only", "Kd,Ah"])
        assert reg._resolve_order(b) == ["Kd", "Ah"]

    def test_list_and_unregister_commands(self, tmp_path, capsys):
        cards = tmp_path / "cards.json"
        _master(tmp_path, {"E0:04:00:01": "Ah"}).__len__()
        rc = reg.main(["--cards-file", str(cards), "list", "--deck", "1", "--jokers", "0"])
        assert rc == 0
        assert "deck 1: 1/52 済" in capsys.readouterr().out
        rc = reg.main(["--cards-file", str(cards), "unregister", "e0040001"])
        assert rc == 0 and "解除: E0:04:00:01" in capsys.readouterr().out
        rc = reg.main(["--cards-file", str(cards), "unregister", "e0040001"])
        assert rc == 1

    def test_run_gates_without_pyscard(self, tmp_path, monkeypatch):
        monkeypatch.setattr(reg, "pyscard_available", lambda: False)
        rc = reg.main(["--cards-file", str(tmp_path / "c.json"), "run"])
        assert rc == 2

    def test_run_with_injected_bridge_registers(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(reg, "pyscard_available", lambda: True)
        cards = tmp_path / "cards.json"
        cfg = _write_config(tmp_path)
        a = reg.build_parser().parse_args(
            ["--config", str(cfg), "--cards-file", str(cards), "run",
             "--only", "Ah,Kd", "--poll-interval", "0"])
        factory = lambda name: MockPCSCBridge(name, ["AA", None, "BB", None])  # noqa: E731
        rc = reg._cmd_run(a, bridge_factory=factory)
        assert rc == 0
        cm = CardMaster(cards)
        assert cm.lookup("AA") == "Ah" and cm.lookup("BB") == "Kd"
        assert "登録 2 枚" in capsys.readouterr().out

    def test_run_uses_selected_config_element(self, tmp_path, monkeypatch, capsys):
        """`--reader` で選んだ要素の name / reader が PCSCBridge に渡る（v1.2 §4）。"""
        monkeypatch.setattr(reg, "pyscard_available", lambda: True)
        cards = tmp_path / "cards.json"
        cfg = _write_config(tmp_path)
        a = reg.build_parser().parse_args(
            ["--config", str(cfg), "--cards-file", str(cards), "run",
             "--only", "Ah", "--reader", "board 1", "--poll-interval", "0"])
        seen: list[tuple[str, int]] = []

        def factory(name: str, index: int):
            seen.append((name, index))
            return MockPCSCBridge(name, ["AA", None], index)

        rc = reg._cmd_run(a, bridge_factory=factory)
        out = capsys.readouterr().out
        assert rc == 0 and seen == [("CCID 0", 8)]
        assert "board 1-3 [r8]" in out and "物理リーダー 8" in out
        assert CardMaster(cards).lookup("AA") == "Ah"

    def test_run_defaults_to_first_config_element(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(reg, "pyscard_available", lambda: True)
        cfg = _write_config(tmp_path)
        a = reg.build_parser().parse_args(
            ["--config", str(cfg), "--cards-file", str(tmp_path / "c.json"), "run",
             "--only", "Ah", "--poll-interval", "0"])
        seen: list[tuple[str, int]] = []
        rc = reg._cmd_run(a, bridge_factory=lambda name, index: (
            seen.append((name, index)) or MockPCSCBridge(name, ["AA", None], index)))
        assert rc == 0 and seen == [("CCID 0", 0)]

    def test_run_rejects_unknown_reader_selector(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(reg, "pyscard_available", lambda: True)
        cfg = _write_config(tmp_path)
        a = reg.build_parser().parse_args(
            ["--config", str(cfg), "--cards-file", str(tmp_path / "c.json"), "run",
             "--only", "Ah", "--reader", "seat 9"])
        rc = reg._cmd_run(a, bridge_factory=lambda name, index: MockPCSCBridge(name))
        assert rc == 2
        assert "seat 9" in capsys.readouterr().err

    def test_run_with_stacked_cards_then_single(self, tmp_path, monkeypatch, capsys):
        """CLI 経路でも重ね置きは登録せず、1 枚になってから登録される（v1.1 §6）。"""
        monkeypatch.setattr(reg, "pyscard_available", lambda: True)
        cards = tmp_path / "cards.json"
        cfg = _write_config(tmp_path)
        a = reg.build_parser().parse_args(
            ["--config", str(cfg), "--cards-file", str(cards), "run",
             "--only", "Ah", "--reader", "0", "--poll-interval", "0"])
        factory = lambda name: MockPCSCBridge(name, [["AA", "BB"], "AA", None])  # noqa: E731
        rc = reg._cmd_run(a, bridge_factory=factory)
        out = capsys.readouterr().out
        assert rc == 0 and "2 枚検出" in out and "登録 1 枚" in out
        cm = CardMaster(cards)
        assert cm.lookup("AA") == "Ah" and cm.lookup("BB") == ""
