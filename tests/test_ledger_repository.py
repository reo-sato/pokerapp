"""tests/test_ledger_repository.py

Phase S3.1: ledger / points / settlement の core テスト。

検査対象（`docs/contracts/ledger-overview.md` / ADR-0016 / error-shapes.md）:
- ledger entry の add / 符号・kind validation / order 明細
- point 残高 = fold（ISSUE-0001）/ 残高不足拒否 / entry fee cash only / grant 冪等性
- ledger↔point 整合（point spend が linked entry を起こす）
- append-only reversal（原 entry 不変・point 払い戻し）
- settlement 計算（net_due = 符号付き Σ cash）/ closed 限定確定 / paid-unpaid
- persistence roundtrip / 参照整合（unknown player / session）
- code↔contract 整合（生成オブジェクトが schema に適合する）
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.ledger_repository import (
    AlreadySettledError,
    DuplicateGrantError,
    EntryFeeRequiresCashError,
    InsufficientPointsError,
    InvalidAmountError,
    LedgerNotFoundError,
    LedgerRepository,
    SessionNotClosedError,
    UnknownPlayerError,
)
from core.player_repository import PlayerRepository
from core.session_repository import SessionRepository


@pytest.fixture
def players(tmp_path: Path) -> PlayerRepository:
    repo = PlayerRepository(path=tmp_path / "players.json")
    repo.create_player("Alice")
    repo.create_player("Bob")
    return repo


@pytest.fixture
def sessions(tmp_path: Path, players: PlayerRepository) -> SessionRepository:
    return SessionRepository(path=tmp_path / "sessions.json", player_repo=players)


@pytest.fixture
def ledger(tmp_path: Path, sessions: SessionRepository, players: PlayerRepository) -> LedgerRepository:
    return LedgerRepository(
        path=tmp_path / "ledger.json", session_repo=sessions, player_repo=players
    )


def _pid(players: PlayerRepository, name: str) -> str:
    return next(p.player_id for p in players.list_players() if p.display_name == name)


# ――― add ledger entry ―――

def test_add_buy_in_cash_only(ledger: LedgerRepository, sessions: SessionRepository, players: PlayerRepository):
    s = sessions.create_session()
    e = ledger.add_entry(s.session_id, _pid(players, "Alice"), "buy_in", cash_amount=5000)
    assert e.kind == "buy_in"
    assert e.cash_amount == 5000 and e.point_amount == 0
    assert ledger.list_entries(session_id=s.session_id) == [e]


def test_invalid_kind_rejected(ledger: LedgerRepository, sessions: SessionRepository, players: PlayerRepository):
    s = sessions.create_session()
    with pytest.raises(ValueError):
        ledger.add_entry(s.session_id, _pid(players, "Alice"), "cashout", cash_amount=1000)


def test_unknown_player_rejected(ledger: LedgerRepository, sessions: SessionRepository):
    s = sessions.create_session()
    with pytest.raises(UnknownPlayerError):
        ledger.add_entry(s.session_id, "ff" * 16, "buy_in", cash_amount=1000)


def test_unknown_session_rejected(ledger: LedgerRepository, players: PlayerRepository):
    with pytest.raises(LedgerNotFoundError):
        ledger.add_entry("nope", _pid(players, "Alice"), "buy_in", cash_amount=1000)


def test_zero_movement_rejected(ledger: LedgerRepository, sessions: SessionRepository, players: PlayerRepository):
    s = sessions.create_session()
    with pytest.raises(InvalidAmountError):
        ledger.add_entry(s.session_id, _pid(players, "Alice"), "buy_in", cash_amount=0, point_amount=0)


def test_order_entry_with_detail(ledger: LedgerRepository, sessions: SessionRepository, players: PlayerRepository):
    s = sessions.create_session()
    e = ledger.add_entry(
        s.session_id, _pid(players, "Alice"), "order", cash_amount=1000,
        order={"item_name": "ドリンク", "unit_amount": 500, "quantity": 2},
    )
    assert e.order == {"item_name": "ドリンク", "unit_amount": 500, "quantity": 2}


def test_order_detail_only_for_order_kind(ledger: LedgerRepository, sessions: SessionRepository, players: PlayerRepository):
    s = sessions.create_session()
    with pytest.raises(InvalidAmountError):
        ledger.add_entry(
            s.session_id, _pid(players, "Alice"), "buy_in", cash_amount=1000,
            order={"item_name": "x", "unit_amount": 1, "quantity": 1},
        )


# ――― points: balance = fold (ISSUE-0001) ―――

def test_point_balance_matches_fold_of_entries(ledger: LedgerRepository, sessions: SessionRepository, players: PlayerRepository):
    alice = _pid(players, "Alice")
    ledger.grant_points(alice, 5000)
    s = sessions.create_session()
    ledger.add_entry(s.session_id, alice, "buy_in", cash_amount=3000, point_amount=2000)
    expected = sum(p.delta_points for p in ledger.list_point_entries(player_id=alice))
    assert ledger.point_balance(alice) == expected == 3000


def test_insufficient_points_rejected(ledger: LedgerRepository, sessions: SessionRepository, players: PlayerRepository):
    alice = _pid(players, "Alice")
    ledger.grant_points(alice, 1000)
    s = sessions.create_session()
    with pytest.raises(InsufficientPointsError):
        ledger.add_entry(s.session_id, alice, "buy_in", cash_amount=0, point_amount=2000)


def test_point_spend_creates_linked_entry(ledger: LedgerRepository, sessions: SessionRepository, players: PlayerRepository):
    alice = _pid(players, "Alice")
    ledger.grant_points(alice, 5000)
    s = sessions.create_session()
    e = ledger.add_entry(s.session_id, alice, "buy_in", cash_amount=3000, point_amount=2000)
    spends = [p for p in ledger.list_point_entries(player_id=alice) if p.reason == "spend_on_buyin"]
    assert len(spends) == 1
    assert spends[0].delta_points == -2000
    assert spends[0].related_ledger_entry_id == e.entry_id


def test_grant_idempotency(ledger: LedgerRepository, players: PlayerRepository):
    alice = _pid(players, "Alice")
    ledger.grant_points(alice, 1000, idempotency_key="campaign-2026-06")
    with pytest.raises(DuplicateGrantError):
        ledger.grant_points(alice, 1000, idempotency_key="campaign-2026-06")
    assert ledger.point_balance(alice) == 1000


def test_grant_must_be_positive(ledger: LedgerRepository, players: PlayerRepository):
    with pytest.raises(InvalidAmountError):
        ledger.grant_points(_pid(players, "Alice"), -100)


# ――― entry fee is cash only ―――

def test_entry_fee_rejects_points(ledger: LedgerRepository, sessions: SessionRepository, players: PlayerRepository):
    alice = _pid(players, "Alice")
    ledger.grant_points(alice, 5000)
    s = sessions.create_session()
    with pytest.raises(EntryFeeRequiresCashError):
        ledger.add_entry(s.session_id, alice, "entry_fee", cash_amount=500, point_amount=100)


def test_entry_fee_requires_positive_cash(ledger: LedgerRepository, sessions: SessionRepository, players: PlayerRepository):
    s = sessions.create_session()
    with pytest.raises(InvalidAmountError):
        ledger.add_entry(s.session_id, _pid(players, "Alice"), "entry_fee", cash_amount=0)


# ――― append-only reversal ―――

def test_reverse_entry_is_append_only(ledger: LedgerRepository, sessions: SessionRepository, players: PlayerRepository):
    alice = _pid(players, "Alice")
    ledger.grant_points(alice, 5000)
    s = sessions.create_session()
    e = ledger.add_entry(s.session_id, alice, "buy_in", cash_amount=3000, point_amount=2000)
    rev = ledger.reverse_entry(e.entry_id)

    # 原 entry は不変（mutate されない）、reversal が追記される
    entries = ledger.list_entries(session_id=s.session_id)
    assert e in entries and rev in entries and len(entries) == 2
    assert rev.reverses_entry_id == e.entry_id
    assert rev.cash_amount == -3000 and rev.point_amount == -2000
    # point は払い戻され残高が元に戻る、cash は net 0
    assert ledger.point_balance(alice) == 5000
    assert sum(x.cash_amount for x in entries) == 0


def test_cannot_double_reverse(ledger: LedgerRepository, sessions: SessionRepository, players: PlayerRepository):
    s = sessions.create_session()
    e = ledger.add_entry(s.session_id, _pid(players, "Alice"), "buy_in", cash_amount=3000)
    rev = ledger.reverse_entry(e.entry_id)
    with pytest.raises(InvalidAmountError):
        ledger.reverse_entry(e.entry_id)          # 既に reverse 済み
    with pytest.raises(InvalidAmountError):
        ledger.reverse_entry(rev.entry_id)        # reversal は再 reverse 不可


def test_reverse_unknown_entry_rejected(ledger: LedgerRepository):
    with pytest.raises(LedgerNotFoundError):
        ledger.reverse_entry("ab" * 16)


# ――― settlement ―――

def test_negative_adjustment_requires_note(ledger: LedgerRepository, sessions: SessionRepository, players: PlayerRepository):
    """B9/ADR-0035: 負の adjustment（店→player 方向）は理由 note 必須（アミューズ・ガードレール）。"""
    alice = _pid(players, "Alice")
    s = sessions.create_session()
    with pytest.raises(InvalidAmountError):
        ledger.add_entry(s.session_id, alice, "adjustment", cash_amount=-500)  # note なし
    with pytest.raises(InvalidAmountError):
        ledger.add_entry(s.session_id, alice, "adjustment", cash_amount=-500, note="   ")  # 空白のみ
    # 理由つきは OK。正の adjustment（誤記訂正の追加）は note なしでも可。
    assert ledger.add_entry(s.session_id, alice, "adjustment", cash_amount=-500,
                            note="飲食ミス返金").cash_amount == -500
    assert ledger.add_entry(s.session_id, alice, "adjustment", cash_amount=300).cash_amount == 300


def test_settlement_net_due_signed_sum_cash(ledger: LedgerRepository, sessions: SessionRepository, players: PlayerRepository):
    alice = _pid(players, "Alice")
    s = sessions.create_session()
    ledger.add_entry(s.session_id, alice, "buy_in", cash_amount=5000)
    ledger.add_entry(s.session_id, alice, "rebuy", cash_amount=3000)
    ledger.add_entry(s.session_id, alice, "order", cash_amount=1000)
    ledger.add_entry(s.session_id, alice, "entry_fee", cash_amount=500)
    ledger.add_entry(s.session_id, alice, "adjustment", cash_amount=-200, note="レジ誤記訂正")

    rows = ledger.compute_settlement(s.session_id)
    assert len(rows) == 1
    row = rows[0]
    assert row.player_id == alice
    assert row.cash_in_total == 8000           # buy_in + rebuy
    assert row.order_total == 1000
    assert row.entry_fee == 500
    assert row.net_due_to_store == 9300        # 5000+3000+1000+500-200
    assert row.payment_status == "unpaid"


def test_compute_settlement_speculative_for_open_session(ledger: LedgerRepository, sessions: SessionRepository, players: PlayerRepository):
    alice = _pid(players, "Alice")
    s = sessions.create_session()
    ledger.add_entry(s.session_id, alice, "buy_in", cash_amount=5000)
    # open のまま中間集計できる（確定はしない）
    rows = ledger.compute_settlement(s.session_id)
    assert rows[0].net_due_to_store == 5000
    assert ledger.list_settlements(s.session_id) == []  # 未確定


def test_commit_requires_closed_session(ledger: LedgerRepository, sessions: SessionRepository, players: PlayerRepository):
    alice = _pid(players, "Alice")
    s = sessions.create_session()
    ledger.add_entry(s.session_id, alice, "buy_in", cash_amount=5000)
    with pytest.raises(SessionNotClosedError):
        ledger.commit_settlement(s.session_id)

    sessions.close_session(s.session_id)
    committed = ledger.commit_settlement(s.session_id)
    assert len(committed) == 1 and committed[0].payment_status == "unpaid"
    with pytest.raises(AlreadySettledError):
        ledger.commit_settlement(s.session_id)


def test_set_payment_status(ledger: LedgerRepository, sessions: SessionRepository, players: PlayerRepository):
    alice = _pid(players, "Alice")
    s = sessions.create_session()
    ledger.add_entry(s.session_id, alice, "buy_in", cash_amount=5000)
    sessions.close_session(s.session_id)
    ledger.commit_settlement(s.session_id)

    updated = ledger.set_payment_status(s.session_id, alice, "paid")
    assert updated.payment_status == "paid"
    assert ledger.list_settlements(s.session_id)[0].payment_status == "paid"
    with pytest.raises(LedgerNotFoundError):
        ledger.set_payment_status(s.session_id, _pid(players, "Bob"), "paid")


# ――― persistence ―――

def test_persistence_roundtrip(tmp_path: Path, sessions: SessionRepository, players: PlayerRepository):
    db = tmp_path / "ledger.json"
    repo = LedgerRepository(path=db, session_repo=sessions, player_repo=players)
    alice = _pid(players, "Alice")
    repo.grant_points(alice, 5000)
    s = sessions.create_session(label="Persist")
    repo.add_entry(s.session_id, alice, "buy_in", cash_amount=3000, point_amount=2000)
    sessions.close_session(s.session_id)
    repo.commit_settlement(s.session_id)

    reloaded = LedgerRepository(path=db, session_repo=sessions, player_repo=players)
    assert reloaded.point_balance(alice) == 3000
    assert [e.cash_amount for e in reloaded.list_entries(session_id=s.session_id)] == [3000]
    settlements = reloaded.list_settlements(s.session_id)
    assert len(settlements) == 1 and settlements[0].net_due_to_store == 3000


# ――― code ↔ contract 整合 ―――

_SCHEMAS = Path(__file__).parent.parent / "docs" / "contracts" / "schemas"


def _validate(model: str, payload: dict) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((_SCHEMAS / f"{model}.schema.json").read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema).validate(payload)


def test_core_ledger_matches_contract(ledger: LedgerRepository, sessions: SessionRepository, players: PlayerRepository):
    alice = _pid(players, "Alice")
    ledger.grant_points(alice, 5000)
    s = sessions.create_session()
    e = ledger.add_entry(
        s.session_id, alice, "order", cash_amount=1000, point_amount=500,
        order={"item_name": "ドリンク", "unit_amount": 500, "quantity": 3}, hand_id=2,
    )
    _validate("ledger_entry", e.to_dict())
    for p in ledger.list_point_entries(player_id=alice):
        _validate("point_ledger_entry", p.to_dict())

    sessions.close_session(s.session_id)
    for row in ledger.commit_settlement(s.session_id):
        _validate("session_settlement", row.to_dict())
