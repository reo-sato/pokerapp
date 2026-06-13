"""tests/test_viewer_api_staff.py

Phase S5 staff write API (ADR-0021) — viewer API ↔ Python staff client の round-trip test。

`api/server.py` の `/api/staff/...` エンドポイントを、staff shared token 認証付きで
in-process（TestClient）round-trip し、

- 正しいトークン + orders_writable=True で会計 write 一通り
  （ledger 追加 / 注文確定・却下 / settlement 確定 / paid 設定 / 中間集計・注文 queue read）、
- 認可失敗（no token / wrong token → 401、staff_token="" → 403、
  read-only モードの write → 503）

を検証する。fastapi / httpx 未導入環境では skip。
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
from core.ledger_repository import LedgerRepository  # noqa: E402
from core.menu import MenuMaster  # noqa: E402
from core.order_request_repository import OrderRequestRepository  # noqa: E402
from core.player_repository import PlayerRepository  # noqa: E402
from core.session_repository import SessionRepository  # noqa: E402

_TOKEN = "s3cr3t-staff-token"


def _write_menu(tmp_path: Path) -> Path:
    p = tmp_path / "menu.json"
    p.write_text(json.dumps({"items": [{"item_name": "ビール", "unit_amount": 700}]},
                            ensure_ascii=False), encoding="utf-8")
    return p


def _build(tmp_path: Path, *, orders_writable: bool, staff_token: str | None) -> dict:
    players = PlayerRepository(path=tmp_path / "players.json")
    alice = players.create_player("Alice")
    bob = players.create_player("Bob")
    sessions = SessionRepository(path=tmp_path / "sessions.json", player_repo=players)
    s = sessions.create_session(label="Friday #1")
    sessions.assign_seat(s.session_id, 1, 1, alice.player_id)
    sessions.assign_seat(s.session_id, 2, 2, bob.player_id)

    log_dir = tmp_path / "logs"
    log_dir.mkdir(exist_ok=True)

    ledger = LedgerRepository(path=tmp_path / "ledger.json", session_repo=sessions)
    menu = MenuMaster(path=_write_menu(tmp_path))
    orders = OrderRequestRepository(path=tmp_path / "order_requests.json", session_repo=sessions)

    app = create_app(players, sessions, log_dir, ledger_repo=ledger,
                     order_repo=orders, menu=menu, orders_writable=orders_writable,
                     staff_token=staff_token)
    http = TestClient(app)
    # player order POST 用の no-auth client + staff write 用の token client。
    player_client = ViewerApiClient(client=http)
    staff_client = ViewerApiClient(client=http, staff_token=_TOKEN)
    return {
        "player_client": player_client, "staff_client": staff_client,
        "alice": alice, "bob": bob, "session": s, "sessions": sessions,
        "ledger": ledger, "orders": orders,
    }


@pytest.fixture
def env(tmp_path: Path):
    e = _build(tmp_path, orders_writable=True, staff_token=_TOKEN)
    yield e
    e["player_client"].close()


def test_staff_write_full_flow(env: dict):
    staff, player = env["staff_client"], env["player_client"]
    alice, bob, session = env["alice"], env["bob"], env["session"]
    sessions, ledger = env["sessions"], env["ledger"]

    # (a) ledger 追加（staff write）
    entry = staff.add_ledger_entry(session.session_id, alice.player_id, "buy_in",
                                   cash_amount=10000)
    assert entry["kind"] == "buy_in" and entry["cash_amount"] == 10000
    assert len(ledger.list_entries(session.session_id)) == 1

    # player が注文 POST（no-auth）→ staff が確定（ledger に order entry が増える）
    req = player.create_order_request(alice.player_id, session.session_id, "ビール", 2)
    confirmed = staff.confirm_order(req["request_id"], 700)
    assert confirmed["status"] == "confirmed"
    order_entries = [e for e in ledger.list_entries(session.session_id) if e.kind == "order"]
    assert len(order_entries) == 1 and order_entries[0].cash_amount == 1400

    # 別の注文を reject
    req2 = player.create_order_request(bob.player_id, session.session_id, "ビール", 1)
    rejected = staff.reject_order(req2["request_id"])
    assert rejected["status"] == "rejected"

    # staff の注文 queue read（全 player）
    queue = staff.list_session_order_requests(session.session_id)
    assert {r["status"] for r in queue} == {"confirmed", "rejected"}
    pending_only = staff.list_session_order_requests(session.session_id, status="pending")
    assert pending_only == []

    # 中間集計 read（speculative）
    spec = staff.compute_settlement(session.session_id)
    alice_row = next(r for r in spec if r["player_id"] == alice.player_id)
    assert alice_row["cash_in_total"] == 10000 and alice_row["order_total"] == 1400

    # close → commit settlement → paid 設定
    sessions.close_session(session.session_id)
    committed = staff.commit_settlement(session.session_id)
    assert all(r["payment_status"] == "unpaid" for r in committed)

    updated = staff.set_payment_status(session.session_id, alice.player_id, "paid")
    assert updated["payment_status"] == "paid"

    # compute_settlement が paid を反映
    after = staff.compute_settlement(session.session_id)
    assert next(r for r in after if r["player_id"] == alice.player_id)["payment_status"] == "paid"


def test_no_token_is_unauthorized(env: dict):
    # token を持たない client（staff_token=None）で staff endpoint を叩く → 401
    no_token = ViewerApiClient(client=env["staff_client"]._client)
    with pytest.raises(ViewerApiError) as ei:
        no_token.compute_settlement(env["session"].session_id)
    assert (ei.value.code, ei.value.status_code) == ("unauthorized", 401)


def test_wrong_token_is_unauthorized(env: dict):
    bad = ViewerApiClient(client=env["staff_client"]._client, staff_token="wrong")
    with pytest.raises(ViewerApiError) as ei:
        bad.add_ledger_entry(env["session"].session_id, env["alice"].player_id,
                             "buy_in", cash_amount=100)
    assert (ei.value.code, ei.value.status_code) == ("unauthorized", 401)


def test_staff_writes_disabled_when_token_unset(tmp_path: Path):
    e = _build(tmp_path, orders_writable=True, staff_token="")
    try:
        with pytest.raises(ViewerApiError) as ei:
            e["staff_client"].compute_settlement(e["session"].session_id)
        assert (ei.value.code, ei.value.status_code) == ("staff_writes_disabled", 403)
    finally:
        e["player_client"].close()


def test_readonly_mode_rejects_staff_write_but_allows_staff_read(tmp_path: Path):
    # standalone read-only（orders_writable=False）。token はあるので staff *read* は可、
    # staff *write* は 503 orders_unavailable（単一書き手, ADR-0020/0021）。
    e = _build(tmp_path, orders_writable=False, staff_token=_TOKEN)
    try:
        staff = e["staff_client"]
        # read は通る
        assert staff.compute_settlement(e["session"].session_id) == []
        # write は 503
        with pytest.raises(ViewerApiError) as ei:
            staff.add_ledger_entry(e["session"].session_id, e["alice"].player_id,
                                   "buy_in", cash_amount=100)
        assert (ei.value.code, ei.value.status_code) == ("orders_unavailable", 503)
    finally:
        e["player_client"].close()


def test_staff_ledger_entry_validation_error(env: dict):
    staff, session, alice = env["staff_client"], env["session"], env["alice"]
    # invalid kind → 400 invalid_amount
    with pytest.raises(ViewerApiError) as ei:
        staff.add_ledger_entry(session.session_id, alice.player_id, "bogus_kind",
                               cash_amount=100)
    assert (ei.value.code, ei.value.status_code) == ("invalid_amount", 400)


def test_staff_commit_open_session_is_session_not_closed(env: dict):
    staff, session = env["staff_client"], env["session"]
    with pytest.raises(ViewerApiError) as ei:
        staff.commit_settlement(session.session_id)
    assert (ei.value.code, ei.value.status_code) == ("session_not_closed", 409)


def test_staff_payment_status_not_found(env: dict):
    staff, session, alice = env["staff_client"], env["session"], env["alice"]
    # 未確定 settlement → not_found 404
    with pytest.raises(ViewerApiError) as ei:
        staff.set_payment_status(session.session_id, alice.player_id, "paid")
    assert (ei.value.code, ei.value.status_code) == ("not_found", 404)


def test_staff_confirm_unknown_request_not_found(env: dict):
    with pytest.raises(ViewerApiError) as ei:
        env["staff_client"].confirm_order("f" * 32, 700)
    assert (ei.value.code, ei.value.status_code) == ("not_found", 404)
