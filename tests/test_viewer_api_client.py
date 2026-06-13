"""tests/test_viewer_api_client.py

Phase S5 (ADR-0020) — viewer API ↔ Python client の round-trip 契約 test。

`api/client.py:ViewerApiClient` を、FastAPI app に対して in-process（httpx ASGITransport,
ソケットなし）で round-trip させ、boundary（API 契約 ↔ client）の drift を検知する。

- read endpoints が repository の中身と一致して返ること（players / sessions / hands / ledger / menu）。
- error が error-shape の code を保持した `ViewerApiError` に変換されること（not_found）。
- read-only モード（orders_writable=False）の注文 POST が `orders_unavailable` になること。
- 書き込み所有モード（orders_writable=True）の注文 POST が pending を作り ledger には書かないこと。

fastapi / httpx / jsonschema 未導入環境では skip。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from api.client import ViewerApiClient, ViewerApiError  # noqa: E402
from api.server import create_app  # noqa: E402
from core.hand_log import HandSummary  # noqa: E402
from core.ledger_repository import LedgerRepository  # noqa: E402
from core.menu import MenuMaster  # noqa: E402
from core.order_request_repository import OrderRequestRepository  # noqa: E402
from core.player_repository import PlayerRepository  # noqa: E402
from core.session_repository import SessionRepository  # noqa: E402


def _hand_dict(hand_id: int, session_id: str) -> dict:
    return HandSummary(
        hand_id=hand_id, session_id=session_id,
        started_at="2026-06-10T20:00:00", ended_at="2026-06-10T20:01:00",
        blinds={"sb": 100, "bb": 200}, board=[], board_source="",
        players=[{"seat": 1, "name": "P1", "hole_cards": None, "hole_cards_source": "",
                  "stack_start": 1000, "stack_end": 900, "result": -100}],
        pot_total=300, winner_seat=1, actions=[], review_required=False,
    ).to_dict()


def _build(tmp_path: Path, *, orders_writable: bool) -> dict:
    players = PlayerRepository(path=tmp_path / "players.json")
    alice = players.create_player("Alice")
    bob = players.create_player("Bob")
    sessions = SessionRepository(path=tmp_path / "sessions.json", player_repo=players)
    s = sessions.create_session(label="Friday #1")
    sessions.assign_seat(s.session_id, 1, 1, alice.player_id)
    sessions.assign_seat(s.session_id, 2, 2, bob.player_id)

    log_dir = tmp_path / "logs"
    log_dir.mkdir(exist_ok=True)
    (log_dir / f"{s.session_id}.json").write_text(
        json.dumps({"session_id": s.session_id, "hands": [_hand_dict(1, s.session_id)]},
                   ensure_ascii=False), encoding="utf-8")

    ledger = LedgerRepository(path=tmp_path / "ledger.json", session_repo=sessions)
    ledger.add_entry(s.session_id, alice.player_id, "buy_in", cash_amount=10000)

    menu = MenuMaster(path=_write_menu(tmp_path))
    orders = OrderRequestRepository(path=tmp_path / "order_requests.json", session_repo=sessions)

    app = create_app(players, sessions, log_dir, ledger_repo=ledger,
                     order_repo=orders, menu=menu, orders_writable=orders_writable)
    # TestClient は ASGI を in-process 処理する sync な httpx.Client 互換。
    # ViewerApiClient はこれを transport として注入され、実ソケットなしで round-trip する。
    http = TestClient(app)
    client = ViewerApiClient(client=http)
    return {"client": client, "alice": alice, "bob": bob, "session": s, "ledger": ledger}


def _write_menu(tmp_path: Path) -> Path:
    p = tmp_path / "menu.json"
    p.write_text(json.dumps({"items": [{"item_name": "ビール", "unit_amount": 700}]},
                            ensure_ascii=False), encoding="utf-8")
    return p


@pytest.fixture
def env(tmp_path: Path):
    e = _build(tmp_path, orders_writable=True)
    yield e
    e["client"].close()


def test_round_trip_reads_match_repository(env: dict):
    client, alice, session = env["client"], env["alice"], env["session"]

    assert client.health()["status"] == "ok"
    assert [p["display_name"] for p in client.list_players()] == ["Alice", "Bob"]
    assert client.get_player(alice.player_id)["player_id"] == alice.player_id

    sessions = client.list_player_sessions(alice.player_id)
    assert [s["session_id"] for s in sessions] == [session.session_id]
    assert sessions[0]["hands_played"] == 1

    hands = client.list_player_hands(alice.player_id, session.session_id)
    assert [h["hand_id"] for h in hands] == [1]
    assert client.get_hand(session.session_id, 1)["hand_id"] == 1

    ledger = client.get_player_session_ledger(alice.player_id, session.session_id)
    assert ledger["entries"][0]["kind"] == "buy_in"
    assert ledger["summary"]["cash_in_total"] == 10000
    assert [i["item_name"] for i in client.get_menu()] == ["ビール"]


def test_error_is_typed_with_code(env: dict):
    client = env["client"]
    with pytest.raises(ViewerApiError) as ei:
        client.get_player("f" * 32)
    assert ei.value.code == "not_found"
    assert ei.value.status_code == 404


def test_order_round_trip_writes_pending_only(env: dict):
    client, alice, session, ledger = (
        env["client"], env["alice"], env["session"], env["ledger"])
    before = len(ledger.list_entries(session.session_id))
    req = client.create_order_request(alice.player_id, session.session_id, "ビール", 2)
    assert req["status"] == "pending"
    # staff 確定前は ledger に書かれない（ADR-0018 / ADR-0020 single-writer）
    assert len(ledger.list_entries(session.session_id)) == before
    listed = client.list_order_requests(alice.player_id, session.session_id)
    assert [r["item_name"] for r in listed] == ["ビール"]

    with pytest.raises(ViewerApiError) as ei:
        client.create_order_request(alice.player_id, session.session_id, "存在しない", 1)
    assert ei.value.code == "unknown_item"


def test_readonly_mode_rejects_order_post(tmp_path: Path):
    e = _build(tmp_path, orders_writable=False)
    try:
        with pytest.raises(ViewerApiError) as ei:
            e["client"].create_order_request(
                e["alice"].player_id, e["session"].session_id, "ビール", 1)
        assert (ei.value.code, ei.value.status_code) == ("orders_unavailable", 503)
    finally:
        e["client"].close()
