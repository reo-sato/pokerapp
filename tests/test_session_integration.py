"""tests/test_session_integration.py

Phase 2.2: hand logger × S2 session/seating レイヤの write-through 接続テスト
（ADR-0008 Pattern A / `docs/contracts/hand-integration.md`）。

検査対象:
- session layer ON: session 作成 → hand 開始で assign_seat → HandSummary に
  session レイヤの session_id(UUID4 hex) と players[i].player_id が additive に入る。
- session layer OFF（rollback path）: SessionRepository を呼ばない。HandSummary に
  player_id を付けず、従来の (JsonWriter) session_id を使う。旧 JSON と構造互換。
- assign_seat 失敗（unknown player 等）でも hand logger は止まらない。

IntegrationThread の hand lifecycle メソッド（``_start_new_hand`` / ``_finalize_hand``）
は同期的なので、スレッドを起動せず直接呼んで検証する。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from core.event_queue import make_audio_queue
from core.game_state import GameStateManager, PlayerState
from core.player_repository import PlayerRepository
from core.session_repository import SessionRepository
from integration.engine import IntegrationThread
from output.json_writer import JsonWriter

_UUID4_HEX = re.compile(r"^[0-9a-f]{32}$")


# ――― fixtures ―――

@pytest.fixture
def players(tmp_path: Path) -> PlayerRepository:
    repo = PlayerRepository(path=tmp_path / "players.json")
    repo.create_player("Alice")
    repo.create_player("Bob")
    return repo


def _pid(players: PlayerRepository, name: str) -> str:
    return next(p.player_id for p in players.list_players() if p.display_name == name)


def _make_game() -> GameStateManager:
    return GameStateManager(
        players=[
            PlayerState(seat=1, name="Alice", stack=10000),
            PlayerState(seat=2, name="Bob", stack=10000),
        ],
        sb=100,
        bb=200,
    )


def _read_hands(writer: JsonWriter) -> list[dict]:
    return json.loads(writer.path.read_text(encoding="utf-8"))["hands"]


# ――― session layer ON ―――

def test_session_layer_on_assigns_seats_and_threads_ids(
    tmp_path: Path, players: PlayerRepository
) -> None:
    session_repo = SessionRepository(
        path=tmp_path / "sessions.json", player_repo=players
    )
    session = session_repo.create_session(blinds={"sb": 100, "bb": 200})
    seating = {1: _pid(players, "Alice"), 2: _pid(players, "Bob")}

    gs = _make_game()
    writer = JsonWriter(log_dir=tmp_path, session_id=session.session_id)
    thread = IntegrationThread(
        audio_queue=make_audio_queue(),
        game_state=gs,
        json_writer=writer,
        session_repo=session_repo,
        session_id=session.session_id,
        seating=seating,
    )

    thread._start_new_hand()  # hand_id -> 1, assign_seat batch
    assert gs.hand_id == 1

    # assign_seat が session レイヤに書き込まれている
    assert session_repo.resolve_seat_map_for_hand(session.session_id, 1) == seating
    hand_ref = session_repo.resolve_hand_ref(session.session_id, 1)
    assert hand_ref.session_id == session.session_id
    assert {sa["seat_no"]: sa["player_id"] for sa in hand_ref.seat_assignments} == seating

    thread._finalize_hand(winner_seat=1)

    hands = _read_hands(writer)
    assert len(hands) == 1
    summary = hands[0]
    # session_id は session レイヤの UUID4 hex
    assert _UUID4_HEX.match(summary["session_id"])
    assert summary["session_id"] == session.session_id
    # players[i].player_id が additive に入る
    by_seat = {p["seat"]: p for p in summary["players"]}
    assert by_seat[1]["player_id"] == seating[1]
    assert by_seat[2]["player_id"] == seating[2]


def test_session_layer_on_carries_seating_across_hands(
    tmp_path: Path, players: PlayerRepository
) -> None:
    session_repo = SessionRepository(
        path=tmp_path / "sessions.json", player_repo=players
    )
    session = session_repo.create_session()
    seating = {1: _pid(players, "Alice"), 2: _pid(players, "Bob")}

    gs = _make_game()
    writer = JsonWriter(log_dir=tmp_path, session_id=session.session_id)
    thread = IntegrationThread(
        audio_queue=make_audio_queue(),
        game_state=gs,
        json_writer=writer,
        session_repo=session_repo,
        session_id=session.session_id,
        seating=seating,
    )

    for _ in range(2):
        thread._start_new_hand()
        thread._finalize_hand(winner_seat=1)

    # 各 hand に独立した seat snapshot が記録される
    assert session_repo.resolve_seat_map_for_hand(session.session_id, 1) == seating
    assert session_repo.resolve_seat_map_for_hand(session.session_id, 2) == seating


# ――― session layer OFF (rollback path) ―――

def test_session_layer_off_is_legacy_behaviour(tmp_path: Path) -> None:
    gs = _make_game()
    writer = JsonWriter(log_dir=tmp_path, session_id="2026-06-03_120000_session1")
    thread = IntegrationThread(
        audio_queue=make_audio_queue(),
        game_state=gs,
        json_writer=writer,
        # session_repo / session_id を渡さない = 従来動作
    )

    thread._start_new_hand()
    thread._finalize_hand(winner_seat=1)

    hands = _read_hands(writer)
    summary = hands[0]
    # 従来の (JsonWriter) session_id を使う
    assert summary["session_id"] == "2026-06-03_120000_session1"
    # player_id キーは付与されない（旧 reader 互換）
    for p in summary["players"]:
        assert "player_id" not in p


def test_session_layer_off_matches_pre_phase22_keys(tmp_path: Path) -> None:
    """OFF 時の players[i] のキー集合が Phase 2.2 以前と一致する（additive のみ確認）。"""
    gs = _make_game()
    writer = JsonWriter(log_dir=tmp_path, session_id="s_legacy")
    thread = IntegrationThread(
        audio_queue=make_audio_queue(),
        game_state=gs,
        json_writer=writer,
    )
    thread._start_new_hand()
    thread._finalize_hand(winner_seat=1)

    summary = _read_hands(writer)[0]
    expected_keys = {
        "seat", "name", "hole_cards", "hole_cards_source",
        "stack_start", "stack_end", "result",
    }
    for p in summary["players"]:
        assert set(p.keys()) == expected_keys


# ――― 耐障害性 ―――

def test_assign_seat_failure_does_not_break_hand(
    tmp_path: Path, players: PlayerRepository
) -> None:
    """unknown player_id でも hand logger は止まらず finalize できる。"""
    session_repo = SessionRepository(
        path=tmp_path / "sessions.json", player_repo=players
    )
    session = session_repo.create_session()
    seating = {1: "ffffffffffffffffffffffffffffffff"}  # registry 非実在

    gs = _make_game()
    writer = JsonWriter(log_dir=tmp_path, session_id=session.session_id)
    thread = IntegrationThread(
        audio_queue=make_audio_queue(),
        game_state=gs,
        json_writer=writer,
        session_repo=session_repo,
        session_id=session.session_id,
        seating=seating,
    )

    # 例外を投げずに hand が完了する
    thread._start_new_hand()
    thread._finalize_hand(winner_seat=1)

    # 失敗した seat は session レイヤに記録されない
    assert session_repo.resolve_seat_map_for_hand(session.session_id, 1) == {}
    assert len(_read_hands(writer)) == 1
