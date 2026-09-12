"""tests/test_viewer_api_oidc.py

ADR-0031 (L2): POST /api/auth/{provider}/exchange の E2E（fake provider 注入）。
fastapi/httpx 未導入は skip。
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from api.client import ViewerApiClient, ViewerApiError  # noqa: E402
from api.server import create_app  # noqa: E402
from core.auth_identity_repository import AuthIdentityRepository  # noqa: E402
from core.oidc import FakeOidcProvider  # noqa: E402
from core.player_repository import PlayerRepository  # noqa: E402
from core.session_repository import SessionRepository  # noqa: E402


def _build(tmp_path: Path, *, with_provider: bool = True) -> dict:
    players = PlayerRepository(path=tmp_path / "players.json")
    sessions = SessionRepository(path=tmp_path / "sessions.json", player_repo=players)
    identities = AuthIdentityRepository(path=tmp_path / "auth_identity.json")
    providers = None
    fake = FakeOidcProvider("line")
    if with_provider:
        fake.register("good-code", "U1", display_name_seed="Bob")
        providers = {"line": fake}
    log_dir = tmp_path / "logs"
    log_dir.mkdir(exist_ok=True)
    app = create_app(players, sessions, log_dir, player_auth="required", orders_writable=True,
                     oidc_providers=providers, identity_repo=identities)
    return {"http": TestClient(app), "players": players, "identities": identities}


def test_oidc_exchange_creates_and_logs_in(tmp_path: Path):
    env = _build(tmp_path)
    c = ViewerApiClient(client=env["http"])
    body = c.oidc_exchange("line", "good-code")
    assert body["token"] and body["player_id"]
    # player と auth_identity が作られている。
    assert env["players"].get(body["player_id"]).display_name == "Bob"
    assert env["identities"].get("line", "U1").player_id == body["player_id"]
    # 再交換は同一 player に解決。
    again = ViewerApiClient(client=env["http"]).oidc_exchange("line", "good-code")
    assert again["player_id"] == body["player_id"]


def test_oidc_exchange_invalid_code(tmp_path: Path):
    env = _build(tmp_path)
    with pytest.raises(ViewerApiError) as ei:
        ViewerApiClient(client=env["http"]).oidc_exchange("line", "bad-code")
    assert (ei.value.code, ei.value.status_code) == ("invalid_idp_code", 401)


def test_oidc_unknown_provider_when_not_configured(tmp_path: Path):
    env = _build(tmp_path, with_provider=False)
    with pytest.raises(ViewerApiError) as ei:
        ViewerApiClient(client=env["http"]).oidc_exchange("line", "good-code")
    assert (ei.value.code, ei.value.status_code) == ("unknown_provider", 404)


def test_oidc_token_works_as_player_principal(tmp_path: Path):
    # 交換で得た token が L1 と同じ principal として self-write に使える（合流点, ADR-0031 D3）。
    env = _build(tmp_path)
    players = env["players"]
    c = ViewerApiClient(client=env["http"])
    c.oidc_exchange("line", "good-code")
    # principal の player_id と一致しない相手の操作は forbidden（merge していないので別人）。
    other = players.create_player("Someone")
    with pytest.raises(ViewerApiError) as ei:
        # principal チェック（_require_player）が player_repo.get より先に効く（forbidden）。
        c.create_order_request(other.player_id, "dummy-session", "beer", 1)
    assert ei.value.code == "forbidden"
