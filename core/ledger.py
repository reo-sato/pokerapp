"""core/ledger.py

Phase S3.1: ledger / points / settlement の最小ドメインモデル。

3 つの contract model（`docs/contracts/ledger-overview.md` / `ledger-schema.md` / ADR-0016）に対応する:

- ``LedgerEntry``       … 金銭/価値イベント 1 件（append-only, cash+point 併用可）。
- ``PointLedgerEntry``  … ポイント増減 1 件（append-only, 残高 = delta_points の fold が source of truth）。
- ``SessionSettlement`` … session 締めの (session, player) 1 行（entries の derived materialized view）。

業務ルール（符号制約・残高非負・ledger↔point 整合・settlement 計算・状態遷移）は
``core/ledger_repository.py`` が source of truth として enforce する。本モジュールは純粋な
データ構造に徹する。

各 ``to_dict`` は対応 schema の ``additionalProperties: false`` に合わせ、absent な optional
フィールドはキーごと省略する（session の流儀に揃える）。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class LedgerEntry:
    """金銭/価値イベント 1 件。

    属性:
        entry_id:          UUID4 hex（ledger レイヤが採番）。
        session_id:        所属 session（S2）。
        player_id:         registry の player_id。
        kind:              ``buy_in`` | ``rebuy`` | ``add_on`` | ``order`` | ``entry_fee`` | ``adjustment``。
        occurred_at:       発生時刻（ISO 8601）。
        cash_amount:       cash 分（整数円）。通常 kind は >=0、adjustment/reversal は符号付き。
        point_amount:      point 充当分（整数点）。通常 spend は >=0、reversal は負。
        note:              任意の備考。
        hand_id:           任意。将来 rake/fee フック（S3 未使用）。
        reverses_entry_id: 任意。reversal のとき相殺対象の entry_id。
        order:             任意。``kind=order`` の明細 ``{item_name, unit_amount, quantity}``。
    """

    entry_id: str
    session_id: str
    player_id: str
    kind: str
    occurred_at: str
    cash_amount: int
    point_amount: int
    note: str | None = None
    hand_id: int | None = None
    reverses_entry_id: str | None = None
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
        if self.hand_id is not None:
            d["hand_id"] = self.hand_id
        if self.reverses_entry_id is not None:
            d["reverses_entry_id"] = self.reverses_entry_id
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
            hand_id=d.get("hand_id"),
            reverses_entry_id=d.get("reverses_entry_id"),
            order=d.get("order"),
        )


@dataclass
class PointLedgerEntry:
    """ポイント増減 1 件。player の point 残高 = この台帳の ``delta_points`` の fold。

    属性:
        entry_id:                UUID4 hex。
        player_id:               registry の player_id。
        delta_points:            符号付き増減（grant は >0、spend は <0）。
        reason:                  増減理由（manual_grant / result_credit / campaign_grant /
                                 spend_on_* / adjustment）。
        occurred_at:             発生時刻（ISO 8601）。
        related_ledger_entry_id: 任意。spend / reversal-refund のとき関係 ledger_entry。
        session_id:              任意。session スコープの集計用。
        idempotency_key:         任意。grant 重複防止キー。
    """

    entry_id: str
    player_id: str
    delta_points: int
    reason: str
    occurred_at: str
    related_ledger_entry_id: str | None = None
    session_id: str | None = None
    idempotency_key: str | None = None

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
        if self.session_id is not None:
            d["session_id"] = self.session_id
        if self.idempotency_key is not None:
            d["idempotency_key"] = self.idempotency_key
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
            session_id=d.get("session_id"),
            idempotency_key=d.get("idempotency_key"),
        )


@dataclass
class SessionSettlement:
    """session 締めの (session, player) 1 行。entries の derived materialized view。

    ``net_due_to_store`` = 当該 session・player の ``cash_amount`` の符号付き総和。常に
    player→店 の 1 方向。``payment_status`` のみ確定後に可変。
    """

    session_id: str
    player_id: str
    cash_in_total: int
    point_spent_total: int
    order_total: int
    entry_fee: int
    point_credited_total: int
    net_due_to_store: int
    payment_status: str
    settled_at: str

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "player_id": self.player_id,
            "cash_in_total": self.cash_in_total,
            "point_spent_total": self.point_spent_total,
            "order_total": self.order_total,
            "entry_fee": self.entry_fee,
            "point_credited_total": self.point_credited_total,
            "net_due_to_store": self.net_due_to_store,
            "payment_status": self.payment_status,
            "settled_at": self.settled_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SessionSettlement":
        return cls(
            session_id=d["session_id"],
            player_id=d["player_id"],
            cash_in_total=d["cash_in_total"],
            point_spent_total=d["point_spent_total"],
            order_total=d["order_total"],
            entry_fee=d["entry_fee"],
            point_credited_total=d["point_credited_total"],
            net_due_to_store=d["net_due_to_store"],
            payment_status=d["payment_status"],
            settled_at=d["settled_at"],
        )
