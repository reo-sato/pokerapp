"""tests/test_phase_e_session_integration.py

Phase E (#10) E1+E2-core — hand logger × S2 session レイヤの write-through 接続（ADR-0008 Pattern A）。

- 接続時: hand 開始で assign_seat（session レイヤへ write-through）、HandSummary.players に player_id を
  additive 埋め込み。出力は hand schema に適合。
- 非接続時（session_repo/seat_player_map 無し）: players に player_id キーを足さない＝従来どおり（rollback）。

main.py の session 選択 UX と seat 選択 GUI（E3, ISSUE-0006）は本増分の対象外。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from core.event_queue import make_audio_queue
from core.events import AudioEvent
from core.game_state import GameStateManager, PlayerState
from core.player_repository import PlayerRepository
from core.session_repository import SessionRepository
from integration.engine import IntegrationThread
from output.json_writer import JsonWriter

_SCHEMAS = Path(__file__).parent.parent / "docs" / "contracts" / "schemas"


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


def _play_one_hand(thread: IntegrationThread):
    thread._handle_audio_event(AudioEvent("new_hand", 0, time.time(), "ハンド開始"))
    thread._handle_audio_event(AudioEvent("winner", 0, time.time(), "シート1 ウィナー"))


class TestSessionLayerConnected:
    def test_assign_seat_written_and_player_id_embedded(self, tmp_path: Path):
        _, srepo, session, seat_map = _registry_and_session(tmp_path)
        gs = GameStateManager(_players(2), sb=100, bb=200)
        captured: list = []
        thread = IntegrationThread(
            audio_queue=make_audio_queue(), game_state=gs,
            json_writer=JsonWriter(tmp_path, session.session_id),
            on_hand=captured.append, stop_event=threading.Event(),
            session_repo=srepo, seat_player_map=seat_map,
        )
        _play_one_hand(thread)

        summary = captured[-1].to_dict()
        hand_id = summary["hand_id"]

        # write-through: assign_seat が session レイヤに永続化されている
        assigns = {a.seat_no: a.player_id
                   for a in srepo.list_seat_assignments(session.session_id, hand_id)}
        assert assigns == seat_map

        # HandSummary.players に player_id が additive 埋め込み
        pmap = {p["seat"]: p.get("player_id") for p in summary["players"]}
        assert pmap[1] == seat_map[1]
        assert pmap[2] == seat_map[2]

    def test_output_conforms_to_hand_schema(self, tmp_path: Path):
        jsonschema = pytest.importorskip("jsonschema")
        _, srepo, session, seat_map = _registry_and_session(tmp_path)
        gs = GameStateManager(_players(2), sb=100, bb=200)
        captured: list = []
        thread = IntegrationThread(
            audio_queue=make_audio_queue(), game_state=gs,
            json_writer=JsonWriter(tmp_path, session.session_id),
            on_hand=captured.append, stop_event=threading.Event(),
            session_repo=srepo, seat_player_map=seat_map,
        )
        _play_one_hand(thread)
        schema = json.loads((_SCHEMAS / "hand.schema.json").read_text(encoding="utf-8"))
        # player_id(UUID hex) を含む出力が hand schema(player_id pattern) を満たす
        jsonschema.Draft202012Validator(schema).validate(captured[-1].to_dict())


class TestSessionLayerOffUnchanged:
    def test_no_player_id_key_when_disconnected(self, tmp_path: Path):
        gs = GameStateManager(_players(2), sb=100, bb=200)
        captured: list = []
        thread = IntegrationThread(  # session_repo/seat_player_map 無し = 従来動作
            audio_queue=make_audio_queue(), game_state=gs,
            json_writer=JsonWriter(tmp_path, "legacy-session"),
            on_hand=captured.append, stop_event=threading.Event(),
        )
        _play_one_hand(thread)
        for p in captured[-1].to_dict()["players"]:
            assert "player_id" not in p   # legacy: キー不在（byte 互換）
