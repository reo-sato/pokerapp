"""tests/test_cloud_app.py

ADR-0067 D3/D4/D9: クラウドのアプリ（許可した受け口だけ）。

- 受け口の一覧を固定する（スタッフ・会計を書く受け口・同期の snapshot/merge・説明のページが無い）。
- 端末トークンで店舗が決まり、別の店舗のデータには届かない。取り消したトークンは使えない。
- 管理の受け口は管理トークン（32 文字以上）が無ければ存在しない。回数制限がある。
- トークンはログに出ない。
"""
from __future__ import annotations

import logging

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
pytest.importorskip("jsonschema")

from fastapi.testclient import TestClient  # noqa: E402

from cloud.app import CloudSettings, create_cloud_app  # noqa: E402
from cloud.storage import MemoryStorage  # noqa: E402
from cloud.tokens import hash_token, new_device_token  # noqa: E402

from tests.cloud_helpers import push_payload, store_snapshot  # noqa: E402

ADMIN = "x" * 40
PUSH = "/api/cloud/v1/sync/push"
STATUS = "/api/cloud/v1/sync/status"
ADMIN_STORES = "/api/cloud/v1/admin/stores"


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _app(storage=None, **kw):
    storage = storage or MemoryStorage()
    clock = Clock()
    settings = CloudSettings(admin_token=kw.pop("admin_token", ADMIN), clock=clock, **kw)
    return TestClient(create_cloud_app(storage, settings)), storage, clock


def _device(storage, name="店"):
    store = storage.create_store(name)
    token = new_device_token()
    device = storage.add_device(store["store_id"], hash_token(token))
    return store["store_id"], token, device


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_only_the_allowed_routes_exist():
    client, _, _ = _app()
    paths = {(sorted(r.methods)[0] if getattr(r, "methods", None) else "", r.path) for r in client.app.routes}
    assert paths == {
        ("GET", "/api/health"),
        ("POST", "/api/cloud/v1/sync/push"),
        ("GET", "/api/cloud/v1/sync/status"),
        ("GET", "/api/cloud/v1/admin/stores"),
        ("POST", "/api/cloud/v1/admin/stores"),
        ("POST", "/api/cloud/v1/admin/stores/{store_id}/devices"),
        ("POST", "/api/cloud/v1/admin/stores/{store_id}/devices/revoke"),
        # 店外での閲覧（2c）: リンクの発行・取り消し（店舗 PC）、リンクでのログイン、自分の記録の読み取り
        ("POST", "/api/cloud/v1/viewer-links"),
        ("POST", "/api/cloud/v1/viewer-links/revoke"),
        ("POST", "/api/cloud/v1/auth/link"),
        ("GET", "/api/players/{player_id}"),
        ("GET", "/api/players/{player_id}/sessions"),
        ("GET", "/api/players/{player_id}/sessions/{session_id}/hands"),
        ("GET", "/api/players/{player_id}/sessions/{session_id}/ledger"),
        ("GET", "/api/sessions/{session_id}/hands/{hand_id}"),
    }
    assert not any(p.startswith("/api/staff") for _, p in paths)
    for path in ("/docs", "/openapi.json", "/redoc", "/api/staff/sync/snapshot", "/api/players"):
        res = client.get(path)
        assert res.status_code == 404 and res.json()["code"] == "not_found"


def test_health_and_security_headers():
    client, _, _ = _app()
    res = client.get("/api/health")
    assert res.json() == {"status": "ok", "database": "ok"}
    assert res.headers["x-content-type-options"] == "nosniff"
    assert res.headers["referrer-policy"] == "no-referrer"
    assert res.headers["cache-control"] == "no-store"


def test_health_reports_a_broken_database():
    class Down(MemoryStorage):
        def ping(self) -> bool:
            return False

    client, _, _ = _app(Down())
    res = client.get("/api/health")
    assert res.status_code == 503 and res.json()["database"] == "error"


def test_push_with_a_device_token(tmp_path):
    client, storage, _ = _app()
    store_id, token, _ = _device(storage)
    res = client.post(PUSH, json=push_payload(store_snapshot(tmp_path)), headers=_auth(token))
    assert res.status_code == 200, res.text
    assert res.json()["results"]["hands"]["added"] == 2
    status = client.get(STATUS, headers=_auth(token)).json()
    assert status["store_id"] == store_id and status["counts"]["players"] == 2 and status["max_seq"] == 1
    assert storage.find_device(hash_token(token))["last_used_at"] is not None


def test_a_store_only_reaches_its_own_data(tmp_path):
    client, storage, _ = _app()
    _, token_a, _ = _device(storage, "A 店")
    store_b, token_b, _ = _device(storage, "B 店")
    client.post(PUSH, json=push_payload(store_snapshot(tmp_path)), headers=_auth(token_a))
    assert client.get(STATUS, headers=_auth(token_b)).json()["counts"] == {}
    assert storage.counts(store_b) == {}


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic abc"}])
def test_push_needs_a_valid_token(headers):
    client, storage, _ = _app()
    _device(storage)
    res = client.post(PUSH, json={"protocol": 1, "seq": 1}, headers=headers)
    assert res.status_code == 401 and res.json()["code"] == "unauthorized"


def test_revoked_tokens_and_inactive_stores():
    client, storage, _ = _app()
    store_id, token, _ = _device(storage)
    storage.revoke_devices(store_id)
    assert client.post(PUSH, json={"protocol": 1, "seq": 1}, headers=_auth(token)).status_code == 401
    store2, token2, _ = _device(storage, "休止中の店")
    storage._stores[store2]["status"] = "suspended"
    res = client.get(STATUS, headers=_auth(token2))
    assert res.status_code == 403 and res.json()["code"] == "store_inactive"


def test_failed_device_auth_is_rate_limited():
    client, _, clock = _app(auth_fail_rate=(3, 60.0))
    codes = [client.get(STATUS, headers=_auth("wrong")).status_code for _ in range(4)]
    assert codes == [401, 401, 401, 429]
    clock.t = 61.0
    assert client.get(STATUS, headers=_auth("wrong")).status_code == 401


def test_push_body_limits_and_errors():
    client, storage, _ = _app(max_body_bytes=200)
    _, token, _ = _device(storage)
    big = {"protocol": 1, "seq": 1, "records": {"hands": [{"x": "a" * 300}]}}
    res = client.post(PUSH, json=big, headers=_auth(token))
    assert res.status_code == 413 and res.json()["code"] == "payload_too_large"
    res = client.post(PUSH, content=b"{not json", headers={**_auth(token), "Content-Type": "application/json"})
    assert res.status_code == 400 and res.json()["code"] == "invalid_payload"
    res = client.post(PUSH, json={"protocol": 99, "seq": 1}, headers=_auth(token))
    assert res.status_code == 409 and res.json()["code"] == "unsupported_protocol"


class TestAdmin:
    def test_disabled_without_a_long_enough_token(self):
        for admin_token in (None, "short"):
            client, _, _ = _app(admin_token=admin_token)
            res = client.post(ADMIN_STORES, json={"name": "店"}, headers=_auth(admin_token or "x"))
            assert res.status_code == 404

    def test_create_a_store_and_use_its_token(self, tmp_path):
        client, storage, _ = _app()
        assert client.post(ADMIN_STORES, json={"name": "店"}, headers=_auth("y" * 40)).status_code == 401
        res = client.post(ADMIN_STORES, json={"name": "  ポーカー店  "}, headers=_auth(ADMIN))
        assert res.status_code == 201
        body = res.json()
        assert body["store"]["name"] == "ポーカー店" and body["device_token"].startswith("pkd_")
        assert "token_hash" not in body["device"]
        assert client.get(STATUS, headers=_auth(body["device_token"])).status_code == 200
        listed = client.get(ADMIN_STORES, headers=_auth(ADMIN)).json()["stores"]
        assert [s["store_id"] for s in listed] == [body["store"]["store_id"]]

    def test_reissue_and_revoke(self):
        client, _, _ = _app()
        created = client.post(ADMIN_STORES, json={"name": "店"}, headers=_auth(ADMIN)).json()
        store_id, old = created["store"]["store_id"], created["device_token"]
        res = client.post(f"{ADMIN_STORES}/{store_id}/devices", json={"revoke_others": True}, headers=_auth(ADMIN))
        assert res.status_code == 201 and res.json()["revoked"] == 1
        new = res.json()["device_token"]
        assert client.get(STATUS, headers=_auth(old)).status_code == 401
        assert client.get(STATUS, headers=_auth(new)).status_code == 200
        res = client.post(f"{ADMIN_STORES}/{store_id}/devices/revoke", headers=_auth(ADMIN))
        assert res.json() == {"revoked": 1}
        assert client.get(STATUS, headers=_auth(new)).status_code == 401
        assert client.post(f"{ADMIN_STORES}/missing/devices", json={}, headers=_auth(ADMIN)).status_code == 404
        assert client.post(f"{ADMIN_STORES}/missing/devices/revoke", headers=_auth(ADMIN)).status_code == 404

    def test_bad_names(self):
        client, _, _ = _app()
        for body in ({}, {"name": "  "}, {"name": "x" * 101}, ["店"]):
            res = client.post(ADMIN_STORES, json=body, headers=_auth(ADMIN))
            assert res.status_code == 400

    def test_rate_limited(self):
        client, _, clock = _app(admin_rate=(2, 60.0))
        codes = [client.get(ADMIN_STORES, headers=_auth(ADMIN)).status_code for _ in range(3)]
        assert codes == [200, 200, 429]
        clock.t = 60.0
        assert client.get(ADMIN_STORES, headers=_auth(ADMIN)).status_code == 200

    def test_rate_limit_uses_the_last_forwarded_address(self):
        # 前の値はお客さんが付けられるので、Cloud Run が最後に足した接続元で数える
        client, _, _ = _app(admin_rate=(1, 60.0))
        assert client.get(ADMIN_STORES, headers={**_auth(ADMIN), "X-Forwarded-For": "1.1.1.1, 9.9.9.9"}).status_code == 200
        res = client.get(ADMIN_STORES, headers={**_auth(ADMIN), "X-Forwarded-For": "2.2.2.2, 9.9.9.9"})
        assert res.status_code == 429


def test_tokens_never_reach_the_logs(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    client, _, _ = _app()
    created = client.post(ADMIN_STORES, json={"name": "店"}, headers=_auth(ADMIN)).json()
    token = created["device_token"]
    client.post(PUSH, json=push_payload(store_snapshot(tmp_path)), headers=_auth(token))
    client.get(STATUS, headers=_auth("wrong-token"))
    text = caplog.text
    assert token not in text and ADMIN not in text and "wrong-token" not in text
    assert "push store=" in text      # 何が起きたかは残る
