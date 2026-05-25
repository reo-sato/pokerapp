"""core/player.py

Phase S1: player registry の最小ドメインモデル。

player は hand logger とは独立した存在で、session / ledger / settlement から
`player_id` で参照される（CLAUDE.md § Future Scope / Cross-app boundary 参照）。
現時点の属性は `display_name` のみ（+ 内部 ID と作成時刻）。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Player:
    """アプリ内で新規作成される player。

    属性:
        player_id:    永続・安定な内部 ID（UUID hex）。発行後は変化しない。
        display_name: 表示名。前後空白を除去した文字列を保持する。
        created_at:   作成時刻（ISO 8601）。
    """

    player_id: str
    display_name: str
    created_at: str

    def to_dict(self) -> dict:
        return {
            "player_id": self.player_id,
            "display_name": self.display_name,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Player":
        return cls(
            player_id=d["player_id"],
            display_name=d["display_name"],
            created_at=d.get("created_at", ""),
        )
