"""tests/test_ledger_view_gui.py

Phase S3.2: LedgerViewWindow のロジック部分のテスト（tkinter 不要）。
customtkinter をモックして構築し、コマンドメソッドが LedgerRepository と
validation メッセージ（error code 表示）を正しく動かすことを確認する。

hand logger dashboard とは別構造であることも併せて確認する。
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from core.ledger_repository import LedgerRepository
from core.player_repository import PlayerRepository
from core.session_repository import SessionRepository


def _make_window(tmp_path: Path):
    players = PlayerRepository(path=tmp_path / "players.json")
    players.create_player("Alice")
    players.create_player("Bob")
    sessions = SessionRepository(path=tmp_path / "sessions.json", player_repo=players)
    ledger = LedgerRepository(
        path=tmp_path / "ledger.json", session_repo=sessions, player_repo=players
    )

    ctk_mock = MagicMock()
    ctk_mock.CTkLabel.return_value = MagicMock()
    ctk_mock.CTkFrame.return_value = MagicMock()
    ctk_mock.CTkScrollableFrame.return_value = MagicMock()
    ctk_mock.CTkButton.return_value = MagicMock()
    ctk_mock.CTkEntry.return_value = MagicMock(get=MagicMock(return_value=""))
    ctk_mock.CTkOptionMenu.return_value = MagicMock(get=MagicMock(return_value="buy_in"))
    ctk_mock.CTk.return_value = MagicMock()
    ctk_mock.CTkToplevel.return_value = MagicMock()

    with patch.dict("sys.modules", {"customtkinter": ctk_mock}):
        from gui.ledger_view import LedgerViewWindow
        win = LedgerViewWindow(ledger_repo=ledger, session_repo=sessions, player_repo=players)

    win._set_status = MagicMock()
    return win, ledger, sessions, players


def _pid(players: PlayerRepository, name: str) -> str:
    return next(p.player_id for p in players.list_players() if p.display_name == name)


def _set_form(win, kind="buy_in", cash="", point="", note="", grant=""):
    win._kind_menu = MagicMock(get=MagicMock(return_value=kind))
    win._cash_entry = MagicMock(get=MagicMock(return_value=cash))
    win._point_entry = MagicMock(get=MagicMock(return_value=point))
    win._note_entry = MagicMock(get=MagicMock(return_value=note))
    win._grant_entry = MagicMock(get=MagicMock(return_value=grant))


class TestAddEntry:
    def test_add_success(self, tmp_path: Path):
        win, ledger, sessions, players = _make_window(tmp_path)
        s = sessions.create_session()
        win._session_id = s.session_id
        win._selected_player_id = _pid(players, "Alice")
        _set_form(win, kind="buy_in", cash="5000")
        win._cmd_add_entry()

        entries = ledger.list_entries(session_id=s.session_id)
        assert [e.kind for e in entries] == ["buy_in"]
        assert entries[0].cash_amount == 5000
        assert win._set_status.call_args.kwargs.get("error", False) is False

    def test_requires_session(self, tmp_path: Path):
        win, ledger, sessions, players = _make_window(tmp_path)
        win._session_id = None
        win._selected_player_id = _pid(players, "Alice")
        _set_form(win, cash="5000")
        win._cmd_add_entry()
        assert win._set_status.call_args.kwargs.get("error") is True

    def test_requires_player(self, tmp_path: Path):
        win, ledger, sessions, players = _make_window(tmp_path)
        s = sessions.create_session()
        win._session_id = s.session_id
        win._selected_player_id = None
        _set_form(win, cash="5000")
        win._cmd_add_entry()
        assert win._set_status.call_args.kwargs.get("error") is True

    def test_non_integer_amount_shows_validation(self, tmp_path: Path):
        win, ledger, sessions, players = _make_window(tmp_path)
        s = sessions.create_session()
        win._session_id = s.session_id
        win._selected_player_id = _pid(players, "Alice")
        _set_form(win, kind="buy_in", cash="abc")
        win._cmd_add_entry()
        assert ledger.list_entries(session_id=s.session_id) == []
        assert win._set_status.call_args.kwargs.get("error") is True

    def test_insufficient_points_shows_core_error(self, tmp_path: Path):
        win, ledger, sessions, players = _make_window(tmp_path)
        s = sessions.create_session()
        win._session_id = s.session_id
        win._selected_player_id = _pid(players, "Alice")  # 残高 0
        _set_form(win, kind="buy_in", cash="0", point="2000")
        win._cmd_add_entry()
        assert ledger.list_entries(session_id=s.session_id) == []
        assert win._set_status.call_args.kwargs.get("error") is True

    def test_entry_fee_with_points_shows_core_error(self, tmp_path: Path):
        win, ledger, sessions, players = _make_window(tmp_path)
        alice = _pid(players, "Alice")
        ledger.grant_points(alice, 5000)
        s = sessions.create_session()
        win._session_id = s.session_id
        win._selected_player_id = alice
        _set_form(win, kind="entry_fee", cash="500", point="100")
        win._cmd_add_entry()
        assert [e.kind for e in ledger.list_entries(session_id=s.session_id)] == []
        assert win._set_status.call_args.kwargs.get("error") is True

    def test_invalid_kind_shows_validation(self, tmp_path: Path):
        win, ledger, sessions, players = _make_window(tmp_path)
        s = sessions.create_session()
        win._session_id = s.session_id
        win._selected_player_id = _pid(players, "Alice")
        _set_form(win, kind="cashout", cash="1000")
        win._cmd_add_entry()
        assert win._set_status.call_args.kwargs.get("error") is True


class TestGrantAndReverse:
    def test_grant_points_success(self, tmp_path: Path):
        win, ledger, sessions, players = _make_window(tmp_path)
        alice = _pid(players, "Alice")
        win._selected_player_id = alice
        _set_form(win, grant="1000")
        win._cmd_grant_points()
        assert ledger.point_balance(alice) == 1000
        assert win._set_status.call_args.kwargs.get("error", False) is False

    def test_grant_requires_player(self, tmp_path: Path):
        win, ledger, sessions, players = _make_window(tmp_path)
        win._selected_player_id = None
        _set_form(win, grant="1000")
        win._cmd_grant_points()
        assert win._set_status.call_args.kwargs.get("error") is True

    def test_reverse_entry_is_append_only(self, tmp_path: Path):
        win, ledger, sessions, players = _make_window(tmp_path)
        alice = _pid(players, "Alice")
        s = sessions.create_session()
        e = ledger.add_entry(s.session_id, alice, "buy_in", cash_amount=3000)
        win._session_id = s.session_id
        win._selected_player_id = alice
        win._cmd_reverse_entry(e.entry_id)

        entries = ledger.list_entries(session_id=s.session_id)
        assert len(entries) == 2  # 原 entry + reversal（append-only）
        assert sum(x.cash_amount for x in entries) == 0
        assert win._set_status.call_args.kwargs.get("error", False) is False


class TestRosterAndSelection:
    def test_roster_prefill_uses_seating(self, tmp_path: Path):
        win, ledger, sessions, players = _make_window(tmp_path)
        alice = _pid(players, "Alice")
        s = sessions.create_session()
        sessions.assign_seat(s.session_id, 1, 3, alice)
        assert win._roster_for_session(s.session_id) == [alice]

    def test_roster_fallback_to_all_players(self, tmp_path: Path):
        win, ledger, sessions, players = _make_window(tmp_path)
        s = sessions.create_session()  # seating 無し
        roster = win._roster_for_session(s.session_id)
        assert set(roster) == {_pid(players, "Alice"), _pid(players, "Bob")}

    def test_select_session_sets_state(self, tmp_path: Path):
        win, ledger, sessions, players = _make_window(tmp_path)
        s = sessions.create_session()
        win._select_session(s.session_id)
        assert win._session_id == s.session_id

    def test_select_player_sets_state(self, tmp_path: Path):
        win, ledger, sessions, players = _make_window(tmp_path)
        alice = _pid(players, "Alice")
        win._select_player(alice)
        assert win._selected_player_id == alice


class TestSeparateFromHandLogger:
    """ledger viewer が hand logger dashboard と別構造であることの最低限の確認。"""

    def test_does_not_require_hand_logger_deps(self, tmp_path: Path):
        win, ledger, sessions, players = _make_window(tmp_path)
        assert not hasattr(win, "_gs")
        assert not hasattr(win, "_writer")
        assert not hasattr(win, "_audio_queue")
        assert win._ledger is ledger

    def test_lives_in_its_own_module(self):
        from gui.ledger_view import LedgerViewWindow
        assert LedgerViewWindow.__module__ == "gui.ledger_view"
