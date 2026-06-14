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
        merged_into:  merge で吸収された場合の survivor player_id（ADR-0030）。未 merge は None。
        merged_at:    merge された時刻（ISO 8601）。merged_into とセットで付く。
    """

    player_id: str
    display_name: str
    created_at: str
    merged_into: str | None = None
    merged_at: str | None = None

    @property
    def is_merged(self) -> bool:
        """この player が merge で吸収された tombstone か（ADR-0030）。"""
        return self.merged_into is not None

    def to_dict(self) -> dict:
        d = {
            "player_id": self.player_id,
            "display_name": self.display_name,
            "created_at": self.created_at,
        }
        # 未 merge の player は merge フィールドを持たない（schema 1.1 optional, 形を従来どおりに保つ）。
        if self.merged_into is not None:
            d["merged_into"] = self.merged_into
            d["merged_at"] = self.merged_at or ""
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Player":
        return cls(
            player_id=d["player_id"],
            display_name=d["display_name"],
            created_at=d.get("created_at", ""),
            merged_into=d.get("merged_into"),
            merged_at=d.get("merged_at"),
        )
