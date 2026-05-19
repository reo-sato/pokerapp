"""tests/test_patch_proposal.py

Phase 5-A: ``core.patch_proposal.compute_patch_proposal`` の単体テスト。
入力は ``_compute_diff`` の出力形式 (``{field: {"online": ..., "offline": ...}}``)
を直接組み立てて与える (HandSummary 全部を組まなくて済むので軽い)。
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

import pytest

from core.patch_proposal import (
    PATCHABLE_FIELDS,
    FieldPatch,
    HandPatchProposal,
    compute_patch_proposal,
)


# ────────────────────────────────────────────────────────────────────────────
# 単一フィールドケース (note 形成 + FieldPatch 値伝搬)
# ────────────────────────────────────────────────────────────────────────────


class TestSingleFieldPatches:
    def test_resolution_type_diff_yields_field_patch(self) -> None:
        diff = {"resolution_type": {"online": "fold_win", "offline": "showdown"}}
        proposal = compute_patch_proposal(
            hand_id=1, online=None, offline=None, diff=diff,
        )
        assert proposal is not None
        assert proposal.hand_id == 1
        assert proposal.can_patch_automatically is False  # Phase 5-A: 提案のみ
        assert len(proposal.fields) == 1
        fp = proposal.fields[0]
        assert fp.field == "resolution_type"
        assert fp.online == "fold_win"
        assert fp.offline == "showdown"
        # note は値を埋めた形に
        assert "resolution_type differs" in fp.note
        assert "fold_win" in fp.note
        assert "showdown" in fp.note

    def test_seat_payouts_diff_yields_field_patch(self) -> None:
        diff = {"seat_payouts": {
            "online":  {1: 300},
            "offline": {1: 150, 2: 150},
        }}
        proposal = compute_patch_proposal(
            hand_id=2, online=None, offline=None, diff=diff,
        )
        assert proposal is not None
        fp = proposal.fields[0]
        assert fp.field == "seat_payouts"
        assert fp.online == {1: 300}
        assert fp.offline == {1: 150, 2: 150}
        assert "seat_payouts differs" in fp.note

    def test_winner_seat_diff_yields_field_patch(self) -> None:
        diff = {"winner_seat": {"online": 1, "offline": 2}}
        proposal = compute_patch_proposal(
            hand_id=3, online=None, offline=None, diff=diff,
        )
        assert proposal is not None
        fp = proposal.fields[0]
        assert fp.field == "winner_seat"
        assert fp.online == 1
        assert fp.offline == 2
        assert "primary winner differs" in fp.note

    def test_pot_total_diff_yields_field_patch(self) -> None:
        diff = {"pot_total": {"online": 300, "offline": 600}}
        proposal = compute_patch_proposal(
            hand_id=4, online=None, offline=None, diff=diff,
        )
        assert proposal is not None
        fp = proposal.fields[0]
        assert fp.field == "pot_total"
        assert fp.online == 300
        assert fp.offline == 600
        assert "total pot differs" in fp.note

    def test_showdown_revealed_cards_diff_yields_field_patch(self) -> None:
        diff = {"showdown_revealed_cards": {
            "online":  {},
            "offline": {1: ["Ah", "Kh"]},
        }}
        proposal = compute_patch_proposal(
            hand_id=5, online=None, offline=None, diff=diff,
        )
        assert proposal is not None
        fp = proposal.fields[0]
        assert fp.field == "showdown_revealed_cards"
        assert "revealed hole cards differ" in fp.note


# ────────────────────────────────────────────────────────────────────────────
# 複数フィールド + 順序
# ────────────────────────────────────────────────────────────────────────────


class TestMultipleFieldsOrdering:
    def test_multiple_fields_in_patchable_order(self) -> None:
        """fields は PATCHABLE_FIELDS の宣言順で並ぶ (deterministic な出力)。"""
        # わざと diff の挿入順を逆にする (PATCHABLE_FIELDS の順序に揃うことを確認)
        diff = {
            "winner_seat":     {"online": 1, "offline": 2},
            "resolution_type": {"online": "fold_win", "offline": "showdown"},
            "pot_total":       {"online": 300, "offline": 600},
            "seat_payouts":    {"online": {1: 300}, "offline": {2: 600}},
        }
        proposal = compute_patch_proposal(
            hand_id=10, online=None, offline=None, diff=diff,
        )
        assert proposal is not None
        names = [fp.field for fp in proposal.fields]
        # PATCHABLE_FIELDS = (resolution_type, seat_payouts, winner_seat, pot_total, showdown_revealed_cards)
        assert names == ["resolution_type", "seat_payouts", "winner_seat", "pot_total"]

    def test_summary_note_lists_field_names(self) -> None:
        diff = {
            "resolution_type": {"online": "fold_win", "offline": "showdown"},
            "seat_payouts":    {"online": {1: 300}, "offline": {2: 600}},
        }
        proposal = compute_patch_proposal(
            hand_id=11, online=None, offline=None, diff=diff,
        )
        assert proposal is not None
        assert proposal.summary_note is not None
        assert "resolution_type" in proposal.summary_note
        assert "seat_payouts" in proposal.summary_note
        assert "differ" in proposal.summary_note


# ────────────────────────────────────────────────────────────────────────────
# Filter / negative cases
# ────────────────────────────────────────────────────────────────────────────


class TestNoProposalCases:
    def test_diff_none_returns_none(self) -> None:
        assert compute_patch_proposal(
            hand_id=1, online=None, offline=None, diff=None,
        ) is None

    def test_diff_empty_returns_none(self) -> None:
        assert compute_patch_proposal(
            hand_id=1, online=None, offline=None, diff={},
        ) is None

    def test_diff_only_unpatchable_field_returns_none(self) -> None:
        """``actions`` のような非対象 field しかない diff → proposal なし。"""
        diff = {"actions": {
            "online":  [(1, "fold", 0)],
            "offline": [(2, "fold", 0)],
        }}
        assert compute_patch_proposal(
            hand_id=1, online=None, offline=None, diff=diff,
        ) is None

    def test_unpatchable_fields_are_ignored_but_others_included(self) -> None:
        """``actions`` は無視されるが、対象 field は拾われる。"""
        diff = {
            "actions": {"online": [], "offline": []},
            "resolution_type": {"online": "fold_win", "offline": "showdown"},
        }
        proposal = compute_patch_proposal(
            hand_id=1, online=None, offline=None, diff=diff,
        )
        assert proposal is not None
        names = [fp.field for fp in proposal.fields]
        assert names == ["resolution_type"]

    def test_non_dict_diff_entry_skipped(self) -> None:
        """diff の値が dict でなければ defensively skip (壊れた JSON 等)。"""
        diff = {
            "resolution_type": "bogus",                          # 不正
            "seat_payouts":    {"online": {1: 300}, "offline": {2: 600}},
        }
        proposal = compute_patch_proposal(
            hand_id=1, online=None, offline=None, diff=diff,
        )
        assert proposal is not None
        # resolution_type は捨てられ、seat_payouts のみ
        names = [fp.field for fp in proposal.fields]
        assert names == ["seat_payouts"]


# ────────────────────────────────────────────────────────────────────────────
# Serialization (dataclasses.asdict との往復)
# ────────────────────────────────────────────────────────────────────────────


class TestSerialization:
    def test_asdict_roundtrip_preserves_shape(self) -> None:
        """``HandPatchProposal`` は asdict で plain dict 化できる (JSONL 用)。"""
        diff = {"resolution_type": {"online": "fold_win", "offline": "showdown"}}
        proposal = compute_patch_proposal(
            hand_id=1, online=None, offline=None, diff=diff,
        )
        d = asdict(proposal)
        assert d["hand_id"] == 1
        assert d["can_patch_automatically"] is False
        assert isinstance(d["fields"], list)
        assert len(d["fields"]) == 1
        f0 = d["fields"][0]
        assert f0["field"] == "resolution_type"
        assert f0["online"] == "fold_win"
        assert f0["offline"] == "showdown"
        assert isinstance(f0["note"], str)
