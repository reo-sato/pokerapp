"""tests/test_gui.py

Phase 4: GUIDashboard のロジック部分のテスト（tkinter 不要）。
- _conf_color() の閾値
- on_action() → _update_queue への投入
- _cmd_new_hand / _cmd_winner が audio_queue に正しいイベントを投入する
"""
from __future__ import annotations

import queue
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.events import AudioEvent
from gui.dashboard import _conf_color, _CONF_COLOR_HIGH, _CONF_COLOR_MEDIUM, _CONF_COLOR_LOW


# ――― _conf_color ―――

class TestConfColor:
    def test_high_threshold(self):
        assert _conf_color(0.75) == _CONF_COLOR_HIGH
        assert _conf_color(1.0)  == _CONF_COLOR_HIGH

    def test_medium_threshold(self):
        assert _conf_color(0.5)  == _CONF_COLOR_MEDIUM
        assert _conf_color(0.74) == _CONF_COLOR_MEDIUM

    def test_low_threshold(self):
        assert _conf_color(0.0)  == _CONF_COLOR_LOW
        assert _conf_color(0.49) == _CONF_COLOR_LOW


# ――― GUIDashboard ロジック（tkinter をモックして構築） ―――

def _make_mock_dashboard(tmp_path: Path):
    """customtkinter と tkinter をモックして GUIDashboard を生成する。"""
    from core.game_state import GameStateManager, PlayerState
    from output.json_writer import JsonWriter

    players = [
        PlayerState(seat=1, name="Alice", stack=10000),
        PlayerState(seat=2, name="Bob",   stack=10000),
    ]
    gs = GameStateManager(players=players, sb=100, bb=200)
    gs.new_hand()
    writer = JsonWriter(log_dir=tmp_path, session_id="gui_test")
    audio_q: queue.Queue = queue.Queue()
    stop = threading.Event()

    # customtkinter 全体をモック
    ctk_mock = MagicMock()
    ctk_mock.CTkLabel.return_value = MagicMock()
    ctk_mock.CTkFrame.return_value = MagicMock()
    ctk_mock.CTkScrollableFrame.return_value = MagicMock()
    ctk_mock.CTkTextbox.return_value = MagicMock()
    ctk_mock.CTkButton.return_value = MagicMock()
    ctk_mock.CTkOptionMenu.return_value = MagicMock()
    ctk_mock.CTkEntry.return_value = MagicMock()
    ctk_mock.StringVar.return_value = MagicMock(get=MagicMock(return_value="1"))
    ctk_mock.CTk.return_value = MagicMock()

    with patch.dict("sys.modules", {"customtkinter": ctk_mock}):
        from gui.dashboard import GUIDashboard
        dash = GUIDashboard(
            game_state=gs,
            json_writer=writer,
            audio_queue=audio_q,
            stop_event=stop,
        )

    return dash, gs, audio_q, stop


class TestGUIDashboardLogic:
    def test_on_action_puts_to_update_queue(self, tmp_path: Path):
        from core.hand_log import ActionRecord
        dash, gs, audio_q, stop = _make_mock_dashboard(tmp_path)

        record = ActionRecord(
            hand_id=1, timestamp="2026-04-02T12:00:00",
            street="preflop", seat=1, player_name="Alice",
            action="bet", amount=500, pot_after=500, stack_after=9500,
            source={"camera": False, "audio": True, "rfid": False},
            needs_review=False, confidence=0.5,
        )
        dash.on_action(record)

        assert not dash._update_queue.empty()
        got = dash._update_queue.get_nowait()
        assert got is record

    def test_cmd_new_hand_puts_audio_event(self, tmp_path: Path):
        dash, gs, audio_q, stop = _make_mock_dashboard(tmp_path)
        dash._cmd_new_hand()

        ev = audio_q.get_nowait()
        assert isinstance(ev, AudioEvent)
        assert ev.action == "new_hand"

    def test_cmd_winner_puts_audio_event_with_seat(self, tmp_path: Path):
        dash, gs, audio_q, stop = _make_mock_dashboard(tmp_path)
        # StringVar.get() は "1" を返すようにモック済み
        dash._cmd_winner()

        ev = audio_q.get_nowait()
        assert isinstance(ev, AudioEvent)
        assert ev.action == "winner"
        assert "シート1" in ev.raw_text

    def test_cmd_rebuy_invalid_amount_does_not_crash(self, tmp_path: Path):
        dash, gs, audio_q, stop = _make_mock_dashboard(tmp_path)
        # entry.get() が空文字列を返す場合
        dash._rebuy_amount_entry = MagicMock(get=MagicMock(return_value="abc"))
        dash._rebuy_seat_var = MagicMock(get=MagicMock(return_value="1"))
        dash._append_log = MagicMock()
        dash._cmd_rebuy()
        # クラッシュせず、_append_log が呼ばれる
        dash._append_log.assert_called_once()
