"""tests/test_atomic_io.py

B2: atomic + fsync な JSON 書き込みヘルパ（core/atomic_io.py）のテスト。
"""
from __future__ import annotations

import json
from pathlib import Path

from core.atomic_io import atomic_write_json, read_json_file


def test_writes_readable_json(tmp_path: Path):
    p = tmp_path / "x.json"
    atomic_write_json(p, {"a": 1, "ja": "あ"})
    assert json.loads(p.read_text(encoding="utf-8")) == {"a": 1, "ja": "あ"}


def test_creates_parent_dirs(tmp_path: Path):
    p = tmp_path / "nested" / "deep" / "x.json"
    atomic_write_json(p, {"ok": True})
    assert p.exists()


def test_overwrites_and_leaves_no_tmp(tmp_path: Path):
    p = tmp_path / "x.json"
    atomic_write_json(p, {"v": 1})
    atomic_write_json(p, {"v": 2})
    assert json.loads(p.read_text()) == {"v": 2}
    # 中途半端な .tmp を残さない
    assert list(tmp_path.glob("*.tmp")) == []


def test_non_ascii_preserved(tmp_path: Path):
    p = tmp_path / "x.json"
    atomic_write_json(p, {"name": "山田 太郎"})
    assert "山田 太郎" in p.read_text(encoding="utf-8")


# ――― read_json_file / 破損退避（B7）―――

def test_read_missing_returns_none(tmp_path: Path):
    assert read_json_file(tmp_path / "nope.json") is None


def test_read_valid_returns_dict(tmp_path: Path):
    p = tmp_path / "x.json"
    atomic_write_json(p, {"a": 1})
    assert read_json_file(p) == {"a": 1}


def test_corrupt_is_quarantined(tmp_path: Path):
    p = tmp_path / "ledger.json"
    p.write_text("{ this is not valid json", encoding="utf-8")
    assert read_json_file(p) is None
    # 元ファイルは退避され、上書き消失しないよう脇に残る。
    assert not p.exists()
    quarantined = list(tmp_path.glob("ledger.json.corrupt-*"))
    assert len(quarantined) == 1
    assert "not valid json" in quarantined[0].read_text(encoding="utf-8")


def test_corrupt_without_quarantine_keeps_file(tmp_path: Path):
    p = tmp_path / "x.json"
    p.write_text("broken", encoding="utf-8")
    assert read_json_file(p, quarantine=False) is None
    assert p.exists()  # 退避しない指定では元ファイルを残す
