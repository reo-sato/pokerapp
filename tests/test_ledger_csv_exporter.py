"""tests/test_ledger_csv_exporter.py

Phase S3.3: settlement / cashflow CSV エクスポートのテスト（ISSUE-0018）。

検査対象（`output/ledger_csv_exporter.py` / ADR-0016）:
- settlement CSV の列・行数・net_due 合計が repo と一致する。
- settlement は player→店の 1 方向（direction 列を持たない）。
- payment_status（paid/unpaid）が CSV に反映される。
- cashflow CSV に reversal が 1 行として含まれ、cash_amount 合計が一致する。
- player_name 列補完・日本語 note の round-trip（utf-8-sig）。
"""
from __future__ import annotations

import csv
from pathlib import Path

from core.ledger_repository import LedgerRepository
from core.player_repository import PlayerRepository
from core.session_repository import SessionRepository
from output.ledger_csv_exporter import (
    ENTRY_COLUMNS,
    SETTLEMENT_COLUMNS,
    LedgerCsvExporter,
)


def _make_repo(tmp_path: Path):
    players = PlayerRepository(path=tmp_path / "players.json")
    players.create_player("Alice")
    players.create_player("Bob")
    sessions = SessionRepository(path=tmp_path / "sessions.json", player_repo=players)
    ledger = LedgerRepository(
        path=tmp_path / "ledger.json", session_repo=sessions, player_repo=players
    )
    return ledger, sessions, players


def _pid(players: PlayerRepository, name: str) -> str:
    return next(p.player_id for p in players.list_players() if p.display_name == name)


def _read_csv(path: Path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    return rows[0], rows[1:]


def _names(players: PlayerRepository) -> dict[str, str]:
    return {p.player_id: p.display_name for p in players.list_players()}


def _committed(ledger, sessions, players):
    """Alice (buy_in 5000 + order 1000) / Bob (buy_in 3000) を確定した session を返す。"""
    alice, bob = _pid(players, "Alice"), _pid(players, "Bob")
    s = sessions.create_session(label="Fri")
    ledger.add_entry(s.session_id, alice, "buy_in", cash_amount=5000)
    ledger.add_entry(s.session_id, alice, "order", cash_amount=1000)
    ledger.add_entry(s.session_id, bob, "buy_in", cash_amount=3000)
    sessions.close_session(s.session_id)
    ledger.commit_settlement(s.session_id)
    return s


def test_export_settlements_columns_and_totals(tmp_path: Path):
    ledger, sessions, players = _make_repo(tmp_path)
    _committed(ledger, sessions, players)

    out = LedgerCsvExporter().export_settlements(
        ledger.all_settlements(), tmp_path / "settlements.csv", player_names=_names(players)
    )
    header, rows = _read_csv(out)

    assert header == SETTLEMENT_COLUMNS
    assert len(rows) == 2
    net_idx = SETTLEMENT_COLUMNS.index("net_due_to_store")
    csv_total = sum(int(r[net_idx]) for r in rows)
    repo_total = sum(s.net_due_to_store for s in ledger.all_settlements())
    assert csv_total == repo_total == 9000  # Alice 6000 + Bob 3000


def test_settlement_csv_is_player_to_store_only(tmp_path: Path):
    ledger, sessions, players = _make_repo(tmp_path)
    _committed(ledger, sessions, players)
    out = LedgerCsvExporter().export_settlements(ledger.all_settlements(), tmp_path / "s.csv")
    header, _ = _read_csv(out)
    # player→店 の 1 方向: from/to/counterparty のような相手方向の列を持たない
    assert not any(k in header for k in ("from", "to", "counterparty", "payer", "payee"))
    assert "net_due_to_store" in header


def test_payment_status_reflected_in_csv(tmp_path: Path):
    ledger, sessions, players = _make_repo(tmp_path)
    s = _committed(ledger, sessions, players)
    alice = _pid(players, "Alice")
    ledger.set_payment_status(s.session_id, alice, "paid")

    out = LedgerCsvExporter().export_settlements(ledger.all_settlements(), tmp_path / "s.csv")
    header, rows = _read_csv(out)
    pid_idx = header.index("player_id")
    status_idx = header.index("payment_status")
    by_pid = {r[pid_idx]: r[status_idx] for r in rows}
    assert by_pid[alice] == "paid"
    assert by_pid[_pid(players, "Bob")] == "unpaid"


def test_export_entries_cashflow_with_reversal(tmp_path: Path):
    ledger, sessions, players = _make_repo(tmp_path)
    alice = _pid(players, "Alice")
    s = sessions.create_session()
    e = ledger.add_entry(s.session_id, alice, "buy_in", cash_amount=3000)
    ledger.reverse_entry(e.entry_id)

    out = LedgerCsvExporter().export_entries(
        ledger.list_entries(), tmp_path / "cashflow.csv", player_names=_names(players)
    )
    header, rows = _read_csv(out)
    assert header == ENTRY_COLUMNS
    assert len(rows) == 2  # 原 entry + reversal
    cash_idx = header.index("cash_amount")
    assert sum(int(r[cash_idx]) for r in rows) == 0  # reversal で net 0
    rev_idx = header.index("reverses_entry_id")
    assert any(r[rev_idx] == e.entry_id for r in rows)  # reversal 行が原 entry を指す


def test_player_name_column_populated(tmp_path: Path):
    ledger, sessions, players = _make_repo(tmp_path)
    _committed(ledger, sessions, players)
    out = LedgerCsvExporter().export_settlements(
        ledger.all_settlements(), tmp_path / "s.csv", player_names=_names(players)
    )
    header, rows = _read_csv(out)
    pid_idx, name_idx = header.index("player_id"), header.index("player_name")
    by_pid = {r[pid_idx]: r[name_idx] for r in rows}
    assert by_pid[_pid(players, "Alice")] == "Alice"


def test_export_empty_settlements_writes_header_only(tmp_path: Path):
    ledger, sessions, players = _make_repo(tmp_path)
    out = LedgerCsvExporter().export_settlements([], tmp_path / "empty.csv")
    header, rows = _read_csv(out)
    assert header == SETTLEMENT_COLUMNS
    assert rows == []


def test_japanese_note_roundtrips(tmp_path: Path):
    ledger, sessions, players = _make_repo(tmp_path)
    alice = _pid(players, "Alice")
    s = sessions.create_session()
    ledger.add_entry(
        s.session_id, alice, "order", cash_amount=1000,
        order={"item_name": "ドリンク", "unit_amount": 500, "quantity": 2}, note="差し入れ",
    )
    out = LedgerCsvExporter().export_entries(ledger.list_entries(), tmp_path / "c.csv")
    header, rows = _read_csv(out)
    note_idx = header.index("note")
    assert rows[0][note_idx] == "差し入れ"
