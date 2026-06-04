"""tests/test_session_viewer_gui.py

WS2-α: Session / Seating Viewer（read-only）のロジックテスト（tkinter 不要）。

customtkinter をモックして `SessionViewerWindow` を構築し、内部 view-model
（`self._current_detail` / `self._sessions`）と state 遷移を検証する。実 repository
（tmp_path）を使って read 経路まで通す。

検査:
- 純粋ヘルパ（resolve_display_name / build_session_detail）。
- session 0 件で empty state。
- session 1 件以上で最初の session が自動選択され概要が出る。
- current seating が表示され player_id が display_name に解決される。
- 未登録 player_id は (unknown) になりつつ player_id も保持される。
- refresh で repository から再読込される（呼び出し回数を観測）。
- read-only であること（編集 API を持たない最低限の確認）。
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.player_repository import PlayerRepository
from core.session_repository import SessionRepository
from gui.session_viewer import (
    UNKNOWN_LABEL,
    SessionDetail,
    build_session_detail,
    resolve_display_name,
)


# ――― fixtures ―――

def _make_repos(tmp_path: Path):
    players = PlayerRepository(path=tmp_path / "players.json")
    sessions = SessionRepository(path=tmp_path / "sessions.json", player_repo=players)
    return sessions, players


def _pid(players: PlayerRepository, name: str) -> str:
    return next(p.player_id for p in players.list_players() if p.display_name == name)


def _mock_ctk():
    ctk = MagicMock()
    for attr in (
        "CTk", "CTkToplevel", "CTkFrame", "CTkScrollableFrame",
        "CTkLabel", "CTkButton",
    ):
        getattr(ctk, attr).return_value = MagicMock()
    return ctk


def _make_window(session_repo, player_repo):
    ctk = _mock_ctk()
    with patch.dict("sys.modules", {"customtkinter": ctk}):
        from gui.session_viewer import SessionViewerWindow
        win = SessionViewerWindow(session_repo=session_repo, player_repo=player_repo)
    return win


# ――― 純粋ヘルパ ―――

class TestPureHelpers:
    def test_resolve_known(self):
        assert resolve_display_name("a" * 32, {"a" * 32: "Alice"}) == "Alice"

    def test_resolve_unknown(self):
        assert resolve_display_name("z" * 32, {}) == UNKNOWN_LABEL

    def test_build_session_detail_shapes_data(self, tmp_path: Path):
        sessions, players = _make_repos(tmp_path)
        players.create_player("Alice")
        players.create_player("Bob")
        s = sessions.create_session(blinds={"sb": 100, "bb": 200})
        sessions.assign_seat(s.session_id, 1, 1, _pid(players, "Alice"))
        sessions.assign_seat(s.session_id, 1, 2, _pid(players, "Bob"))

        name_map = {p.player_id: p.display_name for p in players.list_players()}
        detail = build_session_detail(sessions, name_map, s.session_id)

        assert isinstance(detail, SessionDetail)
        assert detail.status == "open"
        assert {r.seat_no: r.display_name for r in detail.current_seating} == {
            1: "Alice", 2: "Bob",
        }
        assert {(a.hand_id, a.seat_no) for a in detail.hand_assignments} == {
            (1, 1), (1, 2),
        }


# ――― empty state ―――

class TestEmptyState:
    def test_no_sessions(self, tmp_path: Path):
        sessions, players = _make_repos(tmp_path)
        win = _make_window(sessions, players)
        assert win._sessions == []
        assert win._selected_id is None
        assert win._current_detail is None

    def test_session_without_seating(self, tmp_path: Path):
        sessions, players = _make_repos(tmp_path)
        s = sessions.create_session()
        win = _make_window(sessions, players)
        assert win._selected_id == s.session_id
        assert win._current_detail is not None
        assert win._current_detail.current_seating == []
        assert win._current_detail.hand_assignments == []


# ――― 選択 & 詳細 ―――

class TestSelectionAndDetail:
    def test_first_session_auto_selected(self, tmp_path: Path):
        sessions, players = _make_repos(tmp_path)
        s1 = sessions.create_session(label="first")
        sessions.create_session(label="second")
        win = _make_window(sessions, players)
        assert win._selected_id == s1.session_id
        assert win._current_detail.session_id == s1.session_id

    def test_current_seating_resolved(self, tmp_path: Path):
        sessions, players = _make_repos(tmp_path)
        players.create_player("Alice")
        players.create_player("Bob")
        s = sessions.create_session()
        sessions.assign_seat(s.session_id, 1, 1, _pid(players, "Alice"))
        sessions.assign_seat(s.session_id, 1, 3, _pid(players, "Bob"))

        win = _make_window(sessions, players)
        seating = {r.seat_no: r.display_name for r in win._current_detail.current_seating}
        assert seating == {1: "Alice", 3: "Bob"}

    def test_select_other_session_updates_detail(self, tmp_path: Path):
        sessions, players = _make_repos(tmp_path)
        s1 = sessions.create_session()
        s2 = sessions.create_session()
        win = _make_window(sessions, players)
        assert win._selected_id == s1.session_id

        win._select_session(s2.session_id)
        assert win._selected_id == s2.session_id
        assert win._current_detail.session_id == s2.session_id


# ――― unknown player ―――

class TestUnknownPlayer:
    def test_unknown_player_id_shown_safely(self, tmp_path: Path):
        sessions, players = _make_repos(tmp_path)
        alice = players.create_player("Alice")
        s = sessions.create_session()
        sessions.assign_seat(s.session_id, 1, 1, alice.player_id)

        # registry から Alice を消す前提を作るのは難しいので、name_map に無い状況を
        # build_session_detail で直接検証する（viewer は同じ経路を使う）。
        detail = build_session_detail(sessions, {}, s.session_id)
        row = detail.current_seating[0]
        assert row.display_name == UNKNOWN_LABEL
        assert row.player_id == alice.player_id  # player_id は保持される


# ――― refresh ―――

class TestRefresh:
    def test_refresh_rereads_repository(self, tmp_path: Path):
        sessions, players = _make_repos(tmp_path)
        sessions.create_session()
        win = _make_window(sessions, players)

        # list_sessions の呼び出しを観測
        spy = MagicMock(wraps=sessions.list_sessions)
        sessions.list_sessions = spy
        win._cmd_refresh()
        spy.assert_called()

    def test_refresh_picks_up_new_session(self, tmp_path: Path):
        sessions, players = _make_repos(tmp_path)
        sessions.create_session()
        win = _make_window(sessions, players)
        assert len(win._sessions) == 1

        sessions.create_session()
        win._cmd_refresh()
        assert len(win._sessions) == 2


# ――― read-only / 独立性 ―――

class TestReadOnly:
    def test_viewer_has_no_edit_methods(self, tmp_path: Path):
        sessions, players = _make_repos(tmp_path)
        win = _make_window(sessions, players)
        # 編集系メソッドを持たない（read-only）
        for attr in ("create_session", "assign_seat", "close_session",
                     "_cmd_add", "_cmd_rename", "_cmd_new_hand"):
            assert not hasattr(win, attr)

    def test_distinct_from_other_windows(self):
        from gui.session_viewer import SessionViewerWindow
        from gui.dashboard import GUIDashboard
        from gui.player_registry import PlayerRegistryWindow
        assert SessionViewerWindow is not GUIDashboard
        assert SessionViewerWindow is not PlayerRegistryWindow
