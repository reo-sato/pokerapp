"""tests/test_ledger_repository.py

Phase S3a (M4, ADR-0014) — cash-only ledger の core テスト。

検査対象（docs/contracts/ledger.md / validation-rules.md / error-shapes.md）:
- add_entry の kind 別 validation（buy_in 系 / order 明細 / adjustment / points_not_supported）
- session open 要件 / unknown player / unknown session
- list_entries（追記順・player 絞り込み）と session_player_summary（中間集計）
- persistence roundtrip と code↔contract（生成 entry が ledger_entry schema に適合）
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.ledger_repository import (
    InvalidAmountError,
    InvalidKindError,
    LedgerRepository,
    LedgerSessionClosedError,
    LedgerUnknownPlayerError,
    PointsNotSupportedError,
)
from core.player_repository import PlayerRepository
from core.session_repository import SessionNotFoundError, SessionRepository

_SCHEMAS = Path(__file__).parent.parent / "docs" / "contracts" / "schemas"


@pytest.fixture
def env(tmp_path: Path) -> dict:
    players = PlayerRepository(path=tmp_path / "players.json")
    alice = players.create_player("Alice")
    bob = players.create_player("Bob")
    sessions = SessionRepository(path=tmp_path / "sessions.json", player_repo=players)
    session = sessions.create_session(label="ledger-test")
    ledger = LedgerRepository(path=tmp_path / "ledger.json", session_repo=sessions)
    return {"players": players, "sessions": sessions, "session": session,
            "ledger": ledger, "alice": alice, "bob": bob, "tmp": tmp_path}


class TestAddEntryValidation:
    def test_buyin_kinds_require_positive_cash(self, env):
        for kind in ("buy_in", "rebuy", "add_on"):
            entry = env["ledger"].add_entry(
                env["session"].session_id, env["alice"].player_id, kind, 10000)
            assert entry.kind == kind and entry.cash_amount == 10000
            with pytest.raises(InvalidAmountError):
                env["ledger"].add_entry(
                    env["session"].session_id, env["alice"].player_id, kind, 0)

    def test_order_requires_matching_detail(self, env):
        sid, pid = env["session"].session_id, env["alice"].player_id
        entry = env["ledger"].add_entry(
            sid, pid, "order", 1500, item_name="ジントニック", unit_amount=500, quantity=3)
        assert entry.order.quantity == 3
        with pytest.raises(InvalidAmountError):  # 明細なし
            env["ledger"].add_entry(sid, pid, "order", 500)
        with pytest.raises(InvalidAmountError):  # 単価×数量と不一致
            env["ledger"].add_entry(sid, pid, "order", 999,
                                    item_name="コーラ", unit_amount=500, quantity=1)
        with pytest.raises(InvalidAmountError):  # quantity 0
            env["ledger"].add_entry(sid, pid, "order", 0,
                                    item_name="コーラ", unit_amount=500, quantity=0)
        with pytest.raises(InvalidAmountError):  # buy_in に明細
            env["ledger"].add_entry(sid, pid, "buy_in", 500,
                                    item_name="コーラ", unit_amount=500, quantity=1)

    def test_adjustment_allows_negative_but_not_zero(self, env):
        sid, pid = env["session"].session_id, env["alice"].player_id
        entry = env["ledger"].add_entry(sid, pid, "adjustment", -500, note="返金")
        assert entry.cash_amount == -500
        with pytest.raises(InvalidAmountError):
            env["ledger"].add_entry(sid, pid, "adjustment", 0)

    def test_points_not_supported_in_s3a(self, env):
        with pytest.raises(PointsNotSupportedError):
            env["ledger"].add_entry(env["session"].session_id, env["alice"].player_id,
                                    "buy_in", 10000, point_amount=100)

    def test_invalid_kind(self, env):
        with pytest.raises(InvalidKindError):
            env["ledger"].add_entry(env["session"].session_id, env["alice"].player_id,
                                    "entry_fee", 1000)

    def test_unknown_session_and_player(self, env):
        with pytest.raises(SessionNotFoundError):
            env["ledger"].add_entry("deadbeef", env["alice"].player_id, "buy_in", 1000)
        with pytest.raises(LedgerUnknownPlayerError):
            env["ledger"].add_entry(env["session"].session_id, "f" * 32, "buy_in", 1000)

    def test_closed_session_rejected(self, env):
        env["sessions"].close_session(env["session"].session_id)
        with pytest.raises(LedgerSessionClosedError):
            env["ledger"].add_entry(env["session"].session_id, env["alice"].player_id,
                                    "buy_in", 1000)


class TestListAndSummary:
    def test_list_in_append_order_with_player_filter(self, env):
        sid = env["session"].session_id
        env["ledger"].add_entry(sid, env["alice"].player_id, "buy_in", 10000)
        env["ledger"].add_entry(sid, env["bob"].player_id, "buy_in", 20000)
        env["ledger"].add_entry(sid, env["alice"].player_id, "order", 500,
                                item_name="コーラ", unit_amount=500, quantity=1)
        assert [e.kind for e in env["ledger"].list_entries(sid)] == ["buy_in", "buy_in", "order"]
        alice_entries = env["ledger"].list_entries(sid, env["alice"].player_id)
        assert [e.cash_amount for e in alice_entries] == [10000, 500]

    def test_summary_totals(self, env):
        sid, pid = env["session"].session_id, env["alice"].player_id
        env["ledger"].add_entry(sid, pid, "buy_in", 10000)
        env["ledger"].add_entry(sid, pid, "rebuy", 10000)
        env["ledger"].add_entry(sid, pid, "add_on", 5000)
        env["ledger"].add_entry(sid, pid, "order", 1500,
                                item_name="ジントニック", unit_amount=500, quantity=3)
        env["ledger"].add_entry(sid, pid, "adjustment", -500)
        # 他 player の entry は混ざらない
        env["ledger"].add_entry(sid, env["bob"].player_id, "buy_in", 99999)

        summary = env["ledger"].session_player_summary(sid, pid)
        assert summary == {
            "buy_in_total": 25000,
            "order_total": 1500,
            "adjustment_total": -500,
            "total_due": 26000,
        }

    def test_summary_empty_and_unknown_player(self, env):
        summary = env["ledger"].session_player_summary(
            env["session"].session_id, env["alice"].player_id)
        assert summary["total_due"] == 0
        with pytest.raises(LedgerUnknownPlayerError):
            env["ledger"].session_player_summary(env["session"].session_id, "f" * 32)


class TestPersistenceAndContract:
    def test_roundtrip(self, env):
        sid, pid = env["session"].session_id, env["alice"].player_id
        env["ledger"].add_entry(sid, pid, "order", 1000, note="メモ",
                                item_name="ビール", unit_amount=500, quantity=2)
        reloaded = LedgerRepository(path=env["tmp"] / "ledger.json",
                                    session_repo=env["sessions"])
        entries = reloaded.list_entries(sid)
        assert len(entries) == 1
        e = entries[0]
        assert (e.kind, e.cash_amount, e.note, e.order.item_name) == \
            ("order", 1000, "メモ", "ビール")

    def test_entry_matches_contract_schema(self, env):
        jsonschema = pytest.importorskip("jsonschema")
        sid, pid = env["session"].session_id, env["alice"].player_id
        schema = json.loads(
            (_SCHEMAS / "ledger_entry.schema.json").read_text(encoding="utf-8"))
        validator = jsonschema.Draft202012Validator(schema)
        validator.validate(
            env["ledger"].add_entry(sid, pid, "buy_in", 10000).to_dict())
        validator.validate(
            env["ledger"].add_entry(sid, pid, "order", 1500, note="x",
                                    item_name="ジントニック", unit_amount=500,
                                    quantity=3).to_dict())
        validator.validate(
            env["ledger"].add_entry(sid, pid, "adjustment", -500).to_dict())
