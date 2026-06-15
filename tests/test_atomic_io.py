"""tests/test_atomic_io.py

B2: atomic + fsync な JSON 書き込みヘルパ（core/atomic_io.py）のテスト。
"""
from __future__ import annotations

import json
from pathlib import Path

from core.atomic_io import atomic_write_json


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
