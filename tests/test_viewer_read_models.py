"""tests/test_viewer_read_models.py

M1 viewer API の read model テスト (ADR-0013, docs/contracts/viewer-api.md)。

検査対象（HTTP なし・fastapi 非依存）:
- list_player_sessions: 着席 session のみ / hands_played 集計 / 未着席 player は空
- list_player_hands: seat_assignment 起点の join / hand log 欠落の gracefully-empty /
  hand log に player_id が無くても seat_assignment があれば帰属する（E3 前提を置かない）
- get_hand: legacy session_id（session レイヤ未登録）でも log があれば返す / 不在は not_found
- SessionRepository.list_hand_ids（M1 additive）
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from api.read_models import (
    HandNotFoundError,
    get_hand,
    list_player_hands,
    list_player_sessions,
)
from core.hand_log import HandSummary
from core.player_repository import PlayerRepository
from core.session_repository import SessionNotFoundError, SessionRepository


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


@pytest.fixture
def log_dir(tmp_path: Path) -> Path:
    d = tmp_path / "logs"
    d.mkdir()
    return d


def _pid(players: PlayerRepository, name: str) -> str:
    return next(p.player_id for p in players.list_players() if p.display_name == name)


def _hand_dict(hand_id: int, session_id: str) -> dict:
    # 実 hand log と同形（hand schema 1.0）。players[].player_id は意図的に持たない
    # （E3 前の legacy ログ形。帰属判定は seat_assignment 起点であることを検証する）。
    return HandSummary(
        hand_id=hand_id, session_id=session_id,
        started_at="2026-06-10T20:00:00", ended_at="2026-06-10T20:01:00",
        blinds={"sb": 100, "bb": 200}, board=[], board_source="",
        players=[{"seat": 1, "name": "P1", "hole_cards": None, "hole_cards_source": "",
                  "stack_start": 1000, "stack_end": 900, "result": -100}],
        pot_total=300, winner_seat=1, actions=[], review_required=False,
    ).to_dict()


def _write_log(log_dir: Path, session_id: str, hand_ids: list[int]) -> None:
    data = {"session_id": session_id, "hands": [_hand_dict(h, session_id) for h in hand_ids]}
    (log_dir / f"{session_id}.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )


# ――― list_hand_ids (M1 additive) ―――

def test_list_hand_ids_sorted(repo: SessionRepository, players: PlayerRepository):
    s = repo.create_session()
    repo.assign_seat(s.session_id, 5, 1, _pid(players, "Alice"))
    repo.assign_seat(s.session_id, 2, 1, _pid(players, "Bob"))
    assert repo.list_hand_ids(s.session_id) == [2, 5]


def test_list_hand_ids_unknown_session(repo: SessionRepository):
    with pytest.raises(SessionNotFoundError):
        repo.list_hand_ids("deadbeef")


# ――― list_player_sessions ―――

def test_list_player_sessions_counts_and_filters(
    repo: SessionRepository, players: PlayerRepository
):
    alice, bob = _pid(players, "Alice"), _pid(players, "Bob")
    s1 = repo.create_session(label="Friday #1")
    s2 = repo.create_session(label="Friday #2")
    repo.assign_seat(s1.session_id, 1, 1, alice)
    repo.assign_seat(s1.session_id, 1, 2, bob)
    repo.assign_seat(s1.session_id, 2, 1, alice)
    repo.assign_seat(s2.session_id, 1, 3, bob)

    alice_sessions = list_player_sessions(alice, repo)
    assert [s["session_id"] for s in alice_sessions] == [s1.session_id]
    assert alice_sessions[0]["hands_played"] == 2
    assert alice_sessions[0]["label"] == "Friday #1"

    bob_sessions = list_player_sessions(bob, repo)
    assert {s["session_id"] for s in bob_sessions} == {s1.session_id, s2.session_id}


def test_list_player_sessions_unseated_player_empty(
    repo: SessionRepository, players: PlayerRepository
):
    repo.create_session()
    assert list_player_sessions(_pid(players, "Carol"), repo) == []


# ――― list_player_hands ―――

def test_list_player_hands_joins_by_seat_assignment(
    repo: SessionRepository, players: PlayerRepository, log_dir: Path
):
    alice, bob = _pid(players, "Alice"), _pid(players, "Bob")
    s = repo.create_session()
    repo.assign_seat(s.session_id, 1, 1, alice)
    repo.assign_seat(s.session_id, 2, 1, alice)
    repo.assign_seat(s.session_id, 2, 2, bob)
    repo.assign_seat(s.session_id, 3, 2, bob)
    _write_log(log_dir, s.session_id, [1, 2, 3])

    # hand log 側に player_id が無くても seat_assignment があれば帰属する
    alice_hands = list_player_hands(alice, s.session_id, repo, log_dir)
    assert [h["hand_id"] for h in alice_hands] == [1, 2]
    bob_hands = list_player_hands(bob, s.session_id, repo, log_dir)
    assert [h["hand_id"] for h in bob_hands] == [2, 3]


def test_list_player_hands_missing_log_is_empty(
    repo: SessionRepository, players: PlayerRepository, log_dir: Path
):
    alice = _pid(players, "Alice")
    s = repo.create_session()
    repo.assign_seat(s.session_id, 1, 1, alice)
    # hand log 不在（E3 前の実運用状態）→ gracefully-empty
    assert list_player_hands(alice, s.session_id, repo, log_dir) == []


def test_list_player_hands_skips_hands_absent_from_log(
    repo: SessionRepository, players: PlayerRepository, log_dir: Path
):
    alice = _pid(players, "Alice")
    s = repo.create_session()
    repo.assign_seat(s.session_id, 1, 1, alice)
    repo.assign_seat(s.session_id, 9, 1, alice)
    _write_log(log_dir, s.session_id, [1])  # hand 9 は log 未着
    assert [h["hand_id"] for h in list_player_hands(alice, s.session_id, repo, log_dir)] == [1]


def test_list_player_hands_unknown_session(
    repo: SessionRepository, players: PlayerRepository, log_dir: Path
):
    with pytest.raises(SessionNotFoundError):
        list_player_hands(_pid(players, "Alice"), "deadbeef", repo, log_dir)


# ――― get_hand ―――

def test_get_hand_found_and_not_found(log_dir: Path):
    # session レイヤ未登録の legacy session_id（timestamp 形式）でも log があれば返す
    session_id = "2026-06-10_200000_session1"
    _write_log(log_dir, session_id, [1, 2])
    hand = get_hand(session_id, 2, log_dir)
    assert hand["hand_id"] == 2
    with pytest.raises(HandNotFoundError):
        get_hand(session_id, 99, log_dir)
    with pytest.raises(HandNotFoundError):
        get_hand("no_such_session", 1, log_dir)
