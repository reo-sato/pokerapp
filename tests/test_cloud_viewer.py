"""tests/test_cloud_viewer.py

ADR-0067 D5 / 2c: 店外での閲覧（閲覧リンク → 30 日のログイン → 自分の記録だけを読む）。

- 店内の viewer API と**同じ中身**を返す（店舗 PC の read model と突き合わせる。聞き取った文は除く）。
- 自分の記録だけ: 他人の player・座っていなかったハンドは 404。別の店舗には届かない。
- 発行し直す・取り消すと、前のリンクも、それで入ったログインも使えなくなる。期限が切れたら入り直し。
- 店舗 PC から新しいデータが届けば写しを作り直す。
"""
from __future__ import annotations

import json
import re

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
pytest.importorskip("jsonschema")

from fastapi.testclient import TestClient  # noqa: E402

from api.read_models import (  # noqa: E402
    get_hand,
    get_player_session_ledger,
    list_player_hands,
    list_player_sessions,
)
from cloud.app import CloudSettings, create_cloud_app  # noqa: E402
from cloud.ingest import ingest_push  # noqa: E402
from cloud.storage import MemoryStorage  # noqa: E402
from cloud.tokens import hash_token, new_device_token  # noqa: E402
from core.cloud_payload import strip_hand  # noqa: E402
from core.hand_correction_repository import HandCorrectionRepository  # noqa: E402
from core.ledger_repository import LedgerRepository  # noqa: E402
from core.player_repository import PlayerRepository  # noqa: E402
from core.session_repository import SessionRepository  # noqa: E402

from tests.cloud_helpers import make_storage, push_payload, store_snapshot  # noqa: E402

LINKS = "/api/cloud/v1/viewer-links"
LOGIN = "/api/cloud/v1/auth/link"


class Wall:
    def __init__(self) -> None:
        self.t = 1_800_000_000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture(params=["memory", "postgres"])
def env(tmp_path, request):
    snap = store_snapshot(tmp_path / "shop")
    storage = make_storage(request.param)
    if request.param == "postgres":
        request.addfinalizer(lambda: (storage.drop_schema(), storage.close()))
    store = storage.create_store("テスト店")
    device = new_device_token()
    storage.add_device(store["store_id"], hash_token(device))
    ingest_push(storage, store["store_id"], push_payload(snap))
    wall = Wall()
    client = TestClient(create_cloud_app(storage, CloudSettings(
        token_secret="s" * 64, wall_clock=wall, public_url="https://cloud.example", view_root=tmp_path / "views")))
    return {"client": client, "storage": storage, "store_id": store["store_id"], "device": device,
            "ids": snap["ids"], "shop": tmp_path / "shop", "wall": wall, "tmp": tmp_path}


def _issue(env, player_id):
    return env["client"].post(LINKS, json={"player_id": player_id},
                              headers={"Authorization": f"Bearer {env['device']}"})


def _login(env, player_id):
    url = _issue(env, player_id).json()["url"]
    raw = url.split("#link=", 1)[1]
    body = env["client"].post(LOGIN, json={"link": raw}).json()
    return {"Authorization": f"Bearer {body['token']}"}, raw, body


def test_issue_a_link_and_log_in(env):
    res = _issue(env, env["ids"]["alice"])
    assert res.status_code == 201
    url = res.json()["url"]
    assert re.fullmatch(r"https://cloud\.example/#link=pkl_[A-Za-z0-9_-]{40,}", url)
    raw = url.split("#link=", 1)[1]
    body = env["client"].post(LOGIN, json={"link": raw}).json()
    assert body["player_id"] == env["ids"]["alice"] and body["display_name"] == "アリス"
    assert body["store_name"] == "テスト店" and body["expires_at"] == int(env["wall"].t) + 30 * 24 * 3600
    assert env["storage"].find_link(hash_token(raw))["last_used_at"] is not None


def test_links_need_the_device_token_and_a_known_player(env):
    assert env["client"].post(LINKS, json={"player_id": env["ids"]["alice"]}).status_code == 401
    res = _issue(env, "f" * 32)
    assert res.status_code == 404 and res.json()["code"] == "unknown_player"
    assert env["client"].post(LOGIN, json={"link": "pkl_nope"}).json()["code"] == "invalid_link"


def test_reads_match_the_store_pc(env):
    """店舗 PC の read model（同じデータ）と同じ中身を返す。"""
    headers, _, _ = _login(env, env["ids"]["alice"])
    client, alice, sid = env["client"], env["ids"]["alice"], env["ids"]["session_id"]
    shop = env["shop"]
    players = PlayerRepository(shop / "players.json")
    sessions = SessionRepository(shop / "sessions.json", player_repo=players)
    ledger = LedgerRepository(shop / "ledger.json", session_repo=sessions, player_repo=players)
    corrections = HandCorrectionRepository(shop / "hand_corrections.json")

    assert client.get(f"/api/players/{alice}", headers=headers).json() == players.get(alice).to_dict()
    assert client.get(f"/api/players/{alice}/sessions", headers=headers).json() == {
        "sessions": list_player_sessions(alice, sessions)}
    store_hands = [strip_hand(h) for h in list_player_hands(alice, sid, sessions, shop / "logs", corrections)]
    assert client.get(f"/api/players/{alice}/sessions/{sid}/hands", headers=headers).json() == {"hands": store_hands}
    assert client.get(f"/api/sessions/{sid}/hands/1", headers=headers).json() == strip_hand(
        get_hand(sid, 1, shop / "logs", corrections))
    cloud_ledger = client.get(f"/api/players/{alice}/sessions/{sid}/ledger", headers=headers).json()
    store_ledger = get_player_session_ledger(alice, sid, ledger)
    assert cloud_ledger == json.loads(json.dumps(store_ledger))
    assert cloud_ledger["summary"]["paid_amount"] == 1000 and cloud_ledger["summary"]["settled"] is True


def test_only_your_own_records(env):
    headers, _, _ = _login(env, env["ids"]["alice"])
    client, bob, sid = env["client"], env["ids"]["bob"], env["ids"]["session_id"]
    for path in (f"/api/players/{bob}", f"/api/players/{bob}/sessions", f"/api/players/{bob}/sessions/{sid}/hands",
                 f"/api/players/{bob}/sessions/{sid}/ledger"):
        res = client.get(path, headers=headers)
        assert res.status_code == 404 and res.json()["code"] == "not_found", path
    # ボブだけが座っていたハンド（別のセッション）は、セッションと番号を知っていても見えない
    other = {"session_id": "b" * 32, "started_at": "2026-10-11T10:00:00", "status": "open",
             "hands": {"1": {"started_at": "2026-10-11T10:01:00", "seats": [{"seat_no": 1, "player_id": bob}]}}}
    hand = {"hand_id": 1, "session_id": "b" * 32, "started_at": "a", "ended_at": "b", "players": [], "actions": []}
    env["storage"].apply(env["store_id"], [("sessions", other["session_id"], other),
                                           ("hands", f"{'b' * 32}:1", hand)], seq=99)
    res = client.get(f"/api/sessions/{'b' * 32}/hands/1", headers=headers)
    assert res.status_code == 404
    assert client.get(f"/api/sessions/{sid}/hands/99", headers=headers).status_code == 404
    for path in ("/api/players", f"/api/players/{env['ids']['alice']}/sessions/{sid}/order-requests", "/api/menu"):
        assert client.get(path, headers=headers).status_code in (404, 405)   # 名前の一覧・注文はクラウドに無い


def test_reads_need_a_login(env):
    alice = env["ids"]["alice"]
    for headers in ({}, {"Authorization": "Bearer junk"}, {"Authorization": f"Bearer {env['device']}"}):
        res = env["client"].get(f"/api/players/{alice}/sessions", headers=headers)
        assert res.status_code == 401


def test_reissue_and_revoke_end_old_links_and_logins(env):
    alice = env["ids"]["alice"]
    headers, raw, _ = _login(env, alice)
    path = f"/api/players/{alice}/sessions"
    assert env["client"].get(path, headers=headers).status_code == 200
    new_headers, new_raw, _ = _login(env, alice)          # 発行し直し（スマホをなくした）
    assert env["client"].post(LOGIN, json={"link": raw}).status_code == 401
    res = env["client"].get(path, headers=headers)
    assert res.status_code == 401 and res.json()["code"] == "link_revoked"
    assert env["client"].get(path, headers=new_headers).status_code == 200
    res = env["client"].post(f"{LINKS}/revoke", json={"player_id": alice},
                             headers={"Authorization": f"Bearer {env['device']}"})
    assert res.json() == {"revoked": 1}
    assert env["client"].get(path, headers=new_headers).status_code == 401
    assert env["client"].post(LOGIN, json={"link": new_raw}).status_code == 401


def test_logins_expire(env):
    headers, raw, _ = _login(env, env["ids"]["alice"])
    env["wall"].t += 30 * 24 * 3600 + 1
    path = f"/api/players/{env['ids']['alice']}/sessions"
    assert env["client"].get(path, headers=headers).status_code == 401
    again = env["client"].post(LOGIN, json={"link": raw}).json()        # 同じリンクで入り直せる
    assert env["client"].get(path, headers={"Authorization": f"Bearer {again['token']}"}).status_code == 200


def test_new_pushes_rebuild_the_view(env):
    headers, _, _ = _login(env, env["ids"]["alice"])
    alice = env["ids"]["alice"]
    assert env["client"].get(f"/api/players/{alice}", headers=headers).json()["display_name"] == "アリス"
    record = env["storage"].get_record(env["store_id"], "players", alice)
    renamed = {**record, "display_name": "アリス2", "updated_at": "2099-01-01T00:00:00"}
    env["storage"].apply(env["store_id"], [("players", alice, renamed)], seq=10**6)
    assert env["client"].get(f"/api/players/{alice}", headers=headers).json()["display_name"] == "アリス2"


def test_link_login_is_rate_limited(tmp_path):
    storage = MemoryStorage()
    client = TestClient(create_cloud_app(storage, CloudSettings(link_rate=(2, 60.0), view_root=tmp_path)))
    codes = [client.post(LOGIN, json={"link": "pkl_x"}).status_code for _ in range(3)]
    assert codes == [401, 401, 429]


def test_another_store_cannot_be_reached(env):
    """別の店舗の端末トークンでは、この店舗の人の閲覧リンクは作れない。"""
    other = env["storage"].create_store("別の店")
    token = new_device_token()
    env["storage"].add_device(other["store_id"], hash_token(token))
    res = env["client"].post(LINKS, json={"player_id": env["ids"]["alice"]},
                             headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 404 and res.json()["code"] == "unknown_player"


def test_the_player_page_is_served_when_built(tmp_path):
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<html>player</html>", encoding="utf-8")
    client = TestClient(create_cloud_app(MemoryStorage(), CloudSettings(player_web_dir=web, view_root=tmp_path)))
    res = client.get("/")
    assert res.status_code == 200 and "player" in res.text and res.headers["cache-control"] == "no-cache"
    assert client.get("/api/unknown").json()["code"] == "not_found"


def test_hole_card_visibility_in_the_cloud(env):
    """公開範囲（ADR-0067 D6）: 店舗の設定 own_and_shown → 自分の手札だけ。セッションの設定 all が優先。"""
    headers, _, _ = _login(env, env["ids"]["alice"])
    client, alice, sid = env["client"], env["ids"]["alice"], env["ids"]["session_id"]
    shop_hand = client.get(f"/api/sessions/{sid}/hands/1", headers=headers).json()
    seats = {p["seat"] for p in shop_hand["players"]}
    # 記録に手札を入れて送り直す（フィクスチャのハンドは手札が無い）
    hand = env["storage"].get_record(env["store_id"], "hands", f"{sid}:1")
    for p, cards in zip(hand["players"], (["Ah", "Ad"], ["Kh", "Kd"], ["Qh", "Qd"])):
        p["hole_cards"] = cards
    session = env["storage"].get_record(env["store_id"], "sessions", sid)
    env["storage"].apply(env["store_id"], [("hands", f"{sid}:1", hand),
                                           ("store_settings", "settings", {"hole_cards": "own_and_shown"})],
                         seq=10**7)
    alice_seat = next(int(s["seat_no"]) for s in session["hands"]["1"]["seats"] if s["player_id"] == alice)
    got = client.get(f"/api/sessions/{sid}/hands/1", headers=headers).json()
    visible = {p["seat"] for p in got["players"] if p["hole_cards"]}
    assert visible == {alice_seat} and seats
    listed = client.get(f"/api/players/{alice}/sessions/{sid}/hands", headers=headers).json()["hands"][0]
    assert {p["seat"] for p in listed["players"] if p["hole_cards"]} == {alice_seat}
    env["storage"].apply(env["store_id"], [("sessions", sid, {**session, "hole_card_visibility": "all"})],
                         seq=10**7 + 1)
    got = client.get(f"/api/sessions/{sid}/hands/1", headers=headers).json()
    assert all(p["hole_cards"] for p in got["players"])
