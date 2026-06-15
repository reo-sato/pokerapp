"""core/auth_identity_repository.py

ADR-0031 (L2): `auth_identity`（外部 IdP ↔ player_id バインディング）の node-local リポジトリ。

永続は `auth_identity.json`（`.gitignore`, アトミックリネーム）。`(provider, subject)` で一意。
PIN credential（ADR-0027）と同じく **read API / sync snapshot 非対象**（node-local の認証メタデータ。
player_id だけが会場↔cloud を橋渡しする, ADR-0029 D1 / ADR-0031 D2）。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from core.atomic_io import atomic_write_json
from core.auth_identity import AuthIdentity

logger = logging.getLogger(__name__)

_DEFAULT_IDENTITY_DB = Path(__file__).parent.parent / "auth_identity.json"


class AuthIdentityError(Exception):
    """auth_identity 操作の基底例外。"""


class AuthIdentityConflictError(AuthIdentityError):
    """同一 (provider, subject) が別 player_id に既に link 済み（code=identity_conflict）。"""


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class AuthIdentityRepository:
    """`(provider, subject) → player_id` の node-local ストア（多対一）。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path is not None else _DEFAULT_IDENTITY_DB
        self._by_key: dict[tuple[str, str], AuthIdentity] = {}
        self._load()

    @property
    def path(self) -> Path:
        return self._path

    # ――― 永続化 ―――

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            with self._path.open(encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Could not load auth_identity DB (%s), starting empty.", e)
            return
        for raw in data.get("identities", []):
            try:
                identity = AuthIdentity.from_dict(raw)
            except (KeyError, TypeError):
                logger.warning("Skipping malformed auth_identity record: %r", raw)
                continue
            self._by_key[identity.key] = identity

    def _flush(self) -> None:
        try:
            atomic_write_json(self._path, {"identities": [i.to_dict() for i in self._by_key.values()]})
        except OSError:
            logger.exception("Failed to write auth_identity DB: %s", self._path)

    def reload(self) -> None:
        self._by_key.clear()
        self._load()

    # ――― API ―――

    def get(self, provider: str, subject: str) -> AuthIdentity | None:
        return self._by_key.get((provider, subject))

    def link(
        self, provider: str, subject: str, player_id: str,
        display_name_seed: str | None = None,
    ) -> AuthIdentity:
        """(provider, subject) を player_id に link する。

        既存が同一 player_id なら冪等（display_name_seed は初回値を保持）。別 player_id へ link
        しようとしたら AuthIdentityConflictError。
        """
        existing = self._by_key.get((provider, subject))
        if existing is not None:
            if existing.player_id != player_id:
                raise AuthIdentityConflictError(
                    f"({provider}, {subject}) は既に別 player に link 済みです。"
                )
            return existing
        identity = AuthIdentity(
            provider=provider, subject=subject, player_id=player_id,
            linked_at=_now_iso(), display_name_seed=display_name_seed,
        )
        self._by_key[identity.key] = identity
        self._flush()
        logger.info("Linked auth identity (%s, %s) -> %s", provider, subject, player_id)
        return identity

    def list_for_player(self, player_id: str) -> list[AuthIdentity]:
        """player に link された全 identity（複数 provider）を provider 順で返す。"""
        return sorted(
            (i for i in self._by_key.values() if i.player_id == player_id),
            key=lambda i: (i.provider, i.subject),
        )

    def unlink(self, provider: str, subject: str) -> None:
        """link を解除する（退会 / link 解除, ADR-0029 D5）。存在しなければ no-op。"""
        if self._by_key.pop((provider, subject), None) is not None:
            self._flush()
            logger.info("Unlinked auth identity (%s, %s)", provider, subject)
