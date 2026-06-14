"""tests/test_viewer_api_auth.py

ADR-0027 (L1): viewer API の player PIN 認証（login / PIN 設定 / self-write の principal
ガード）の round-trip test。fastapi / httpx 未導入環境では skip。

`player_auth` の 3 モード（off / optional / required）と、PIN 設定の認可（staff / self-enroll /
現 PIN）、lockout、principal 不一致（forbidden）を検証する。
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

_TOKEN = "s3cr3t-staff-token"


def _write_menu(tmp_path: Path) -> Path:
    p = tmp_path / "menu.json"
    p.write_text(json.dumps({"items": [{"item_name": "ビール", "unit_amount": 700}]},
                            ensure_ascii=False), encoding="utf-8")
    return p


def _build(tmp_path: Path, *, player_auth: str, pin_self_enroll: bool = False,
           max_attempts: int = 5) -> dict:
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
    cred = PlayerCredentialRepository(path=tmp_path / "creds.json",
                                      iterations=1000, max_attempts=max_attempts)

    app = create_app(players, sessions, log_dir, ledger_repo=ledger,
                     order_repo=orders, menu=menu, orders_writable=True,
                     staff_token=_TOKEN, credential_repo=cred,
                     player_auth=player_auth, pin_self_enroll=pin_self_enroll)
    http = TestClient(app)
    return {
        "http": http, "alice": alice, "bob": bob, "session": s,
        "orders": orders, "cred": cred,
    }


def _client(env: dict, **kw) -> ViewerApiClient:
    return ViewerApiClient(client=env["http"], **kw)


# ――― 後方互換（off）―――

def test_off_mode_order_without_token(tmp_path: Path):
    env = _build(tmp_path, player_auth="off")
    c = _client(env)
    # name-pick: トークン無しで注文できる（従来動作）。
    req = c.create_order_request(env["alice"].player_id, env["session"].session_id, "ビール", 1)
    assert req["status"] == "pending"
    # login は無効。
    with pytest.raises(ViewerApiError) as ei:
        c.login(env["alice"].player_id, "1234")
    assert (ei.value.code, ei.value.status_code) == ("player_auth_disabled", 403)


# ――― required ―――

def test_required_login_and_authenticated_order(tmp_path: Path):
    env = _build(tmp_path, player_auth="required")
    alice, sid = env["alice"], env["session"].session_id
    staff = _client(env, staff_token=_TOKEN)
    staff.set_pin(alice.player_id, "1234")  # staff が初回設定

    c = _client(env)
    # トークン無しの注文は 401。
    with pytest.raises(ViewerApiError) as ei:
        c.create_order_request(alice.player_id, sid, "ビール", 1)
    assert (ei.value.code, ei.value.status_code) == ("unauthorized", 401)

    body = c.login(alice.player_id, "1234")
    assert body["player_id"] == alice.player_id and body["token"]
    # ログイン後はトークンが付き、注文できる。
    req = c.create_order_request(alice.player_id, sid, "ビール", 1)
    assert req["status"] == "pending"


def test_required_login_wrong_pin_and_unknown(tmp_path: Path):
    env = _build(tmp_path, player_auth="required")
    alice = env["alice"]
    _client(env, staff_token=_TOKEN).set_pin(alice.player_id, "1234")
    c = _client(env)
    with pytest.raises(ViewerApiError) as ei:
        c.login(alice.player_id, "9999")
    assert (ei.value.code, ei.value.status_code) == ("invalid_pin", 401)
    with pytest.raises(ViewerApiError) as ei:
        c.login("f" * 32, "1234")
    assert (ei.value.code, ei.value.status_code) == ("not_found", 404)


def test_required_principal_mismatch_is_forbidden(tmp_path: Path):
    env = _build(tmp_path, player_auth="required")
    alice, bob, sid = env["alice"], env["bob"], env["session"].session_id
    staff = _client(env, staff_token=_TOKEN)
    staff.set_pin(alice.player_id, "1234")
    c = _client(env)
    c.login(alice.player_id, "1234")
    # alice のトークンで bob として注文 → 403 forbidden。
    with pytest.raises(ViewerApiError) as ei:
        c.create_order_request(bob.player_id, sid, "ビール", 1)
    assert (ei.value.code, ei.value.status_code) == ("forbidden", 403)


# ――― optional ―――

def test_optional_unenrolled_player_uses_name_pick(tmp_path: Path):
    env = _build(tmp_path, player_auth="optional")
    alice, bob, sid = env["alice"], env["bob"], env["session"].session_id
    # alice だけ PIN 登録。
    _client(env, staff_token=_TOKEN).set_pin(alice.player_id, "1234")
    c = _client(env)
    # bob（未登録）はトークン無しで注文できる。
    assert c.create_order_request(bob.player_id, sid, "ビール", 1)["status"] == "pending"
    # alice（登録済）はトークン必須。
    with pytest.raises(ViewerApiError) as ei:
        c.create_order_request(alice.player_id, sid, "ビール", 1)
    assert (ei.value.code, ei.value.status_code) == ("unauthorized", 401)
    c.login(alice.player_id, "1234")
    assert c.create_order_request(alice.player_id, sid, "ビール", 1)["status"] == "pending"


# ――― PIN 設定の認可 ―――

def test_initial_set_requires_staff_when_self_enroll_off(tmp_path: Path):
    env = _build(tmp_path, player_auth="required", pin_self_enroll=False)
    alice = env["alice"]
    # staff token 無しの初回設定は 401。
    with pytest.raises(ViewerApiError) as ei:
        _client(env).set_pin(alice.player_id, "1234")
    assert (ei.value.code, ei.value.status_code) == ("unauthorized", 401)
    # staff なら OK。
    _client(env, staff_token=_TOKEN).set_pin(alice.player_id, "1234")
    assert env["cred"].has_pin(alice.player_id)


def test_self_enroll_allows_initial_set(tmp_path: Path):
    env = _build(tmp_path, player_auth="required", pin_self_enroll=True)
    alice = env["alice"]
    _client(env).set_pin(alice.player_id, "1234")  # staff token 無しでも初回設定可
    assert env["cred"].has_pin(alice.player_id)


def test_change_pin_requires_current_or_staff(tmp_path: Path):
    env = _build(tmp_path, player_auth="required", pin_self_enroll=True)
    alice = env["alice"]
    _client(env).set_pin(alice.player_id, "1234")
    # 現 PIN 無しの変更は 401。
    with pytest.raises(ViewerApiError) as ei:
        _client(env).set_pin(alice.player_id, "5678")
    assert (ei.value.code, ei.value.status_code) == ("unauthorized", 401)
    # 現 PIN 一致なら変更でき、新 PIN でログインできる。
    _client(env).set_pin(alice.player_id, "5678", current_pin="1234")
    assert _client(env).login(alice.player_id, "5678")["token"]
    # staff は現 PIN 無しで reset 可。
    _client(env, staff_token=_TOKEN).set_pin(alice.player_id, "0000")
    assert _client(env).login(alice.player_id, "0000")["token"]


def test_pin_too_short(tmp_path: Path):
    env = _build(tmp_path, player_auth="required", pin_self_enroll=True)
    with pytest.raises(ViewerApiError) as ei:
        _client(env).set_pin(env["alice"].player_id, "12")
    assert (ei.value.code, ei.value.status_code) == ("pin_too_short", 400)


def test_login_lockout(tmp_path: Path):
    env = _build(tmp_path, player_auth="required", max_attempts=2)
    alice = env["alice"]
    _client(env, staff_token=_TOKEN).set_pin(alice.player_id, "1234")
    c = _client(env)
    for _ in range(2):
        with pytest.raises(ViewerApiError) as ei:
            c.login(alice.player_id, "9999")
        assert ei.value.code == "invalid_pin"
    # 2 回失敗で lockout → 正しい PIN でも 429 pin_locked。
    with pytest.raises(ViewerApiError) as ei:
        c.login(alice.player_id, "1234")
    assert (ei.value.code, ei.value.status_code) == ("pin_locked", 429)
