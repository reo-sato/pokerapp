"""tests/test_oidc.py

ADR-0031 (L2): provider 抽象 + claim→principal 解決（実 IdP なし、FakeOidcProvider）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core.auth_identity_repository import AuthIdentityRepository
from core.oidc import (
    FakeOidcProvider,
    OidcError,
    VerifiedClaim,
    resolve_player_for_claim,
)
from core.player_repository import PlayerRepository


def _world(tmp_path: Path):
    players = PlayerRepository(path=tmp_path / "players.json")
    identities = AuthIdentityRepository(path=tmp_path / "auth_identity.json")
    return players, identities


def test_fake_provider_verifies_registered_code(tmp_path: Path):
    p = FakeOidcProvider("line")
    p.register("code-abc", "U1", display_name_seed="Bob")
    claim = p.verify_code("code-abc")
    assert claim == VerifiedClaim("line", "U1", "Bob")


def test_fake_provider_rejects_unknown_code(tmp_path: Path):
    p = FakeOidcProvider("line")
    with pytest.raises(OidcError):
        p.verify_code("nope")


def test_first_login_creates_player_and_links(tmp_path: Path):
    players, identities = _world(tmp_path)
    claim = VerifiedClaim("line", "U1", "Bob")
    pid = resolve_player_for_claim(claim, identities, players)
    # 新規 player が作られ、display_name は seed 由来。
    assert players.get(pid).display_name == "Bob"
    # auth_identity が張られている。
    assert identities.get("line", "U1").player_id == pid


def test_relogin_resolves_same_player(tmp_path: Path):
    players, identities = _world(tmp_path)
    claim = VerifiedClaim("line", "U1", "Bob")
    pid1 = resolve_player_for_claim(claim, identities, players)
    pid2 = resolve_player_for_claim(claim, identities, players)
    assert pid1 == pid2
    assert len(players.list_players()) == 1  # 2 回目は新規作成しない


def test_multi_provider_links_to_distinct_players_until_merged(tmp_path: Path):
    players, identities = _world(tmp_path)
    line_pid = resolve_player_for_claim(VerifiedClaim("line", "U1", "Bob"), identities, players)
    google_pid = resolve_player_for_claim(VerifiedClaim("google", "G1", "Bob"), identities, players)
    # 別 provider の初回ログインは別 player（同一人物の統合は staff merge で行う, ADR-0030）。
    assert line_pid != google_pid
    # display_name 重複は一意化される。
    assert players.get(line_pid).display_name != players.get(google_pid).display_name


def test_resolve_follows_merge_canonical(tmp_path: Path):
    players, identities = _world(tmp_path)
    cloud_pid = resolve_player_for_claim(VerifiedClaim("line", "U1", "Bob"), identities, players)
    venue = players.create_player("Bob (venue)")
    players.merge_players(venue.player_id, cloud_pid)  # cloud を venue に統合
    # 同じ IdP で再ログイン → survivor(venue) に解決される（ADR-0031 D3 + ADR-0030）。
    again = resolve_player_for_claim(VerifiedClaim("line", "U1", "Bob"), identities, players)
    assert again == venue.player_id


def test_display_name_seed_collision_is_uniquified(tmp_path: Path):
    players, identities = _world(tmp_path)
    players.create_player("Bob")  # seed と衝突する既存 player
    pid = resolve_player_for_claim(VerifiedClaim("line", "U1", "Bob"), identities, players)
    assert players.get(pid).display_name != "Bob"  # サフィックスで一意化
