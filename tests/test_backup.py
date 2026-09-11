"""tests/test_backup.py

B2 (v1.0 ローンチレビュー): データバックアップ core のテスト。now 注入で決定的に世代を作る。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from core.backup import backup_data_files


def _touch(p: Path, text: str = "{}") -> Path:
    p.write_text(text, encoding="utf-8")
    return p


def test_backup_copies_existing_sources(tmp_path: Path):
    a = _touch(tmp_path / "ledger.json", '{"x":1}')
    b = _touch(tmp_path / "players.json", '{"players":[]}')
    missing = tmp_path / "auth_identity.json"  # 存在しない
    dest = tmp_path / "backups"
    target = backup_data_files([a, b, missing], dest, now=datetime(2026, 6, 15, 9, 0, 0))
    assert target == dest / "20260615-090000"
    assert (target / "ledger.json").read_text() == '{"x":1}'
    assert (target / "players.json").exists()
    assert not (target / "auth_identity.json").exists()  # 存在しないものは skip


def test_backup_no_sources_returns_none(tmp_path: Path):
    dest = tmp_path / "backups"
    assert backup_data_files([tmp_path / "nope.json"], dest) is None
    assert not dest.exists()


def test_backup_prunes_to_keep(tmp_path: Path):
    a = _touch(tmp_path / "ledger.json")
    dest = tmp_path / "backups"
    for h in (8, 9, 10, 11):  # 4 世代作る
        backup_data_files([a], dest, keep=2, now=datetime(2026, 6, 15, h, 0, 0))
    gens = sorted(p.name for p in dest.iterdir() if p.is_dir())
    # 最新 2 世代（10:00, 11:00）だけ残る
    assert gens == ["20260615-100000", "20260615-110000"]


def test_backup_keep_zero_does_not_prune(tmp_path: Path):
    a = _touch(tmp_path / "ledger.json")
    dest = tmp_path / "backups"
    backup_data_files([a], dest, keep=0, now=datetime(2026, 6, 15, 8, 0, 0))
    backup_data_files([a], dest, keep=0, now=datetime(2026, 6, 15, 9, 0, 0))
    gens = [p for p in dest.iterdir() if p.is_dir()]
    assert len(gens) == 2  # keep<=0 は剪定しない
