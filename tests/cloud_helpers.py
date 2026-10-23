"""クラウドのテスト（ADR-0067）で使う、店舗 PC の本物の保存から作った push の本文。"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest

from core.cloud_payload import PROTOCOL
from core.ledger_repository import LedgerRepository
from core.order_request_repository import OrderRequestRepository
from core.player_repository import PlayerRepository
from core.session_repository import SessionRepository
from core.sync import build_snapshot

REPO_ROOT = Path(__file__).resolve().parent.parent
HAND_FIXTURE = REPO_ROOT / "docs" / "contracts" / "fixtures" / "hand" / "canonical.json"


def store_snapshot(root: Path) -> dict:
    """店舗 PC の各ストアを実際の repository で作り、`build_snapshot` で読んだもの。"""
    players = PlayerRepository(root / "players.json")
    sessions = SessionRepository(root / "sessions.json", player_repo=players)
    ledger = LedgerRepository(root / "ledger.json", session_repo=sessions, player_repo=players)
    orders = OrderRequestRepository(root / "order_requests.json", session_repo=sessions, player_repo=players)
    alice = players.create_player("アリス")
    bob = players.create_player("ボブ")
    session = sessions.create_session(label="金曜 #1", blinds={"sb": 100, "bb": 200})
    for hand_id in (1, 2):
        sessions.assign_seat(session.session_id, hand_id, 1, alice.player_id)
        sessions.assign_seat(session.session_id, hand_id, 2, bob.player_id)
    ledger.add_entry(session.session_id, alice.player_id, "buy_in", cash_amount=5000)
    ledger.add_entry(session.session_id, bob.player_id, "buy_in", cash_amount=5000)
    ledger.grant_points(alice.player_id, 300, session_id=session.session_id)
    req = orders.create_request(session.session_id, bob.player_id, "コーラ", 2)
    orders.confirm_request(req.request_id, 300, ledger)
    orders.create_request(session.session_id, alice.player_id, "お茶", 1)
    sessions.close_session(session.session_id)
    ledger.commit_settlement(session.session_id)
    ledger.record_payment(session.session_id, alice.player_id, 1000)
    log_dir = root / "logs"
    log_dir.mkdir()
    hands = []
    for hand_id in (1, 2):
        hand = json.loads(HAND_FIXTURE.read_text(encoding="utf-8"))
        hand.update(session_id=session.session_id, hand_id=hand_id)
        hand["actions"] = [{
            "hand_id": hand_id, "timestamp": "2026-06-05T04:00:01.000", "street": "preflop", "seat": 1,
            "player_name": "P1", "action": "call", "amount": 200, "pot_after": 500, "stack_after": 800,
            "source": {"audio": True, "rfid": False, "camera": False}, "needs_review": False,
            "confidence": 0.5, "raw_text": "コール", "actor_source": "engine_prior", "asr_confidence": 0.9,
            "apply_ok": True, "reason": "x", "corrected_from": None,
        }]
        hands.append(hand)
    (log_dir / f"{session.session_id}.json").write_text(
        json.dumps({"session_id": session.session_id, "hands": hands}, ensure_ascii=False), encoding="utf-8")
    snap = build_snapshot(players_path=players.path, sessions_path=sessions.path, ledger_path=ledger.path,
                          orders_path=orders.path, log_dir=log_dir)
    snap["ids"] = {"session_id": session.session_id, "alice": alice.player_id, "bob": bob.player_id}
    return snap


def push_payload(snap: dict, seq: int = 1, **extra) -> dict:
    hands = [h for doc in snap["hand_logs"].values() for h in doc["hands"]]
    payload = {
        "protocol": PROTOCOL,
        "seq": seq,
        "store_pc": {"branch": "stable", "commit": "a" * 40},
        "records": {
            "players": snap["players"], "sessions": snap["sessions"],
            "ledger_entries": snap["ledger_entries"], "point_entries": snap["point_entries"],
            "settlements": snap["settlements"], "order_requests": snap["order_requests"],
            "hands": hands, "corrections": [],
        },
    }
    payload.update(extra)
    return payload


def postgres_dsn() -> str | None:
    return os.environ.get("POKERAPP_TEST_DATABASE_URL") or None


def make_storage(kind: str):
    """("memory" | "postgres") の保存。postgres は試験用のスキーマを作り、終わったら消す（呼び出し側）。"""
    if kind == "memory":
        from cloud.storage import MemoryStorage

        return MemoryStorage()
    dsn = postgres_dsn()
    if not dsn:
        pytest.skip("POKERAPP_TEST_DATABASE_URL が無いので Postgres の試験は飛ばします（CI では設定）")
    psycopg = pytest.importorskip("psycopg")
    from cloud.postgres import PostgresStorage

    psycopg.connect(dsn, connect_timeout=3).close()   # つながらなければすぐ失敗させる（待ち続けない）

    storage = PostgresStorage(dsn, schema=f"t_{uuid.uuid4().hex[:12]}", max_size=2)
    storage.migrate()
    return storage
