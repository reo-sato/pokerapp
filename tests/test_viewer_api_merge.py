"""tests/test_viewer_api_merge.py

ADR-0030: player merge の viewer/staff API round-trip。fastapi/httpx 未導入は skip。

staff merge エンドポイント + merge 後に viewer の per-player read が survivor 視点で
absorbed のデータも拾うこと + login が survivor principal を返すことを検証する。
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
from core.player_credential_repository import PlayerCredentialRepository  # noqa: E402
from core.player_repository import PlayerRepository  # noqa: E402
from core.session_repository import SessionRepository  # noqa: E402

_TOKEN = "staff-token"


def _build(tmp_path: Path, *, player_auth: str = "off") -> dict:
    players = PlayerRepository(path=tmp_path / "players.json")
    venue = players.create_player("Bob (venue)")
    cloud = players.create_player("Bob (cloud)")
    sessions = SessionRepository(path=tmp_path / "sessions.json", player_repo=players)
    s = sessions.create_session(label="Fri")
    # venue は hand 1 に着席、cloud は hand 2 に着席（別 ID で別 hand）。
    sessions.assign_seat(s.session_id, 1, 1, venue.player_id)
    sessions.assign_seat(s.session_id, 2, 3, cloud.player_id)

    log_dir = tmp_path / "logs"
    log_dir.mkdir(exist_ok=True)
    (log_dir / f"{s.session_id}.json").write_text(json.dumps({"hands": [
        {"hand_id": 1, "players": []}, {"hand_id": 2, "players": []},
    ]}), encoding="utf-8")

    ledger = LedgerRepository(path=tmp_path / "ledger.json",
                              session_repo=sessions, player_repo=players)
    ledger.add_entry(s.session_id, venue.player_id, "buy_in", cash_amount=10000)
    ledger.add_entry(s.session_id, cloud.player_id, "buy_in", cash_amount=5000)
    (tmp_path / "menu.json").write_text(json.dumps(
        {"items": [{"item_name": "beer", "unit_amount": 700}]}), encoding="utf-8")
    orders = OrderRequestRepository(path=tmp_path / "orders.json",
                                    session_repo=sessions, player_repo=players)
    cred = PlayerCredentialRepository(path=tmp_path / "creds.json", iterations=1000)

    app = create_app(players, sessions, log_dir, ledger_repo=ledger, order_repo=orders,
                     menu=MenuMaster(path=tmp_path / "menu.json"), orders_writable=True,
                     staff_token=_TOKEN, credential_repo=cred, player_auth=player_auth)
    http = TestClient(app)
    return {"http": http, "venue": venue, "cloud": cloud, "session": s,
            "players": players, "cred": cred}


def test_staff_merge_endpoint(tmp_path: Path):
    env = _build(tmp_path)
    staff = ViewerApiClient(client=env["http"], staff_token=_TOKEN)
    res = staff.merge_players(env["venue"].player_id, env["cloud"].player_id)
    assert res["survivor_id"] == env["venue"].player_id
    assert res["merged_into"] == env["venue"].player_id
    # registry の一覧（read）は tombstone を隠す。
    listed = {p["player_id"] for p in staff.list_players()}
    assert listed == {env["venue"].player_id}


def test_staff_merge_requires_token(tmp_path: Path):
    env = _build(tmp_path)
    no_token = ViewerApiClient(client=env["http"])
    with pytest.raises(ViewerApiError) as ei:
        no_token.merge_players(env["venue"].player_id, env["cloud"].player_id)
    assert (ei.value.code, ei.value.status_code) == ("unauthorized", 401)


def test_staff_merge_invalid(tmp_path: Path):
    env = _build(tmp_path)
    staff = ViewerApiClient(client=env["http"], staff_token=_TOKEN)
    with pytest.raises(ViewerApiError) as ei:
        staff.merge_players(env["venue"].player_id, env["venue"].player_id)
    assert (ei.value.code, ei.value.status_code) == ("invalid_merge", 400)


def test_viewer_read_spans_merge(tmp_path: Path):
    env = _build(tmp_path)
    staff = ViewerApiClient(client=env["http"], staff_token=_TOKEN)
    venue, cloud, sid = env["venue"], env["cloud"], env["session"].session_id

    player = ViewerApiClient(client=env["http"])
    # merge 前: survivor から見えるのは自分が着席した hand（1）のみ。
    before = player.list_player_hands(venue.player_id, sid)
    assert {h["hand_id"] for h in before} == {1}

    staff.merge_players(venue.player_id, cloud.player_id)

    # merge 後: survivor から見ると absorbed の hand（2）も拾える。
    after = player.list_player_hands(venue.player_id, sid)
    assert {h["hand_id"] for h in after} == {1, 2}
    # ledger summary も両 ID の buy_in を合算（10000 + 5000）。
    led = player.get_player_session_ledger(venue.player_id, sid)
    assert led["summary"]["cash_in_total"] == 15000
    # session 一覧も hands_played=2 で 1 件。
    sess = player.list_player_sessions(venue.player_id)
    assert len(sess) == 1 and sess[0]["hands_played"] == 2


def test_login_returns_survivor_principal(tmp_path: Path):
    env = _build(tmp_path, player_auth="required")
    venue, cloud = env["venue"], env["cloud"]
    staff = ViewerApiClient(client=env["http"], staff_token=_TOKEN)
    # absorbed(cloud) に PIN を設定 → merge → cloud の PIN でログインしても survivor principal。
    staff.set_pin(cloud.player_id, "1234")
    staff.merge_players(venue.player_id, cloud.player_id)
    c = ViewerApiClient(client=env["http"])
    body = c.login(cloud.player_id, "1234")
    assert body["player_id"] == venue.player_id  # survivor に解決
    # その token で survivor として注文できる。
    sid = env["session"].session_id
    assert c.create_order_request(venue.player_id, sid, "beer", 1)["status"] == "pending"
