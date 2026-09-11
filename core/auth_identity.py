"""core/auth_identity.py

ADR-0031 (L2): 外部 IdP の `(provider, subject) → player_id` バインディングのドメインモデル。

`auth_identity` は player_id を**置き換えず**、外部 subject を内部 player_id に対応づける lookup
（ADR-0028 D1）。1 player に複数 provider（LINE / Google）を link 可能な**多対一**。player_id は
IdP sub から導出しない（ADR-0004）。node-local（read API / sync 非対象, ADR-0031 D2）。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AuthIdentity:
    """外部 IdP アカウントと内部 player_id の対応 1 件。

    属性:
        provider:           IdP 識別子（"line" / "google"、テストは "fake"）。
        subject:            IdP の sub（provider 内で一意）。
        player_id:          紐づく内部 player_id（アプリ内採番・不変）。
        linked_at:          link した時刻（ISO 8601）。
        display_name_seed:  初回 link 時の表示名候補（PII 最小化のためこれ以上は持たない）。
    """

    provider: str
    subject: str
    player_id: str
    linked_at: str
    display_name_seed: str | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.provider, self.subject)

    def to_dict(self) -> dict:
        d = {
            "provider": self.provider,
            "subject": self.subject,
            "player_id": self.player_id,
            "linked_at": self.linked_at,
        }
        if self.display_name_seed is not None:
            d["display_name_seed"] = self.display_name_seed
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "AuthIdentity":
        return cls(
            provider=d["provider"],
            subject=d["subject"],
            player_id=d["player_id"],
            linked_at=d.get("linked_at", ""),
            display_name_seed=d.get("display_name_seed"),
        )
