"""tests/test_player_registry_gui.py

Phase S1: PlayerRegistryWindow のロジック部分のテスト（tkinter 不要）。
customtkinter をモックして構築し、コマンドメソッドがリポジトリと
validation メッセージを正しく動かすことを確認する。

hand logger dashboard とは別構造であることも併せて確認する。
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch


from core.player_repository import PlayerRepository


def _make_window(tmp_path: Path):
    repo = PlayerRepository(path=tmp_path / "players.json")

    ctk_mock = MagicMock()
    ctk_mock.CTkLabel.return_value = MagicMock()
    ctk_mock.CTkFrame.return_value = MagicMock()
    ctk_mock.CTkScrollableFrame.return_value = MagicMock()
    ctk_mock.CTkButton.return_value = MagicMock()
    ctk_mock.CTkEntry.return_value = MagicMock(get=MagicMock(return_value=""))
    ctk_mock.CTk.return_value = MagicMock()
    ctk_mock.CTkToplevel.return_value = MagicMock()

    with patch.dict("sys.modules", {"customtkinter": ctk_mock}):
        from gui.player_registry import PlayerRegistryWindow
        win = PlayerRegistryWindow(repository=repo)

    # _set_status を監視できるようモックに差し替え
    win._set_status = MagicMock()
    return win, repo


class TestAddPlayer:
    def test_add_success(self, tmp_path: Path):
        win, repo = _make_window(tmp_path)
        win._name_entry = MagicMock(get=MagicMock(return_value="Alice"))
        win._cmd_add()

        names = [p.display_name for p in repo.list_players()]
        assert names == ["Alice"]
        win._set_status.assert_called_once()
        # error フラグなしで成功通知
        assert win._set_status.call_args.kwargs.get("error", False) is False

    def test_add_blank_shows_validation(self, tmp_path: Path):
        win, repo = _make_window(tmp_path)
        win._name_entry = MagicMock(get=MagicMock(return_value="   "))
        win._cmd_add()

        assert repo.list_players() == []
        win._set_status.assert_called_once()
        assert win._set_status.call_args.kwargs.get("error") is True

    def test_add_duplicate_shows_validation(self, tmp_path: Path):
        win, repo = _make_window(tmp_path)
        repo.create_player("Alice")
        win._name_entry = MagicMock(get=MagicMock(return_value="Alice"))
        win._cmd_add()

        # 重複は追加されない
        assert len([p for p in repo.list_players() if p.display_name == "Alice"]) == 1
        win._set_status.assert_called_once()
        assert win._set_status.call_args.kwargs.get("error") is True


class TestRenamePlayer:
    def test_rename_success(self, tmp_path: Path):
        win, repo = _make_window(tmp_path)
        a = repo.create_player("Alice")
        win._selected_id = a.player_id
        win._rename_entry = MagicMock(get=MagicMock(return_value="Alicia"))
        win._cmd_rename()

        assert repo.get(a.player_id).display_name == "Alicia"
        assert win._set_status.call_args.kwargs.get("error", False) is False

    def test_rename_without_selection_shows_validation(self, tmp_path: Path):
        win, repo = _make_window(tmp_path)
        win._selected_id = None
        win._rename_entry = MagicMock(get=MagicMock(return_value="Whoever"))
        win._cmd_rename()

        win._set_status.assert_called_once()
        assert win._set_status.call_args.kwargs.get("error") is True

    def test_rename_duplicate_shows_validation(self, tmp_path: Path):
        win, repo = _make_window(tmp_path)
        a = repo.create_player("Alice")
        repo.create_player("Bob")
        win._selected_id = a.player_id
        win._rename_entry = MagicMock(get=MagicMock(return_value="Bob"))
        win._cmd_rename()

        assert repo.get(a.player_id).display_name == "Alice"
        assert win._set_status.call_args.kwargs.get("error") is True

    def test_select_player_sets_selected_id(self, tmp_path: Path):
        win, repo = _make_window(tmp_path)
        a = repo.create_player("Alice")
        win._select_player(a.player_id)
        assert win._selected_id == a.player_id


class TestMergePlayers:
    """ADR-0030: registry GUI から player merge を行う（統合先設定 → 統合）。"""

    def test_merge_marks_tombstone_and_hides(self, tmp_path: Path):
        win, repo = _make_window(tmp_path)
        survivor = repo.create_player("Bob (venue)")
        absorbed = repo.create_player("Bob (cloud)")
        # survivor を選択 → 統合先に設定
        win._select_player(survivor.player_id)
        win._cmd_set_merge_survivor()
        assert win._merge_survivor_id == survivor.player_id
        # absorbed を選択 → 統合
        win._select_player(absorbed.player_id)
        win._cmd_merge()

        assert repo.resolve_canonical(absorbed.player_id) == survivor.player_id
        assert {p.player_id for p in repo.list_players()} == {survivor.player_id}
        assert win._set_status.call_args.kwargs.get("error", False) is False

    def test_merge_without_survivor_shows_validation(self, tmp_path: Path):
        win, repo = _make_window(tmp_path)
        a = repo.create_player("A")
        win._select_player(a.player_id)
        win._cmd_merge()  # 統合先未設定
        assert win._set_status.call_args.kwargs.get("error") is True
        assert not repo.get(a.player_id).is_merged

    def test_merge_self_shows_validation(self, tmp_path: Path):
        win, repo = _make_window(tmp_path)
        a = repo.create_player("A")
        win._select_player(a.player_id)
        win._cmd_set_merge_survivor()
        win._select_player(a.player_id)  # survivor と同一を統合元に
        win._cmd_merge()
        assert win._set_status.call_args.kwargs.get("error") is True
        assert not repo.get(a.player_id).is_merged


class TestSeparateFromHandLogger:
    """player registry が hand logger dashboard と別構造であることの最低限の確認。"""

    def test_registry_does_not_require_game_state(self, tmp_path: Path):
        win, repo = _make_window(tmp_path)
        # hand logger 依存 (_gs / _writer / _audio_queue) を持たない
        assert not hasattr(win, "_gs")
        assert not hasattr(win, "_writer")
        assert not hasattr(win, "_audio_queue")
        # player registry 固有の依存のみ
        assert win._repo is repo

    def test_registry_and_dashboard_are_distinct_classes(self):
        from gui.player_registry import PlayerRegistryWindow
        from gui.dashboard import GUIDashboard
        assert PlayerRegistryWindow is not GUIDashboard
        assert PlayerRegistryWindow.__module__ != GUIDashboard.__module__
