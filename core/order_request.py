"""core/order_request.py

Phase M5 (ADR-0018): player からの注文リクエスト = OrderRequest。

契約は docs/contracts/viewer-api.md § 注文リクエスト / schemas/order_request.schema.json (0.x)。
ledger には書かれず、スタッフ確定 (confirm) で ledger_entry がリンクされる。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class OrderRequest:
    """注文リクエスト 1 件（pending → confirmed | rejected）。"""

    request_id: str        # UUID4 hex (32 文字)。アプリ内採番・不変
    session_id: str
    player_id: str         # registry の UUID4 hex
    item_name: str
    quantity: int          # 1..99
    status: str            # "pending" | "confirmed" | "rejected"
    requested_at: str      # ISO 8601
    note: str | None = None
    resolved_at: str | None = None       # confirmed / rejected 時のみ
    ledger_entry_id: str | None = None   # confirmed 時のみ

    def to_dict(self) -> dict:
        data: dict = {
            "request_id": self.request_id,
            "session_id": self.session_id,
            "player_id": self.player_id,
            "item_name": self.item_name,
            "quantity": self.quantity,
            "status": self.status,
            "requested_at": self.requested_at,
        }
        if self.note is not None:
            data["note"] = self.note
        if self.resolved_at is not None:
            data["resolved_at"] = self.resolved_at
        if self.ledger_entry_id is not None:
            data["ledger_entry_id"] = self.ledger_entry_id
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "OrderRequest":
        return cls(
            request_id=data["request_id"],
            session_id=data["session_id"],
            player_id=data["player_id"],
            item_name=data["item_name"],
            quantity=data["quantity"],
            status=data["status"],
            requested_at=data["requested_at"],
            note=data.get("note"),
            resolved_at=data.get("resolved_at"),
            ledger_entry_id=data.get("ledger_entry_id"),
        )
