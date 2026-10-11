"""cloud/tokens.py — 端末トークン・管理トークン（ADR-0067 D3）。

- 端末トークンは 32 バイトの乱数。クラウドは sha256 のハッシュだけを保存する。
- 比べるときは時間が一定の比較（`hmac.compare_digest`）。
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Optional

DEVICE_TOKEN_PREFIX = "pkd_"     # 見て何のトークンか分かるように（ログに出ていないか探すときにも使える）
LINK_TOKEN_PREFIX = "pkl_"       # 閲覧リンクの乱数（URL の # の後ろ, ADR-0067 D5）
MIN_ADMIN_TOKEN_LEN = 32


def new_device_token() -> str:
    return DEVICE_TOKEN_PREFIX + secrets.token_urlsafe(32)


def new_link_token() -> str:
    return LINK_TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def bearer(header: Optional[str]) -> Optional[str]:
    """`Authorization: Bearer <token>` のトークン（無ければ None）。"""
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


def admin_token_ok(given: Optional[str], expected: Optional[str]) -> bool:
    if not expected or len(expected) < MIN_ADMIN_TOKEN_LEN or not given:
        return False
    return hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))
