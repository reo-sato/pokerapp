"""output/ledger_csv_exporter.py

Phase S3.3: ledger / settlement の CSV エクスポート（ISSUE-0014）。

home game host が家計簿 / Excel で再集計できるよう、settlement（session 締めの精算）と
cashflow（ledger entry 1 件ずつ）を CSV に書き出す。Excel で日本語が文字化けしないよう
**UTF-8 with BOM（utf-8-sig）** で書く。

本モジュールは **純粋な書き出し**（データ + 出力先を受け取り CSV を書く）に徹し、
`LedgerRepository` / GUI には依存しない（`output/phh_exporter.py` と同じ思想）。
settlement の確定（commit）/ paid-unpaid 操作は `core/ledger_repository.py`（S3.1）が source of truth。

settlement schema の `1.0` freeze は S4。本フェーズは derived view + export まで。
"""
from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from core.ledger import LedgerEntry, SessionSettlement

logger = logging.getLogger(__name__)

# 1 行 = (session, player) の確定 settlement。net_due_to_store は常に player→店（1 方向）。
SETTLEMENT_COLUMNS = [
    "session_id",
    "player_id",
    "player_name",
    "cash_in_total",
    "point_spent_total",
    "order_total",
    "entry_fee",
    "point_credited_total",
    "net_due_to_store",
    "payment_status",
    "settled_at",
]

# 1 行 = ledger entry 1 件（reversal 含む）。再集計用の生キャッシュフロー。
ENTRY_COLUMNS = [
    "occurred_at",
    "session_id",
    "player_id",
    "player_name",
    "kind",
    "cash_amount",
    "point_amount",
    "note",
    "reverses_entry_id",
    "entry_id",
]


class LedgerCsvExporter:
    """settlement / cashflow を CSV に書き出す（Excel 向け utf-8-sig）。"""

    def export_settlements(
        self,
        settlements: list["SessionSettlement"],
        path: str | Path,
        player_names: Optional[dict[str, str]] = None,
    ) -> Path:
        """settlement 行を CSV に書き出す。`player_names` で display_name 列を補完できる。"""
        names = player_names or {}
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(SETTLEMENT_COLUMNS)
            for s in settlements:
                writer.writerow(
                    [
                        s.session_id,
                        s.player_id,
                        names.get(s.player_id, ""),
                        s.cash_in_total,
                        s.point_spent_total,
                        s.order_total,
                        s.entry_fee,
                        s.point_credited_total,
                        s.net_due_to_store,
                        s.payment_status,
                        s.settled_at,
                    ]
                )
        logger.info("Settlement CSV exported: %s (%d rows)", path, len(settlements))
        return path

    def export_entries(
        self,
        entries: list["LedgerEntry"],
        path: str | Path,
        player_names: Optional[dict[str, str]] = None,
    ) -> Path:
        """ledger entry（cashflow）を CSV に書き出す。reversal も 1 行として含む。"""
        names = player_names or {}
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(ENTRY_COLUMNS)
            for e in entries:
                writer.writerow(
                    [
                        e.occurred_at,
                        e.session_id,
                        e.player_id,
                        names.get(e.player_id, ""),
                        e.kind,
                        e.cash_amount,
                        e.point_amount,
                        e.note or "",
                        e.reverses_entry_id or "",
                        e.entry_id,
                    ]
                )
        logger.info("Cashflow CSV exported: %s (%d rows)", path, len(entries))
        return path
