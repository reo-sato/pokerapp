"""tests/test_player_repository.py

Phase S1: Player ドメインモデルと PlayerRepository のテスト。
- stable id 生成
- save/load roundtrip（永続化）
- rename 保存
- duplicate name reject
- blank name reject
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core.player import Player
from core.player_repository import (
    DuplicateDisplayNameError,
    EmptyDisplayNameError,
    PlayerNotFoundError,
    PlayerRepository,
)


def _repo(tmp_path: Path) -> PlayerRepository:
    return PlayerRepository(path=tmp_path / "players.json")


class TestPlayerModel:
    def test_to_from_dict_roundtrip(self):
        p = Player(player_id="abc", display_name="Alice", created_at="2026-05-22T10:00:00")
        assert Player.from_dict(p.to_dict()) == p

    def test_from_dict_tolerates_missing_created_at(self):
        p = Player.from_dict({"player_id": "x", "display_name": "Bob"})
        assert p.created_at == ""


class TestCreate:
    def test_create_generates_stable_unique_id(self, tmp_path: Path):
        repo = _repo(tmp_path)
        a = repo.create_player("Alice")
        b = repo.create_player("Bob")
        assert a.player_id and b.player_id
        assert a.player_id != b.player_id
        # 同じ ID が一覧で安定して取得できる
        assert repo.get(a.player_id).player_id == a.player_id

    def test_create_strips_whitespace(self, tmp_path: Path):
        repo = _repo(tmp_path)
        p = repo.create_player("  Alice  ")
        assert p.display_name == "Alice"

    def test_blank_name_rejected(self, tmp_path: Path):
        repo = _repo(tmp_path)
        with pytest.raises(EmptyDisplayNameError):
            repo.create_player("")

    def test_whitespace_only_name_rejected(self, tmp_path: Path):
        repo = _repo(tmp_path)
        with pytest.raises(EmptyDisplayNameError):
            repo.create_player("   ")

    def test_duplicate_name_rejected(self, tmp_path: Path):
        repo = _repo(tmp_path)
        repo.create_player("Alice")
        with pytest.raises(DuplicateDisplayNameError):
            repo.create_player("Alice")

    def test_duplicate_after_strip_rejected(self, tmp_path: Path):
        repo = _repo(tmp_path)
        repo.create_player("Alice")
        with pytest.raises(DuplicateDisplayNameError):
            repo.create_player("  Alice ")


class TestPersistence:
    def test_save_load_roundtrip(self, tmp_path: Path):
        repo = _repo(tmp_path)
        a = repo.create_player("Alice")
        b = repo.create_player("Bob")

        # 別インスタンスで再ロード（アプリ再起動相当）
        repo2 = _repo(tmp_path)
        loaded = {p.player_id: p.display_name for p in repo2.list_players()}
        assert loaded == {a.player_id: "Alice", b.player_id: "Bob"}

    def test_id_stable_across_restart(self, tmp_path: Path):
        repo = _repo(tmp_path)
        a = repo.create_player("Alice")
        repo2 = _repo(tmp_path)
        assert repo2.get(a.player_id).display_name == "Alice"

    def test_load_corrupt_file_starts_empty(self, tmp_path: Path):
        path = tmp_path / "players.json"
        path.write_text("{ not valid json", encoding="utf-8")
        repo = PlayerRepository(path=path)
        assert repo.list_players() == []

    def test_list_players_sorted_by_creation(self, tmp_path: Path):
        repo = _repo(tmp_path)
        repo.create_player("First")
        repo.create_player("Second")
        names = [p.display_name for p in repo.list_players()]
        assert names == ["First", "Second"]


class TestRename:
    def test_rename_persists(self, tmp_path: Path):
        repo = _repo(tmp_path)
        a = repo.create_player("Alice")
        repo.rename_player(a.player_id, "Alicia")

        repo2 = _repo(tmp_path)
        assert repo2.get(a.player_id).display_name == "Alicia"

    def test_rename_to_same_name_allowed(self, tmp_path: Path):
        repo = _repo(tmp_path)
        a = repo.create_player("Alice")
        # 自分自身との一致は許容（no-op rename）
        result = repo.rename_player(a.player_id, "Alice")
        assert result.display_name == "Alice"

    def test_rename_to_existing_other_name_rejected(self, tmp_path: Path):
        repo = _repo(tmp_path)
        a = repo.create_player("Alice")
        repo.create_player("Bob")
        with pytest.raises(DuplicateDisplayNameError):
            repo.rename_player(a.player_id, "Bob")

    def test_rename_blank_rejected(self, tmp_path: Path):
        repo = _repo(tmp_path)
        a = repo.create_player("Alice")
        with pytest.raises(EmptyDisplayNameError):
            repo.rename_player(a.player_id, "   ")

    def test_rename_unknown_id_rejected(self, tmp_path: Path):
        repo = _repo(tmp_path)
        with pytest.raises(PlayerNotFoundError):
            repo.rename_player("nonexistent", "Whoever")
