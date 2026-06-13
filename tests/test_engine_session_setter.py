"""tests/test_engine_session_setter.py

Phase E3 (ISSUE-0006): IntegrationThread.set_seat_player_map の振る舞い。

GUI(座席設定ダイアログ)が構築後に seat_player_map を確定/変更するための setter。
session_repo 注入 + 非空 map で session レイヤが有効化され、write-through + player_id 埋め込みが
setter 経由でも成立すること、空 map / repo 無しでは無効のままであることを検証する。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from core.event_queue import make_audio_queue
from core.events import AudioEvent
from core.game_state import GameStateManager, PlayerState
from core.player_repository import PlayerRepository
from core.session_repository import SessionRepository
from integration.engine import IntegrationThread
from output.json_writer import JsonWriter


def _players(n: int = 2) -> list[PlayerState]:
    return [PlayerState(seat=i + 1, name=f"P{i + 1}", stack=10000) for i in range(n)]


def _registry_and_session(tmp_path: Path):
    prepo = PlayerRepository(path=tmp_path / "players.json")
    alice = prepo.create_player("Alice")
    bob = prepo.create_player("Bob")
    srepo = SessionRepository(path=tmp_path / "sessions.json", player_repo=prepo)
    session = srepo.create_session(label="test")
    seat_map = {1: alice.player_id, 2: bob.player_id}
    return prepo, srepo, session, seat_map


def _make_thread(tmp_path, srepo, session, **kwargs) -> IntegrationThread:
    return IntegrationThread(
        audio_queue=make_audio_queue(),
        game_state=GameStateManager(_players(2), sb=100, bb=200),
        json_writer=JsonWriter(tmp_path, session.session_id),
        stop_event=threading.Event(),
        session_repo=srepo,
        **kwargs,
    )


def _play_one_hand(thread: IntegrationThread):
    thread._handle_audio_event(AudioEvent("new_hand", 0, time.time(), "ハンド開始"))
    thread._handle_audio_event(AudioEvent("winner", 0, time.time(), "シート1 ウィナー"))


class TestSetSeatPlayerMap:
    def test_empty_map_keeps_layer_inactive(self, tmp_path: Path):
        _, srepo, session, _ = _registry_and_session(tmp_path)
        thread = _make_thread(tmp_path, srepo, session, seat_player_map={})
        assert thread._session_layer_active is False
        thread.set_seat_player_map({})
        assert thread._session_layer_active is False

    def test_setter_activates_layer_and_writes_through(self, tmp_path: Path):
        _, srepo, session, seat_map = _registry_and_session(tmp_path)
        captured: list = []
        thread = _make_thread(
            tmp_path, srepo, session, seat_player_map={}, on_hand=captured.append
        )
        assert thread._session_layer_active is False

        thread.set_seat_player_map(seat_map)
        assert thread._session_layer_active is True

        _play_one_hand(thread)
        summary = captured[-1].to_dict()
        hand_id = summary["hand_id"]
        assigns = {
            a.seat_no: a.player_id
            for a in srepo.list_seat_assignments(session.session_id, hand_id)
        }
        assert assigns == seat_map
        pmap = {p["seat"]: p.get("player_id") for p in summary["players"]}
        assert pmap[1] == seat_map[1]
        assert pmap[2] == seat_map[2]

    def test_clearing_map_deactivates_layer(self, tmp_path: Path):
        _, srepo, session, seat_map = _registry_and_session(tmp_path)
        thread = _make_thread(tmp_path, srepo, session, seat_player_map=seat_map)
        assert thread._session_layer_active is True
        thread.set_seat_player_map({})
        assert thread._session_layer_active is False


class TestSetterWithoutRepo:
    def test_map_without_repo_stays_inactive(self, tmp_path: Path):
        # session_repo 無し(rollback path)では map を入れても有効化しない
        thread = IntegrationThread(
            audio_queue=make_audio_queue(),
            game_state=GameStateManager(_players(2), sb=100, bb=200),
            json_writer=JsonWriter(tmp_path, "legacy-session"),
            stop_event=threading.Event(),
        )
        assert thread._session_layer_active is False
        thread.set_seat_player_map({1: "x", 2: "y"})
        assert thread._session_layer_active is False
