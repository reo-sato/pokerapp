"""tests/test_order_request_repository.py

Phase M5 (ADR-0018) — 注文リクエストの core テスト。

検査対象（docs/contracts/viewer-api.md § 注文リクエスト / error-shapes.md）:
- create_request の validation（open session / 実在 player / quantity 1..99 / item_name / note）
- 状態遷移 pending → confirmed | rejected、already_resolved、confirm の ledger リンク
- confirm 失敗時（ledger validation 透過）は pending のまま
- persistence roundtrip / reload-on-read（別プロセス write への追従）/ schema 適合
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.ledger_repository import LedgerRepository
from core.order_request_repository import (
    AlreadyResolvedError,
    InvalidOrderRequestError,
    OrderRequestNotFoundError,
    OrderRequestRepository,
    OrderSessionClosedError,
    OrderUnknownPlayerError,
)
from core.player_repository import PlayerRepository
from core.session_repository import SessionNotFoundError, SessionRepository

_SCHEMAS = Path(__file__).parent.parent / "docs" / "contracts" / "schemas"


@pytest.fixture
def env(tmp_path: Path) -> dict:
    players = PlayerRepository(path=tmp_path / "players.json")
    alice = players.create_player("Alice")
    sessions = SessionRepository(path=tmp_path / "sessions.json", player_repo=players)
    session = sessions.create_session(label="m5")
    ledger = LedgerRepository(path=tmp_path / "ledger.json", session_repo=sessions)
    orders = OrderRequestRepository(path=tmp_path / "order_requests.json",
                                    session_repo=sessions)
    return {"players": players, "sessions": sessions, "session": session,
            "ledger": ledger, "orders": orders, "alice": alice, "tmp": tmp_path}


class TestCreateRequest:
    def test_create_pending(self, env):
        r = env["orders"].create_request(
            env["session"].session_id, env["alice"].player_id, "ビール", 2, note="冷えたの")
        assert r.status == "pending"
        assert r.ledger_entry_id is None and r.resolved_at is None
        # ledger には何も書かれない（staff-in-the-loop, ADR-0018 §2）
        assert env["ledger"].list_entries(env["session"].session_id) == []

    @pytest.mark.parametrize("quantity", [0, 100, -1])
    def test_quantity_bounds(self, env, quantity):
        with pytest.raises(InvalidOrderRequestError):
            env["orders"].create_request(
                env["session"].session_id, env["alice"].player_id, "ビール", quantity)

    def test_item_and_note_validation(self, env):
        sid, pid = env["session"].session_id, env["alice"].player_id
        with pytest.raises(InvalidOrderRequestError):
            env["orders"].create_request(sid, pid, "   ", 1)
        with pytest.raises(InvalidOrderRequestError):
            env["orders"].create_request(sid, pid, "x" * 101, 1)
        with pytest.raises(InvalidOrderRequestError):
            env["orders"].create_request(sid, pid, "ビール", 1, note="x" * 201)

    def test_unknown_session_player_and_closed(self, env):
        with pytest.raises(SessionNotFoundError):
            env["orders"].create_request("deadbeef", env["alice"].player_id, "ビール", 1)
        with pytest.raises(OrderUnknownPlayerError):
            env["orders"].create_request(env["session"].session_id, "f" * 32, "ビール", 1)
        env["sessions"].close_session(env["session"].session_id)
        with pytest.raises(OrderSessionClosedError):
            env["orders"].create_request(
                env["session"].session_id, env["alice"].player_id, "ビール", 1)


class TestConfirmReject:
    def test_confirm_links_ledger_entry(self, env):
        sid, pid = env["session"].session_id, env["alice"].player_id
        r = env["orders"].create_request(sid, pid, "ジントニック", 3, note="氷少なめ")
        confirmed = env["orders"].confirm_request(r.request_id, 800, env["ledger"])
        assert confirmed.status == "confirmed"
        assert confirmed.resolved_at is not None
        entries = env["ledger"].list_entries(sid, pid)
        assert len(entries) == 1
        assert entries[0].entry_id == confirmed.ledger_entry_id
        assert entries[0].cash_amount == 2400  # 800 × 3
        assert entries[0].order["item_name"] == "ジントニック"
        assert entries[0].note == "氷少なめ"

    def test_reject_writes_nothing(self, env):
        sid, pid = env["session"].session_id, env["alice"].player_id
        r = env["orders"].create_request(sid, pid, "ビール", 1)
        rejected = env["orders"].reject_request(r.request_id)
        assert rejected.status == "rejected"
        assert env["ledger"].list_entries(sid) == []

    def test_already_resolved(self, env):
        sid, pid = env["session"].session_id, env["alice"].player_id
        r = env["orders"].create_request(sid, pid, "ビール", 1)
        env["orders"].reject_request(r.request_id)
        with pytest.raises(AlreadyResolvedError):
            env["orders"].confirm_request(r.request_id, 700, env["ledger"])
        with pytest.raises(AlreadyResolvedError):
            env["orders"].reject_request(r.request_id)

    def test_confirm_failure_keeps_pending(self, env):
        """確定失敗（closed session）では pending のまま（ledger には何も書かない）。

        verify-v1 ledger は closed session を拒否しないため、注文確定の closed ガードは
        OrderRequestRepository.confirm_request が担う（OrderSessionClosedError）。
        """
        sid, pid = env["session"].session_id, env["alice"].player_id
        r = env["orders"].create_request(sid, pid, "ビール", 1)
        env["sessions"].close_session(sid)
        with pytest.raises(OrderSessionClosedError):
            env["orders"].confirm_request(r.request_id, 700, env["ledger"])
        assert env["orders"].get(r.request_id).status == "pending"
        # ledger には何も書かれていない
        assert env["ledger"].list_entries(sid) == []

    def test_unknown_request(self, env):
        with pytest.raises(OrderRequestNotFoundError):
            env["orders"].confirm_request("f" * 32, 700, env["ledger"])


class TestListPersistenceAndContract:
    def test_list_filters(self, env):
        sid, pid = env["session"].session_id, env["alice"].player_id
        r1 = env["orders"].create_request(sid, pid, "ビール", 1)
        env["orders"].create_request(sid, pid, "コーラ", 2)
        env["orders"].reject_request(r1.request_id)
        assert len(env["orders"].list_requests(sid)) == 2
        assert [r.item_name for r in env["orders"].list_requests(sid, status="pending")] == ["コーラ"]
        assert len(env["orders"].list_requests(sid, player_id=pid)) == 2

    def test_roundtrip_and_reload_on_read(self, env):
        sid, pid = env["session"].session_id, env["alice"].player_id
        # reader を先に開いておき、writer の追記が reload-on-read で見えること
        reader = OrderRequestRepository(path=env["tmp"] / "order_requests.json",
                                        session_repo=env["sessions"])
        assert reader.list_requests(sid) == []
        env["orders"].create_request(sid, pid, "ビール", 1)
        import os
        os.utime(env["tmp"] / "order_requests.json")  # mtime 解像度対策
        assert [r.item_name for r in reader.list_requests(sid)] == ["ビール"]

    def test_request_matches_contract_schema(self, env):
        jsonschema = pytest.importorskip("jsonschema")
        sid, pid = env["session"].session_id, env["alice"].player_id
        schema = json.loads(
            (_SCHEMAS / "order_request.schema.json").read_text(encoding="utf-8"))
        validator = jsonschema.Draft202012Validator(schema)
        r = env["orders"].create_request(sid, pid, "ビール", 2, note="x")
        validator.validate(r.to_dict())
        validator.validate(
            env["orders"].confirm_request(r.request_id, 700, env["ledger"]).to_dict())
