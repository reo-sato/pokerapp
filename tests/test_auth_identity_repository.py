"""tests/test_auth_identity_repository.py

ADR-0031 (L2): AuthIdentityRepository（(provider, subject)→player_id, node-local）の単体テスト。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core.auth_identity_repository import (
    AuthIdentityConflictError,
    AuthIdentityRepository,
)


def _repo(tmp_path: Path) -> AuthIdentityRepository:
    return AuthIdentityRepository(path=tmp_path / "auth_identity.json")


def test_link_and_get(tmp_path: Path):
    repo = _repo(tmp_path)
    assert repo.get("line", "U123") is None
    repo.link("line", "U123", "player1", display_name_seed="Bob")
    got = repo.get("line", "U123")
    assert got is not None and got.player_id == "player1"
    assert got.display_name_seed == "Bob"


def test_link_is_idempotent_same_player(tmp_path: Path):
    repo = _repo(tmp_path)
    first = repo.link("google", "G1", "player1")
    again = repo.link("google", "G1", "player1")
    assert again.linked_at == first.linked_at  # 再 link で更新しない


def test_link_conflict_different_player(tmp_path: Path):
    repo = _repo(tmp_path)
    repo.link("line", "U1", "player1")
    with pytest.raises(AuthIdentityConflictError):
        repo.link("line", "U1", "player2")


def test_multi_provider_for_one_player(tmp_path: Path):
    repo = _repo(tmp_path)
    repo.link("line", "U1", "player1")
    repo.link("google", "G1", "player1")
    linked = repo.list_for_player("player1")
    assert {i.provider for i in linked} == {"line", "google"}


def test_unlink(tmp_path: Path):
    repo = _repo(tmp_path)
    repo.link("line", "U1", "player1")
    repo.unlink("line", "U1")
    assert repo.get("line", "U1") is None
    repo.unlink("line", "U1")  # no-op, no error


def test_persistence_across_instances(tmp_path: Path):
    _repo(tmp_path).link("line", "U1", "player1", display_name_seed="Bob")
    reopened = _repo(tmp_path)
    got = reopened.get("line", "U1")
    assert got is not None and got.player_id == "player1" and got.display_name_seed == "Bob"
