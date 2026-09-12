"""core/auth_token.py

ADR-0027 (L1): player principal の stateless 署名トークン。

PIN 検証成功後にサーバが発行し、player の self-write 認可に使う短命トークン。サーバ側
セッションストアを持たず、HMAC-SHA256 署名のみで検証する（staff token と同じ軽量路線）。
L2（ADR-0028, 外部 IdP）も OIDC 検証後に同じ形式のトークンを発行するため、principal 解決は
トークン形式だけに依存し、認証方式に非依存になる（L0→L1→L2 が additive な合流点）。

形式: ``v1.<player_id>.<exp_unix>.<hmac_hex>``
  hmac = HMAC-SHA256(secret, "v1.<player_id>.<exp_unix>") の hexdigest

player_id は UUID hex（ドットを含まない）なので、トークンは常にちょうど 4 セグメントになる。
"""
from __future__ import annotations

import hmac
import time
from hashlib import sha256

_PREFIX = "v1"


def issue_player_token(
    player_id: str, secret: str, ttl_sec: int, *, now: float | None = None
) -> tuple[str, int]:
    """player_id に対する署名トークンと失効 unix 時刻を返す。

    secret が空なら発行しない（呼び出し側が ephemeral secret を用意する, ADR-0027 D3）。
    """
    if not secret:
        raise ValueError("player token secret is empty")
    if not player_id:
        raise ValueError("player_id is empty")
    issued = int(now if now is not None else time.time())
    exp = issued + int(ttl_sec)
    body = f"{_PREFIX}.{player_id}.{exp}"
    return f"{body}.{_sign(body, secret)}", exp


def verify_player_token(
    token: str, secret: str, *, now: float | None = None
) -> str | None:
    """有効なら player_id を返す。形式不正 / 署名不一致 / 期限切れ / secret 空なら None。"""
    if not token or not secret:
        return None
    parts = token.split(".")
    if len(parts) != 4:
        return None
    prefix, player_id, exp_s, sig = parts
    if prefix != _PREFIX or not player_id:
        return None
    try:
        exp = int(exp_s)
    except ValueError:
        return None
    expected = _sign(f"{prefix}.{player_id}.{exp}", secret)
    if not hmac.compare_digest(sig, expected):
        return None
    current = now if now is not None else time.time()
    if current >= exp:
        return None
    return player_id


def _sign(body: str, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), body.encode("utf-8"), sha256).hexdigest()
