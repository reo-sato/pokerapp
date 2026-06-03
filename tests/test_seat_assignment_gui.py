"""tests/test_seat_assignment_gui.py

Phase 2.3: seat selection UX の GUI ロジックテスト（tkinter 不要）。

customtkinter をモックして:
- `build_seating_map` の純粋ロジック（空席・display_name→player_id 変換）。
- `SeatAssignmentDialog` が carry-forward 初期値を表示し、OK で seating を組み立てて
  on_confirm に渡し、キャンセルでは渡さないこと。
- `GUIDashboard` の seat フロー: flag off では seat UI を出さず new_hand を直接流す、
  flag on では new_hand 前に seat ダイアログ経由で IntegrationThread.update_seating する。
"""
from __future__ import annotations

import queue
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.player import Player
from gui.seat_assignment import EMPTY_LABEL, SeatAssignmentDialog, build_seating_map


# ――― build_seating_map（純粋ロジック） ―――

class TestBuildSeatingMap:
    def test_maps_names_to_ids(self):
        name_to_id = {"Alice": "a" * 32, "Bob": "b" * 32}
        selections = {1: "Alice", 2: "Bob"}
        assert build_seating_map(selections, name_to_id) == {1: "a" * 32, 2: "b" * 32}

    def test_empty_label_is_excluded(self):
        name_to_id = {"Alice": "a" * 32}
        selections = {1: "Alice", 2: EMPTY_LABEL}
        assert build_seating_map(selections, name_to_id) == {1: "a" * 32}

    def test_unknown_name_is_excluded(self):
        name_to_id = {"Alice": "a" * 32}
        selections = {1: "Ghost"}
        assert build_seating_map(selections, name_to_id) == {}


# ――― SeatAssignmentDialog ―――

def _players() -> list[Player]:
    return [
        Player(player_id="a" * 32, display_name="Alice", created_at="2026-06-03T00:00:00"),
        Player(player_id="b" * 32, display_name="Bob", created_at="2026-06-03T00:00:00"),
    ]


def _mock_ctk():
    ctk = MagicMock()
    ctk.CTkToplevel.return_value = MagicMock()
    ctk.CTkLabel.return_value = MagicMock()
    ctk.CTkButton.return_value = MagicMock()
    ctk.CTkOptionMenu.return_value = MagicMock()
    # StringVar は value を保持して get() で返す簡易モック
    def _string_var(value="", **kwargs):
        m = MagicMock()
        m.get.return_value = value
        return m
    ctk.StringVar.side_effect = _string_var
    return ctk


class TestSeatAssignmentDialog:
    def test_carry_forward_initial_values(self):
        ctk = _mock_ctk()
        current = {1: "a" * 32}  # 席1 は Alice 継続、席2 は空席
        dlg = SeatAssignmentDialog(
            parent=MagicMock(), ctk=ctk, players=_players(),
            current_seating=current, seats=[1, 2], on_confirm=MagicMock(),
        )
        # 席1 の StringVar 初期値は "Alice"、席2 は EMPTY_LABEL
        assert dlg._seat_vars[1].get() == "Alice"
        assert dlg._seat_vars[2].get() == EMPTY_LABEL

    def test_ok_builds_seating_and_calls_confirm(self):
        ctk = _mock_ctk()
        on_confirm = MagicMock()
        dlg = SeatAssignmentDialog(
            parent=MagicMock(), ctk=ctk, players=_players(),
            current_seating={1: "a" * 32, 2: "b" * 32}, seats=[1, 2],
            on_confirm=on_confirm,
        )
        dlg._cmd_ok()
        on_confirm.assert_called_once_with({1: "a" * 32, 2: "b" * 32})
        dlg._win.destroy.assert_called_once()

    def test_empty_selection_excluded_on_ok(self):
        ctk = _mock_ctk()
        on_confirm = MagicMock()
        dlg = SeatAssignmentDialog(
            parent=MagicMock(), ctk=ctk, players=_players(),
            current_seating={1: "a" * 32}, seats=[1, 2], on_confirm=on_confirm,
        )
        # 席2 は EMPTY_LABEL のまま → seating に入らない
        dlg._cmd_ok()
        on_confirm.assert_called_once_with({1: "a" * 32})

    def test_cancel_does_not_call_confirm(self):
        ctk = _mock_ctk()
        on_confirm = MagicMock()
        dlg = SeatAssignmentDialog(
            parent=MagicMock(), ctk=ctk, players=_players(),
            current_seating={}, seats=[1, 2], on_confirm=on_confirm,
        )
        dlg._cmd_cancel()
        on_confirm.assert_not_called()
        dlg._win.destroy.assert_called_once()

    def test_player_list_shown_as_options(self):
        ctk = _mock_ctk()
        dlg = SeatAssignmentDialog(
            parent=MagicMock(), ctk=ctk, players=_players(),
            current_seating={}, seats=[1], on_confirm=MagicMock(),
        )
        assert dlg._options == [EMPTY_LABEL, "Alice", "Bob"]


# ――― GUIDashboard の seat フロー ―――

def _make_dashboard(tmp_path: Path, session_layer_enabled: bool):
    from core.game_state import GameStateManager, PlayerState
    from core.player_repository import PlayerRepository
    from output.json_writer import JsonWriter

    gs = GameStateManager(
        players=[
            PlayerState(seat=1, name="Alice", stack=10000),
            PlayerState(seat=2, name="Bob", stack=10000),
        ],
        sb=100, bb=200,
    )
    gs.new_hand()
    writer = JsonWriter(log_dir=tmp_path, session_id="gui_test")
    audio_q: queue.Queue = queue.Queue()
    stop = threading.Event()

    player_repo = PlayerRepository(path=tmp_path / "players.json")
    player_repo.create_player("Alice")
    player_repo.create_player("Bob")

    ctk_mock = _mock_ctk()
    ctk_mock.CTkFrame.return_value = MagicMock()
    ctk_mock.CTkScrollableFrame.return_value = MagicMock()
    ctk_mock.CTkTextbox.return_value = MagicMock()
    ctk_mock.CTkEntry.return_value = MagicMock()
    ctk_mock.CTk.return_value = MagicMock()
    ctk_mock.StringVar.side_effect = None
    ctk_mock.StringVar.return_value = MagicMock(get=MagicMock(return_value="1"))

    with patch.dict("sys.modules", {"customtkinter": ctk_mock}):
        from gui.dashboard import GUIDashboard
        dash = GUIDashboard(
            game_state=gs,
            json_writer=writer,
            audio_queue=audio_q,
            stop_event=stop,
            player_repo=player_repo,
            session_layer_enabled=session_layer_enabled,
        )
    return dash, audio_q, player_repo


class TestDashboardSeatFlow:
    def test_flag_off_new_hand_enqueues_directly(self, tmp_path: Path):
        """flag off: seat ダイアログを出さず new_hand を直接流す（従来動作）。"""
        from core.events import AudioEvent
        dash, audio_q, _ = _make_dashboard(tmp_path, session_layer_enabled=False)
        assert dash._session_layer_enabled is False

        dash._cmd_new_hand()
        ev = audio_q.get_nowait()
        assert isinstance(ev, AudioEvent)
        assert ev.action == "new_hand"

    def test_flag_on_new_hand_opens_dialog_then_starts(self, tmp_path: Path):
        """flag on: seat ダイアログ経由で update_seating → new_hand が流れる。"""
        from core.events import AudioEvent
        dash, audio_q, player_repo = _make_dashboard(tmp_path, session_layer_enabled=True)
        assert dash._session_layer_enabled is True

        # IntegrationThread をスタブ化（update_seating / get_seating を観測）
        integ = MagicMock()
        integ.get_seating.return_value = {}
        dash._integration_thread = integ

        # ダイアログを captureして即 OK を押す
        captured = {}

        def fake_dialog(*, parent, ctk, players, current_seating, seats, on_confirm):
            captured["players"] = list(players)
            captured["seats"] = list(seats)
            # ユーザが Alice→席1 を選んで OK したと仮定
            alice = next(p for p in player_repo.list_players() if p.display_name == "Alice")
            on_confirm({1: alice.player_id})
            return MagicMock()

        with patch("gui.seat_assignment.SeatAssignmentDialog", side_effect=fake_dialog):
            dash._cmd_new_hand()

        # player 候補が registry から渡る
        assert {p.display_name for p in captured["players"]} == {"Alice", "Bob"}
        # update_seating が呼ばれ、続けて new_hand が流れる
        integ.update_seating.assert_called_once()
        alice = next(p for p in player_repo.list_players() if p.display_name == "Alice")
        assert integ.update_seating.call_args.args[0] == {1: alice.player_id}
        ev = audio_q.get_nowait()
        assert ev.action == "new_hand"

    def test_flag_on_cancel_does_not_start_hand(self, tmp_path: Path):
        """flag on: ダイアログをキャンセルすると new_hand は流れない。"""
        dash, audio_q, _ = _make_dashboard(tmp_path, session_layer_enabled=True)
        integ = MagicMock()
        integ.get_seating.return_value = {}
        dash._integration_thread = integ

        def fake_dialog(*, parent, ctk, players, current_seating, seats, on_confirm):
            # on_confirm を呼ばない = キャンセル相当
            return MagicMock()

        with patch("gui.seat_assignment.SeatAssignmentDialog", side_effect=fake_dialog):
            dash._cmd_new_hand()

        integ.update_seating.assert_not_called()
        assert audio_q.empty()
