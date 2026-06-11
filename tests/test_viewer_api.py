"""tests/test_viewer_api.py

M1 viewer API の HTTP テスト (ADR-0013, docs/contracts/viewer-api.md)。

検査対象:
- 各 endpoint の 200 応答が契約どおりの envelope を返す
- 404 が error-shapes.md の論理形 {"code": "not_found", "message": ...} を返す
- 応答が対応 schema（player 1.0 / hand 1.0 / player_session_summary 0.x）に適合する
  (code↔contract drift, test_contracts.py と同 idiom)

fastapi / httpx (TestClient) / jsonschema 未導入環境では skip
（CI には requirements-dev.txt で導入し skip 0）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
jsonschema = pytest.importorskip("jsonschema")

from fastapi.testclient import TestClient  # noqa: E402

from api.server import create_app  # noqa: E402
from core.hand_log import HandSummary  # noqa: E402
from core.ledger_repository import LedgerRepository  # noqa: E402
from core.player_repository import PlayerRepository  # noqa: E402
from core.session_repository import SessionRepository  # noqa: E402

_SCHEMAS = Path(__file__).parent.parent / "docs" / "contracts" / "schemas"


def _schema(name: str) -> dict:
    return json.loads((_SCHEMAS / f"{name}.schema.json").read_text(encoding="utf-8"))


def _validate(instance: dict, schema_name: str) -> None:
    jsonschema.Draft202012Validator(_schema(schema_name)).validate(instance)


def _hand_dict(hand_id: int, session_id: str) -> dict:
    return HandSummary(
        hand_id=hand_id, session_id=session_id,
        started_at="2026-06-10T20:00:00", ended_at="2026-06-10T20:01:00",
        blinds={"sb": 100, "bb": 200}, board=[], board_source="",
        players=[{"seat": 1, "name": "P1", "hole_cards": None, "hole_cards_source": "",
                  "stack_start": 1000, "stack_end": 900, "result": -100}],
        pot_total=300, winner_seat=1, actions=[], review_required=False,
    ).to_dict()


@pytest.fixture
def env(tmp_path: Path) -> dict:
    """実 repository + 合成 hand log で構築した TestClient 一式。"""
    players = PlayerRepository(path=tmp_path / "players.json")
    alice = players.create_player("Alice")
    bob = players.create_player("Bob")
    sessions = SessionRepository(path=tmp_path / "sessions.json", player_repo=players)
    s = sessions.create_session(label="Friday #1")
    sessions.assign_seat(s.session_id, 1, 1, alice.player_id)
    sessions.assign_seat(s.session_id, 2, 1, alice.player_id)
    sessions.assign_seat(s.session_id, 2, 2, bob.player_id)

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    log = {"session_id": s.session_id,
           "hands": [_hand_dict(1, s.session_id), _hand_dict(2, s.session_id)]}
    (log_dir / f"{s.session_id}.json").write_text(
        json.dumps(log, ensure_ascii=False), encoding="utf-8"
    )

    ledger = LedgerRepository(path=tmp_path / "ledger.json", session_repo=sessions)
    ledger.add_entry(s.session_id, alice.player_id, "buy_in", 10000)
    ledger.add_entry(s.session_id, alice.player_id, "order", 1500,
                     item_name="ジントニック", unit_amount=500, quantity=3)
    ledger.add_entry(s.session_id, bob.player_id, "buy_in", 20000)

    client = TestClient(create_app(players, sessions, log_dir, ledger_repo=ledger))
    return {"client": client, "alice": alice, "bob": bob, "session": s}


def test_health(env: dict):
    res = env["client"].get("/api/health")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert "version" in body


def test_players_list_and_get_match_contract(env: dict):
    res = env["client"].get("/api/players")
    assert res.status_code == 200
    players = res.json()["players"]
    assert [p["display_name"] for p in players] == ["Alice", "Bob"]
    for p in players:
        _validate(p, "player")

    res = env["client"].get(f"/api/players/{env['alice'].player_id}")
    assert res.status_code == 200
    _validate(res.json(), "player")


def test_player_sessions_match_contract(env: dict):
    res = env["client"].get(f"/api/players/{env['alice'].player_id}/sessions")
    assert res.status_code == 200
    sessions = res.json()["sessions"]
    assert len(sessions) == 1
    assert sessions[0]["session_id"] == env["session"].session_id
    assert sessions[0]["hands_played"] == 2
    _validate(sessions[0], "player_session_summary")

    res = env["client"].get(f"/api/players/{env['bob'].player_id}/sessions")
    assert res.json()["sessions"][0]["hands_played"] == 1


def test_player_hands_filtered_and_match_contract(env: dict):
    sid = env["session"].session_id
    res = env["client"].get(f"/api/players/{env['bob'].player_id}/sessions/{sid}/hands")
    assert res.status_code == 200
    hands = res.json()["hands"]
    assert [h["hand_id"] for h in hands] == [2]
    for h in hands:
        _validate(h, "hand")


def test_get_hand_match_contract(env: dict):
    sid = env["session"].session_id
    res = env["client"].get(f"/api/sessions/{sid}/hands/1")
    assert res.status_code == 200
    assert res.json()["hand_id"] == 1
    _validate(res.json(), "hand")


def test_player_ledger_entries_and_summary(env: dict):
    """M4/S3a (ADR-0014): 会計参照 endpoint。entries は schema 適合、summary は中間集計。"""
    sid = env["session"].session_id
    res = env["client"].get(f"/api/players/{env['alice'].player_id}/sessions/{sid}/ledger")
    assert res.status_code == 200
    body = res.json()
    assert [e["kind"] for e in body["entries"]] == ["buy_in", "order"]
    for e in body["entries"]:
        _validate(e, "ledger_entry")
    assert body["summary"] == {
        "buy_in_total": 10000, "order_total": 1500,
        "adjustment_total": 0, "total_due": 11500,
    }
    # bob は自分の entry だけ見える
    res = env["client"].get(f"/api/players/{env['bob'].player_id}/sessions/{sid}/ledger")
    assert res.json()["summary"]["total_due"] == 20000


@pytest.mark.parametrize("path", [
    "/api/players/{missing_pid}",
    "/api/players/{missing_pid}/sessions",
    "/api/players/{missing_pid}/sessions/{sid}/hands",
    "/api/players/{alice_pid}/sessions/{missing_sid}/hands",
    "/api/players/{missing_pid}/sessions/{sid}/ledger",
    "/api/players/{alice_pid}/sessions/{missing_sid}/ledger",
    "/api/sessions/{sid}/hands/999",
    "/api/sessions/{missing_sid}/hands/1",
])
def test_not_found_error_shape(env: dict, path: str):
    url = path.format(
        missing_pid="f" * 32,
        alice_pid=env["alice"].player_id,
        sid=env["session"].session_id,
        missing_sid="deadbeef",
    )
    res = env["client"].get(url)
    assert res.status_code == 404
    body = res.json()
    assert body["code"] == "not_found"
    assert isinstance(body["message"], str) and body["message"]
