"""tests/test_session_repository.py

Phase S2: session レイヤと hand-based seat assignment の core テスト。

検査対象（`docs/contracts/session-seating.md` / ADR-0006 / error-shapes.md）:
- session create / list / get / close
- persistence roundtrip（再起動相当の再ロード）
- assign seat success / duplicate seat reject / player already seated reject
- unknown player reject / unknown session reject / invalid seat reject
- resolve seat map / hand_ref / current seating
- code↔contract 整合（生成オブジェクトが schema に適合する）
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.player_repository import PlayerRepository
from core.session_repository import (
    InvalidSeatError,
    PlayerAlreadySeatedError,
    SeatTakenError,
    SessionAlreadyClosedError,
    SessionClosedError,
    SessionNotFoundError,
    SessionRepository,
    UnknownPlayerError,
)


@pytest.fixture
def players(tmp_path: Path) -> PlayerRepository:
    repo = PlayerRepository(path=tmp_path / "players.json")
    repo.create_player("Alice")
    repo.create_player("Bob")
    repo.create_player("Carol")
    return repo


@pytest.fixture
def repo(tmp_path: Path, players: PlayerRepository) -> SessionRepository:
    return SessionRepository(path=tmp_path / "sessions.json", player_repo=players)


def _pid(players: PlayerRepository, name: str) -> str:
    return next(p.player_id for p in players.list_players() if p.display_name == name)


# ――― session CRUD ―――

def test_create_list_get_session(repo: SessionRepository):
    s1 = repo.create_session(label="Friday #1")
    s2 = repo.create_session()
    assert s1.status == "open"
    assert s1.session_id != s2.session_id
    assert s1.ended_at is None
    assert [s.session_id for s in repo.list_sessions()] == [s1.session_id, s2.session_id]
    assert repo.get_session(s1.session_id).label == "Friday #1"


def test_get_unknown_session_rejected(repo: SessionRepository):
    with pytest.raises(SessionNotFoundError):
        repo.get_session("deadbeef")


def test_close_session_and_double_close(repo: SessionRepository):
    s = repo.create_session()
    closed = repo.close_session(s.session_id)
    assert closed.status == "closed"
    assert closed.ended_at is not None
    with pytest.raises(SessionAlreadyClosedError):
        repo.close_session(s.session_id)


def test_persistence_roundtrip(tmp_path: Path, players: PlayerRepository):
    db = tmp_path / "sessions.json"
    repo = SessionRepository(path=db, player_repo=players)
    s = repo.create_session(label="Persist", blinds={"sb": 100, "bb": 200})
    repo.assign_seat(s.session_id, 1, 3, _pid(players, "Alice"))
    repo.assign_seat(s.session_id, 1, 5, _pid(players, "Bob"))
    repo.close_session(s.session_id)

    reloaded = SessionRepository(path=db, player_repo=players)
    got = reloaded.get_session(s.session_id)
    assert got.label == "Persist"
    assert got.status == "closed"
    assert got.blinds == {"sb": 100, "bb": 200}
    assert reloaded.resolve_seat_map_for_hand(s.session_id, 1) == {
        3: _pid(players, "Alice"),
        5: _pid(players, "Bob"),
    }


# ――― seat assignment ―――

def test_assign_seat_success(repo: SessionRepository, players: PlayerRepository):
    s = repo.create_session()
    a = repo.assign_seat(s.session_id, 7, 4, _pid(players, "Alice"))
    assert a.session_id == s.session_id
    assert a.hand_id == 7
    assert a.seat_no == 4
    assert a.player_id == _pid(players, "Alice")
    assert repo.list_seat_assignments(s.session_id, 7) == [a]


def test_duplicate_seat_rejected(repo: SessionRepository, players: PlayerRepository):
    s = repo.create_session()
    repo.assign_seat(s.session_id, 1, 3, _pid(players, "Alice"))
    with pytest.raises(SeatTakenError):
        repo.assign_seat(s.session_id, 1, 3, _pid(players, "Bob"))


def test_player_twice_in_same_hand_rejected(repo: SessionRepository, players: PlayerRepository):
    s = repo.create_session()
    repo.assign_seat(s.session_id, 1, 3, _pid(players, "Alice"))
    with pytest.raises(PlayerAlreadySeatedError):
        repo.assign_seat(s.session_id, 1, 4, _pid(players, "Alice"))


def test_same_player_different_hands_ok(repo: SessionRepository, players: PlayerRepository):
    s = repo.create_session()
    repo.assign_seat(s.session_id, 1, 3, _pid(players, "Alice"))
    a = repo.assign_seat(s.session_id, 2, 5, _pid(players, "Alice"))
    assert a.hand_id == 2 and a.seat_no == 5


def test_unknown_player_rejected(repo: SessionRepository):
    s = repo.create_session()
    with pytest.raises(UnknownPlayerError):
        repo.assign_seat(s.session_id, 1, 3, "ff" * 16)


def test_unknown_session_on_assign_rejected(repo: SessionRepository, players: PlayerRepository):
    with pytest.raises(SessionNotFoundError):
        repo.assign_seat("nope", 1, 3, _pid(players, "Alice"))


def test_assign_to_closed_session_rejected(repo: SessionRepository, players: PlayerRepository):
    s = repo.create_session()
    repo.close_session(s.session_id)
    with pytest.raises(SessionClosedError):
        repo.assign_seat(s.session_id, 1, 3, _pid(players, "Alice"))


@pytest.mark.parametrize("seat_no", [0, 10, -1])
def test_invalid_seat_rejected(repo: SessionRepository, players: PlayerRepository, seat_no: int):
    s = repo.create_session()
    with pytest.raises(InvalidSeatError):
        repo.assign_seat(s.session_id, 1, seat_no, _pid(players, "Alice"))


# ――― resolve / derive ―――

def test_resolve_hand_ref(repo: SessionRepository, players: PlayerRepository):
    s = repo.create_session()
    repo.assign_seat(s.session_id, 5, 3, _pid(players, "Alice"))
    repo.assign_seat(s.session_id, 5, 1, _pid(players, "Bob"))
    ref = repo.resolve_hand_ref(s.session_id, 5)
    assert ref.session_id == s.session_id
    assert ref.hand_id == 5
    assert ref.started_at
    assert ref.seat_assignments == [
        {"seat_no": 1, "player_id": _pid(players, "Bob")},
        {"seat_no": 3, "player_id": _pid(players, "Alice")},
    ]


def test_resolve_hand_ref_unknown_hand_rejected(repo: SessionRepository):
    s = repo.create_session()
    with pytest.raises(SessionNotFoundError):
        repo.resolve_hand_ref(s.session_id, 99)


def test_current_seating_uses_latest_hand(repo: SessionRepository, players: PlayerRepository):
    s = repo.create_session()
    repo.assign_seat(s.session_id, 1, 3, _pid(players, "Alice"))
    repo.assign_seat(s.session_id, 2, 6, _pid(players, "Bob"))
    seats = repo.current_seating(s.session_id)
    assert [(sa.seat_no, sa.player_id) for sa in seats] == [(6, _pid(players, "Bob"))]


def test_current_seating_empty_without_hands(repo: SessionRepository):
    s = repo.create_session()
    assert repo.current_seating(s.session_id) == []


# ――― code ↔ contract 整合 ―――

_SCHEMAS = Path(__file__).parent.parent / "docs" / "contracts" / "schemas"


def _validate(model: str, payload: dict) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((_SCHEMAS / f"{model}.schema.json").read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema).validate(payload)


def test_core_session_matches_contract(repo: SessionRepository, players: PlayerRepository):
    s = repo.create_session(label="contract", blinds={"sb": 1, "bb": 2})
    _validate("session", s.to_dict())
    closed = repo.close_session(s.session_id)
    _validate("session", closed.to_dict())

    s2 = repo.create_session()
    a = repo.assign_seat(s2.session_id, 1, 2, _pid(players, "Alice"))
    _validate("seat_assignment", a.to_dict())
    _validate("hand_ref", repo.resolve_hand_ref(s2.session_id, 1).to_dict())
