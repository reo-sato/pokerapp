"""tests/test_auth_token.py

ADR-0027 (L1): player principal の stateless 署名トークン（`core/auth_token.py`）の単体テスト。
fastapi 非依存（純関数）。clock は now 注入で決定的（ADR-0011 と同方針）。
"""
from __future__ import annotations

import pytest

from core.auth_token import issue_player_token, verify_player_token

_SECRET = "test-secret-key"


def test_issue_and_verify_roundtrip():
    token, exp = issue_player_token("abc123", _SECRET, ttl_sec=100, now=1000)
    assert exp == 1100
    assert verify_player_token(token, _SECRET, now=1050) == "abc123"


def test_expired_token_is_none():
    token, _ = issue_player_token("abc123", _SECRET, ttl_sec=100, now=1000)
    # exp ちょうど / 超過は無効。
    assert verify_player_token(token, _SECRET, now=1100) is None
    assert verify_player_token(token, _SECRET, now=2000) is None


def test_wrong_secret_is_none():
    token, _ = issue_player_token("abc123", _SECRET, ttl_sec=100, now=1000)
    assert verify_player_token(token, "other-secret", now=1050) is None


def test_tampered_token_is_none():
    token, _ = issue_player_token("abc123", _SECRET, ttl_sec=100, now=1000)
    # player_id を別人に書き換えても署名は一致しない。
    parts = token.split(".")
    forged = ".".join([parts[0], "victim", parts[2], parts[3]])
    assert verify_player_token(forged, _SECRET, now=1050) is None


@pytest.mark.parametrize("bad", ["", "v1.abc.x", "garbage", "v2.abc.1100.deadbeef"])
def test_malformed_token_is_none(bad: str):
    assert verify_player_token(bad, _SECRET, now=1050) is None


def test_empty_secret_rejected():
    with pytest.raises(ValueError):
        issue_player_token("abc123", "", ttl_sec=100)
    assert verify_player_token("v1.abc.1100.sig", "", now=0) is None
