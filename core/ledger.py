"""core/ledger.py

Phase S3a (M4, ADR-0014): session 中の金銭イベント 1 件 = LedgerEntry。

契約は docs/contracts/ledger.md / schemas/ledger_entry.schema.json (draft 0.x)。
S3a は cash-only（point_amount は常に 0。validation は ledger_repository が source of truth）。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class OrderDetail:
    """kind=order の明細サブ構造。"""

    item_name: str
    unit_amount: int
    quantity: int

    def to_dict(self) -> dict:
        return {
            "item_name": self.item_name,
            "unit_amount": self.unit_amount,
            "quantity": self.quantity,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "OrderDetail":
        return cls(
            item_name=data["item_name"],
            unit_amount=data["unit_amount"],
            quantity=data["quantity"],
        )


@dataclass
class LedgerEntry:
    """session 中の金銭イベント 1 件（append-only。訂正は adjustment を追記）。"""

    entry_id: str          # UUID4 hex (32 文字)。アプリ内採番・不変
    session_id: str
    player_id: str         # registry の UUID4 hex
    kind: str              # "buy_in" | "rebuy" | "add_on" | "order" | "adjustment"
    occurred_at: str       # ISO 8601
    cash_amount: int       # adjustment のみ負可
    point_amount: int = 0  # S3a では常に 0 (points_not_supported)
    note: str | None = None
    order: OrderDetail | None = None  # kind=order のみ

    def to_dict(self) -> dict:
        data: dict = {
            "entry_id": self.entry_id,
            "session_id": self.session_id,
            "player_id": self.player_id,
            "kind": self.kind,
            "occurred_at": self.occurred_at,
            "cash_amount": self.cash_amount,
            "point_amount": self.point_amount,
        }
        if self.note is not None:
            data["note"] = self.note
        if self.order is not None:
            data["order"] = self.order.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "LedgerEntry":
        return cls(
            entry_id=data["entry_id"],
            session_id=data["session_id"],
            player_id=data["player_id"],
            kind=data["kind"],
            occurred_at=data["occurred_at"],
            cash_amount=data["cash_amount"],
            point_amount=data.get("point_amount", 0),
            note=data.get("note"),
            order=OrderDetail.from_dict(data["order"]) if data.get("order") else None,
        )
