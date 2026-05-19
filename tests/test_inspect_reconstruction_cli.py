"""tests/test_inspect_reconstruction_cli.py

Phase 4-C1: ``output.inspect_reconstruction`` の CLI 動作を検証する。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from output.inspect_reconstruction import (
    format_entry,
    inspect,
    main,
)


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def _ok_entry(hand_id: int, **overrides) -> dict:
    base = {
        "hand_id": hand_id,
        "needs_review": False,
        "reason": "reconstructed_no_diff",
        "bootstrap_source": "online_summary",
        "bootstrap_meta": None,
        "diff": None,
        "confidence": 1.0,
        "online_summary": {"hand_id": hand_id},
        "offline_summary": {"hand_id": hand_id},
    }
    base.update(overrides)
    return base


def _review_entry(hand_id: int, diff: dict, **overrides) -> dict:
    base = {
        "hand_id": hand_id,
        "needs_review": True,
        "reason": "reconstructed_with_diff",
        "bootstrap_source": "raw",
        "bootstrap_meta": {"source": "raw"},
        "diff": diff,
        "confidence": 0.7,
        "online_summary": {"hand_id": hand_id},
        "offline_summary": {"hand_id": hand_id},
    }
    base.update(overrides)
    return base


def _skipped_entry(hand_id: int, **overrides) -> dict:
    base = {
        "hand_id": hand_id,
        "needs_review": False,
        "reason": "reconstruction_skipped",
        "bootstrap_source": None,
        "bootstrap_meta": None,
        "diff": None,
        "confidence": None,
        "online_summary": {"hand_id": hand_id},
        "offline_summary": None,
    }
    base.update(overrides)
    return base


# ────────────────────────────────────────────────────────────────────────────
# 1. 基本表示
# ────────────────────────────────────────────────────────────────────────────


class TestBasicListing:
    def test_three_hands_listed_in_hand_id_order(self, tmp_path: Path) -> None:
        path = tmp_path / "r.jsonl"
        # わざと逆順で書く (hand_id 昇順でソートされることを確認)
        _write_jsonl(path, [
            _skipped_entry(3),
            _review_entry(2, diff={
                "resolution_type": {"online": "showdown", "offline": "fold_win"},
                "seat_payouts":    {"online": {"1": 300}, "offline": {"2": 300}},
            }),
            _ok_entry(1),
        ])

        lines = inspect(path)
        assert len(lines) == 3
        assert lines[0].startswith("hand 1 [OK]")
        assert "bootstrap=online_summary" in lines[0]
        assert "reason=reconstructed_no_diff" in lines[0]
        # OK には diff_fields= は出ない
        assert "diff_fields=" not in lines[0]

        assert lines[1].startswith("hand 2 [REVIEW]")
        assert "bootstrap=raw" in lines[1]
        assert "reason=reconstructed_with_diff" in lines[1]
        assert "diff_fields=resolution_type,seat_payouts" in lines[1]

        assert lines[2].startswith("hand 3 [SKIPPED]")
        assert "bootstrap=None" in lines[2]
        assert "reason=reconstruction_skipped" in lines[2]

    def test_format_entry_with_no_diff_omits_diff_fields(self) -> None:
        line = format_entry(_ok_entry(7))
        assert line.startswith("hand 7 [OK]")
        assert "diff_fields=" not in line

    def test_format_entry_with_review_includes_diff_fields(self) -> None:
        line = format_entry(_review_entry(7, diff={
            "actions": {}, "pot_total": {},
        }))
        # alphabetical (sorted) ordering
        assert "diff_fields=actions,pot_total" in line


# ────────────────────────────────────────────────────────────────────────────
# 2. --only-needs-review filter
# ────────────────────────────────────────────────────────────────────────────


class TestOnlyNeedsReviewFilter:
    def test_only_review_hand_is_kept(self, tmp_path: Path) -> None:
        path = tmp_path / "r.jsonl"
        _write_jsonl(path, [
            _ok_entry(1),
            _review_entry(2, diff={
                "resolution_type": {"online": "showdown", "offline": "fold_win"},
            }),
            _skipped_entry(3),
        ])
        lines = inspect(path, only_needs_review=True)
        assert len(lines) == 1
        assert lines[0].startswith("hand 2 [REVIEW]")

    def test_only_needs_review_with_no_matches_returns_empty(
        self, tmp_path: Path,
    ) -> None:
        path = tmp_path / "r.jsonl"
        _write_jsonl(path, [_ok_entry(1), _skipped_entry(2)])
        lines = inspect(path, only_needs_review=True)
        assert lines == []


# ────────────────────────────────────────────────────────────────────────────
# 3. --fields filter
# ────────────────────────────────────────────────────────────────────────────


class TestFieldsFilter:
    def test_fields_filter_intersects_with_diff_keys(self, tmp_path: Path) -> None:
        path = tmp_path / "r.jsonl"
        _write_jsonl(path, [
            _review_entry(1, diff={
                "resolution_type": {"online": "showdown", "offline": "fold_win"},
                "seat_payouts":    {"online": {"1": 300}, "offline": {"2": 300}},
                "pot_total":       {"online": 300, "offline": 600},
            }),
        ])
        lines = inspect(path, only_fields=["resolution_type", "seat_payouts"])
        assert "diff_fields=resolution_type,seat_payouts" in lines[0]
        # pot_total はフィルタアウトされる
        assert "pot_total" not in lines[0]

    def test_fields_filter_no_intersection_omits_diff_fields_segment(
        self, tmp_path: Path,
    ) -> None:
        path = tmp_path / "r.jsonl"
        _write_jsonl(path, [
            _review_entry(1, diff={
                "resolution_type": {"online": "showdown", "offline": "fold_win"},
            }),
        ])
        # diff には resolution_type しかないので、winner_seat 指定では一致なし
        lines = inspect(path, only_fields=["winner_seat"])
        # diff_fields= は出ない (空集合)
        assert "diff_fields=" not in lines[0]
        # それでも基本情報は出る
        assert lines[0].startswith("hand 1 [REVIEW]")


# ────────────────────────────────────────────────────────────────────────────
# 4. label 判定
# ────────────────────────────────────────────────────────────────────────────


class TestStatusLabel:
    def test_offline_summary_none_yields_skipped(self, tmp_path: Path) -> None:
        """offline_summary が None なら reason が他であっても [SKIPPED]。"""
        path = tmp_path / "r.jsonl"
        _write_jsonl(path, [
            {"hand_id": 1, "needs_review": False, "reason": "reconstructed",
             "bootstrap_source": None, "diff": None, "offline_summary": None},
        ])
        lines = inspect(path)
        assert lines[0].startswith("hand 1 [SKIPPED]")

    def test_needs_review_true_yields_review_even_if_reason_other(
        self, tmp_path: Path,
    ) -> None:
        """needs_review=True なら reason が "reconstructed" でも [REVIEW]。"""
        path = tmp_path / "r.jsonl"
        _write_jsonl(path, [{
            "hand_id": 1, "needs_review": True, "reason": "reconstructed",
            "bootstrap_source": "raw", "diff": {"pot_total": {}},
            "offline_summary": {"hand_id": 1},
        }])
        lines = inspect(path)
        assert lines[0].startswith("hand 1 [REVIEW]")


# ────────────────────────────────────────────────────────────────────────────
# 5. argparse main() 経由
# ────────────────────────────────────────────────────────────────────────────


class TestMainCli:
    def test_main_prints_to_stdout(self, tmp_path: Path, capsys) -> None:
        path = tmp_path / "r.jsonl"
        _write_jsonl(path, [
            _ok_entry(1, bootstrap_source="raw"),
        ])
        ret = main(["--reconstruct", str(path), "--quiet"])
        assert ret == 0
        out = capsys.readouterr().out
        assert "hand 1 [OK]" in out
        assert "bootstrap=raw" in out
        assert "reason=reconstructed_no_diff" in out

    def test_main_with_only_needs_review_flag(
        self, tmp_path: Path, capsys,
    ) -> None:
        path = tmp_path / "r.jsonl"
        _write_jsonl(path, [
            _ok_entry(1),
            _review_entry(2, diff={"actions": {}}),
        ])
        ret = main([
            "--reconstruct", str(path),
            "--only-needs-review",
            "--quiet",
        ])
        assert ret == 0
        out = capsys.readouterr().out
        assert "hand 1" not in out
        assert "hand 2 [REVIEW]" in out

    def test_main_with_fields_flag(self, tmp_path: Path, capsys) -> None:
        path = tmp_path / "r.jsonl"
        _write_jsonl(path, [
            _review_entry(1, diff={
                "resolution_type": {}, "seat_payouts": {}, "pot_total": {},
            }),
        ])
        ret = main([
            "--reconstruct", str(path),
            "--fields", "resolution_type,seat_payouts",
            "--quiet",
        ])
        assert ret == 0
        out = capsys.readouterr().out
        assert "diff_fields=resolution_type,seat_payouts" in out
        assert "pot_total" not in out


# ────────────────────────────────────────────────────────────────────────────
# 6. 防御的: 壊れた行は skip
# ────────────────────────────────────────────────────────────────────────────


class TestRobustness:
    def test_invalid_json_line_is_skipped(self, tmp_path: Path) -> None:
        path = tmp_path / "r.jsonl"
        # 1 行目: 不正 JSON、2 行目: valid
        with path.open("w", encoding="utf-8") as f:
            f.write("{this is not json}\n")
            f.write(json.dumps(_ok_entry(5)) + "\n")
        lines = inspect(path)
        assert len(lines) == 1
        assert lines[0].startswith("hand 5 [OK]")

    def test_empty_file_yields_empty_listing(self, tmp_path: Path) -> None:
        path = tmp_path / "r.jsonl"
        path.write_text("", encoding="utf-8")
        assert inspect(path) == []
