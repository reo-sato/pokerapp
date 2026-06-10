"""tests/test_ledger_repository.py

Phase S3: session ledger / point ledger の core テスト。

検査対象（`docs/contracts/ledger-points.md` / ADR-0013 / error-shapes.md）:
- add entry success（cash only / cash+point 併用 / order 明細）
- kind / 金額 / order 明細 / session 状態 / player 実在の validation
- point 充当時の spend 系 point_ledger_entry 同時生成（back-link 込み）
- persistence roundtrip（再起動相当の再ロード）
- 中間集計 session_totals（buy-in 合計 / 注文合計）
- grant / adjust / list の point ledger 操作

ISSUE-0001 の決着を固定する残高系の回帰テストは `tests/test_point_ledger.py`。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.ledger_repository import (
    EntryFeeRequiresCashError,
    InvalidAmountError,
    InvalidKindError,
    InvalidOrderDetailError,
    InvalidReasonError,
    LedgerRepository,
)
from core.player_repository import PlayerRepository
from core.session_repository import (
    SessionClosedError,
    SessionNotFoundError,
    SessionRepository,
    UnknownPlayerError,
)


@pytest.fixture
def players(tmp_path: Path) -> PlayerRepository:
    repo = PlayerRepository(path=tmp_path / "players.json")
    repo.create_player("Alice")
    repo.create_player("Bob")
    return repo


@pytest.fixture
def sessions(tmp_path: Path, players: PlayerRepository) -> SessionRepository:
    repo = SessionRepository(path=tmp_path / "sessions.json", player_repo=players)
    repo.create_session(label="Friday #1")
    return repo


@pytest.fixture
def repo(
    tmp_path: Path, players: PlayerRepository, sessions: SessionRepository
) -> LedgerRepository:
    return LedgerRepository(
        path=tmp_path / "ledger.json", session_repo=sessions, player_repo=players
    )


def _pid(players: PlayerRepository, name: str) -> str:
    return next(p.player_id for p in players.list_players() if p.display_name == name)


def _sid(sessions: SessionRepository) -> str:
    return sessions.list_sessions()[0].session_id


# ――― add entry ―――

def test_add_cash_entry(repo, players, sessions):
    entry = repo.add_entry(_sid(sessions), _pid(players, "Alice"), "buy_in", cash_amount=10000)
    assert entry.kind == "buy_in"
    assert (entry.cash_amount, entry.point_amount) == (10000, 0)
    assert entry.entry_id
    assert repo.list_entries(session_id=_sid(sessions)) == [entry]
    # point 充当なしなら point ledger は増えない
    assert repo.list_point_entries() == []


def test_add_entry_with_points_creates_linked_spend(repo, players, sessions):
    alice = _pid(players, "Alice")
    repo.grant_points(alice, 5000, "manual_grant")
    entry = repo.add_entry(
        _sid(sessions), alice, "rebuy", cash_amount=7000, point_amount=3000
    )
    spends = [e for e in repo.list_point_entries(alice) if e.delta_points < 0]
    assert len(spends) == 1
    assert spends[0].reason == "spend_on_rebuy"
    assert spends[0].delta_points == -3000
    assert spends[0].related_ledger_entry_id == entry.entry_id
    assert repo.point_balance(alice) == 2000


def test_order_entry_requires_consistent_detail(repo, players, sessions):
    sid, alice = _sid(sessions), _pid(players, "Alice")
    order = {"item_name": "ジントニック", "unit_amount": 600, "quantity": 2}
    entry = repo.add_entry(sid, alice, "order", cash_amount=1200, order=order)
    assert entry.order == order

    with pytest.raises(InvalidOrderDetailError):
        repo.add_entry(sid, alice, "order", cash_amount=1200)  # 明細なし
    with pytest.raises(InvalidOrderDetailError):
        repo.add_entry(sid, alice, "order", cash_amount=1000, order=order)  # 合計不一致
    with pytest.raises(InvalidOrderDetailError):
        repo.add_entry(
            sid, alice, "order", cash_amount=600,
            order={"item_name": "", "unit_amount": 600, "quantity": 1},
        )
    with pytest.raises(InvalidOrderDetailError):
        repo.add_entry(sid, alice, "buy_in", cash_amount=600, order=order)  # order 以外に明細


def test_adjustment_allows_negative_cash_but_not_points(repo, players, sessions):
    sid, alice = _sid(sessions), _pid(players, "Alice")
    entry = repo.add_entry(sid, alice, "adjustment", cash_amount=-500, note="返金")
    assert entry.cash_amount == -500
    with pytest.raises(InvalidAmountError):
        repo.add_entry(sid, alice, "adjustment", cash_amount=0)
    with pytest.raises(InvalidAmountError):
        repo.add_entry(sid, alice, "adjustment", cash_amount=100, point_amount=100)


def test_add_entry_rejects_bad_inputs(repo, players, sessions):
    sid, alice = _sid(sessions), _pid(players, "Alice")
    with pytest.raises(InvalidKindError):
        repo.add_entry(sid, alice, "cash_out", cash_amount=100)
    with pytest.raises(InvalidAmountError):
        repo.add_entry(sid, alice, "buy_in", cash_amount=-100)
    with pytest.raises(InvalidAmountError):
        repo.add_entry(sid, alice, "buy_in", cash_amount=0, point_amount=0)
    with pytest.raises(InvalidAmountError):
        repo.add_entry(sid, alice, "buy_in", cash_amount=100.5)  # 非整数
    with pytest.raises(UnknownPlayerError):
        repo.add_entry(sid, "deadbeef", "buy_in", cash_amount=100)
    with pytest.raises(SessionNotFoundError):
        repo.add_entry("deadbeef", alice, "buy_in", cash_amount=100)


def test_add_entry_rejected_on_closed_session(repo, players, sessions):
    sid = _sid(sessions)
    sessions.close_session(sid)
    with pytest.raises(SessionClosedError):
        repo.add_entry(sid, _pid(players, "Alice"), "buy_in", cash_amount=100)


# ――― grant / adjust validation ―――

def test_grant_rejects_bad_reason_and_amount(repo, players):
    alice = _pid(players, "Alice")
    with pytest.raises(InvalidReasonError):
        repo.grant_points(alice, 100, "spend_on_buyin")  # spend 系は grant 不可
    with pytest.raises(InvalidReasonError):
        repo.grant_points(alice, 100, "bonus")
    with pytest.raises(InvalidAmountError):
        repo.grant_points(alice, 0, "manual_grant")
    with pytest.raises(InvalidAmountError):
        repo.grant_points(alice, -100, "manual_grant")
    with pytest.raises(UnknownPlayerError):
        repo.grant_points("deadbeef", 100, "manual_grant")


def test_adjust_points_both_directions(repo, players):
    alice = _pid(players, "Alice")
    repo.grant_points(alice, 1000, "campaign_grant")
    repo.adjust_points(alice, -300, note="誤付与の補正")
    repo.adjust_points(alice, 50)
    assert repo.point_balance(alice) == 750
    with pytest.raises(InvalidAmountError):
        repo.adjust_points(alice, 0)


# ――― 中間集計（業務ルール 7: 確定値ではないスナップショット） ―――

def test_session_totals_snapshot(repo, players, sessions):
    sid = _sid(sessions)
    alice, bob = _pid(players, "Alice"), _pid(players, "Bob")
    repo.grant_points(alice, 2000, "manual_grant")
    repo.add_entry(sid, alice, "entry_fee", cash_amount=1000)
    repo.add_entry(sid, alice, "buy_in", cash_amount=10000)
    repo.add_entry(sid, alice, "rebuy", cash_amount=8000, point_amount=2000)
    repo.add_entry(
        sid, alice, "order", cash_amount=1200,
        order={"item_name": "ジントニック", "unit_amount": 600, "quantity": 2},
    )
    repo.add_entry(sid, bob, "buy_in", cash_amount=5000)

    totals = repo.session_totals(sid)
    # buy-in 合計は cash+point 込み、entry_fee / adjustment は含めない
    assert totals[alice] == {"buy_in_total": 20000, "order_total": 1200}
    assert totals[bob] == {"buy_in_total": 5000, "order_total": 0}
    with pytest.raises(SessionNotFoundError):
        repo.session_totals("deadbeef")


# ――― persistence ―――

def test_persistence_roundtrip(tmp_path, players, sessions):
    db = tmp_path / "ledger.json"
    repo = LedgerRepository(path=db, session_repo=sessions, player_repo=players)
    alice = _pid(players, "Alice")
    repo.grant_points(alice, 5000, "manual_grant", idempotency_key="g1", note="初回")
    entry = repo.add_entry(
        _sid(sessions), alice, "buy_in", cash_amount=7000, point_amount=3000
    )

    # 再起動相当
    reloaded = LedgerRepository(path=db, session_repo=sessions, player_repo=players)
    assert [e.to_dict() for e in reloaded.list_entries()] == [entry.to_dict()]
    assert reloaded.point_balance(alice) == 2000
    grants = [e for e in reloaded.list_point_entries(alice) if e.delta_points > 0]
    assert grants[0].idempotency_key == "g1"

    raw = json.loads(db.read_text(encoding="utf-8"))
    assert set(raw) == {"ledger_entries", "point_ledger_entries"}


def test_entry_fee_rejects_points_error_type(repo, players, sessions):
    """entry_fee_requires_cash の error 型確認（残高系の回帰は test_point_ledger.py）。"""
    alice = _pid(players, "Alice")
    repo.grant_points(alice, 1000, "manual_grant")
    with pytest.raises(EntryFeeRequiresCashError):
        repo.add_entry(_sid(sessions), alice, "entry_fee", cash_amount=500, point_amount=500)
