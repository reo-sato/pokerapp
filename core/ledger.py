"""core/ledger.py

Phase S3: session ledger と point ledger の最小ドメインモデル。

2 つの contract model（`docs/contracts/ledger-points.md` / ADR-0013）に対応する:

- ``LedgerEntry``      … session 中の金銭イベント 1 件（buy_in / rebuy / add_on / order /
                         adjustment / entry_fee）。cash と point の併用を 1 行で表す。
- ``PointLedgerEntry`` … prize point の増減 1 件。残高はこの entry 列の fold が
                         source of truth（ADR-0013, ISSUE-0001 決着）。

業務ルール（kind/reason の整合・残高・cash 補完・冪等性）は
``core/ledger_repository.py`` が source of truth として enforce する。
本モジュールは純粋なデータ構造に徹する。

各 ``to_dict`` は対応 schema の ``additionalProperties: false`` に合わせ、absent な
optional フィールドはキーごと省略する。
"""
from __future__ import annotations

from dataclasses import dataclass

LEDGER_KINDS = ("buy_in", "rebuy", "add_on", "order", "adjustment", "entry_fee")

POINT_GRANT_REASONS = ("manual_grant", "result_credit", "campaign_grant")
POINT_SPEND_REASONS = (
    "spend_on_buyin", "spend_on_rebuy", "spend_on_addon", "spend_on_order",
)
POINT_REASONS = POINT_GRANT_REASONS + POINT_SPEND_REASONS + ("adjustment",)

# point 充当可能な ledger kind → 対応する spend reason（業務ルール 2: 併用可は 4 種のみ）
SPEND_REASON_BY_KIND = {
    "buy_in": "spend_on_buyin",
    "rebuy": "spend_on_rebuy",
    "add_on": "spend_on_addon",
    "order": "spend_on_order",
}


@dataclass
class LedgerEntry:
    """session 中の金銭イベント 1 件。

    属性:
        entry_id:     永続・安定な識別子（ledger レイヤが採番する UUID hex）。
        session_id:   紐づく session（S2 session レイヤの session_id）。
        player_id:    対象 player（S1 registry の player_id）。
        kind:         ``buy_in`` | ``rebuy`` | ``add_on`` | ``order`` | ``adjustment`` | ``entry_fee``。
        occurred_at:  発生時刻（ISO 8601）。
        cash_amount:  cash 計上額。adjustment のみ負値可（返金等）。
        point_amount: point 充当額（常に 0 以上）。entry_fee / adjustment では 0。
        note:         任意メモ。
        order:        ``kind="order"`` のみ必須の明細 ``{item_name, unit_amount, quantity}``。
    """

    entry_id: str
    session_id: str
    player_id: str
    kind: str
    occurred_at: str
    cash_amount: int
    point_amount: int
    note: str | None = None
    order: dict | None = None

    def to_dict(self) -> dict:
        d: dict = {
            "entry_id": self.entry_id,
            "session_id": self.session_id,
            "player_id": self.player_id,
            "kind": self.kind,
            "occurred_at": self.occurred_at,
            "cash_amount": self.cash_amount,
            "point_amount": self.point_amount,
        }
        if self.note is not None:
            d["note"] = self.note
        if self.order is not None:
            d["order"] = dict(self.order)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "LedgerEntry":
        return cls(
            entry_id=d["entry_id"],
            session_id=d["session_id"],
            player_id=d["player_id"],
            kind=d["kind"],
            occurred_at=d["occurred_at"],
            cash_amount=d["cash_amount"],
            point_amount=d["point_amount"],
            note=d.get("note"),
            order=d.get("order"),
        )


@dataclass
class PointLedgerEntry:
    """prize point の増減 1 件。player 残高 = 当該 player の delta_points 総和（fold）。

    属性:
        entry_id:                永続・安定な識別子（UUID hex）。
        player_id:               対象 player。
        delta_points:            増減（非 0。grant 系は正、spend 系は負、adjustment は両方向）。
        reason:                  増減理由（``POINT_REASONS``）。
        occurred_at:             発生時刻（ISO 8601）。
        related_ledger_entry_id: spend 系で、起点の ``LedgerEntry`` への back-link。
        idempotency_key:         grant 系の重複防止キー（任意。repository が一意性を enforce）。
        note:                    任意メモ。
    """

    entry_id: str
    player_id: str
    delta_points: int
    reason: str
    occurred_at: str
    related_ledger_entry_id: str | None = None
    idempotency_key: str | None = None
    note: str | None = None

    def to_dict(self) -> dict:
        d: dict = {
            "entry_id": self.entry_id,
            "player_id": self.player_id,
            "delta_points": self.delta_points,
            "reason": self.reason,
            "occurred_at": self.occurred_at,
        }
        if self.related_ledger_entry_id is not None:
            d["related_ledger_entry_id"] = self.related_ledger_entry_id
        if self.idempotency_key is not None:
            d["idempotency_key"] = self.idempotency_key
        if self.note is not None:
            d["note"] = self.note
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "PointLedgerEntry":
        return cls(
            entry_id=d["entry_id"],
            player_id=d["player_id"],
            delta_points=d["delta_points"],
            reason=d["reason"],
            occurred_at=d["occurred_at"],
            related_ledger_entry_id=d.get("related_ledger_entry_id"),
            idempotency_key=d.get("idempotency_key"),
            note=d.get("note"),
        )
