"""tests/test_player_credential_repository.py

ADR-0027 (L1): `PlayerCredentialRepository`（PIN ハッシュ + lockout + 永続）の単体テスト。
lockout は now 注入で決定的に検証する。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core.player_credential_repository import (
    PinLockedError,
    PinTooShortError,
    PlayerCredentialRepository,
)


def _repo(tmp_path: Path, **kw) -> PlayerCredentialRepository:
    return PlayerCredentialRepository(path=tmp_path / "creds.json",
                                      iterations=1000, **kw)  # iterations 小=高速


def test_set_and_verify(tmp_path: Path):
    repo = _repo(tmp_path)
    assert repo.has_pin("p1") is False
    repo.set_pin("p1", "1234")
    assert repo.has_pin("p1") is True
    assert repo.verify_pin("p1", "1234") is True
    assert repo.verify_pin("p1", "9999") is False


def test_unknown_player_returns_false(tmp_path: Path):
    repo = _repo(tmp_path)
    assert repo.verify_pin("nobody", "1234") is False


def test_too_short_pin_rejected(tmp_path: Path):
    repo = _repo(tmp_path, min_length=4)
    with pytest.raises(PinTooShortError):
        repo.set_pin("p1", "12")
    assert repo.has_pin("p1") is False


def test_hash_format_no_plaintext(tmp_path: Path):
    repo = _repo(tmp_path)
    repo.set_pin("p1", "secret-pin")
    raw = (tmp_path / "creds.json").read_text(encoding="utf-8")
    assert "secret-pin" not in raw
    assert "pbkdf2_sha256$" in raw


def test_persistence_across_instances(tmp_path: Path):
    _repo(tmp_path).set_pin("p1", "4321")
    reopened = _repo(tmp_path)
    assert reopened.verify_pin("p1", "4321") is True


def test_lockout_after_max_attempts(tmp_path: Path):
    repo = _repo(tmp_path, max_attempts=3, lockout_sec=300)
    repo.set_pin("p1", "1234")
    # 3 回失敗で lockout。now を固定して決定的に。
    for _ in range(3):
        assert repo.verify_pin("p1", "0000", now=1000) is False
    # lockout 中は正しい PIN でも PinLockedError。
    with pytest.raises(PinLockedError):
        repo.verify_pin("p1", "1234", now=1100)
    # lockout 期間が過ぎれば再び検証できる。
    assert repo.verify_pin("p1", "1234", now=1000 + 301) is True


def test_successful_verify_resets_failed_attempts(tmp_path: Path):
    repo = _repo(tmp_path, max_attempts=3)
    repo.set_pin("p1", "1234")
    assert repo.verify_pin("p1", "0000", now=1000) is False
    assert repo.verify_pin("p1", "0000", now=1000) is False
    assert repo.verify_pin("p1", "1234", now=1000) is True
    # reset 後はまた max_attempts 回まで失敗できる（直前の 2 回は数えない）。
    assert repo.verify_pin("p1", "0000", now=1000) is False
    assert repo.verify_pin("p1", "0000", now=1000) is False
    # まだ lockout していない。
    assert repo.verify_pin("p1", "1234", now=1000) is True


def test_set_pin_clears_lockout(tmp_path: Path):
    repo = _repo(tmp_path, max_attempts=1, lockout_sec=300)
    repo.set_pin("p1", "1234")
    assert repo.verify_pin("p1", "0000", now=1000) is False  # lockout 発火
    repo.set_pin("p1", "5678")  # reset
    assert repo.verify_pin("p1", "5678", now=1100) is True
