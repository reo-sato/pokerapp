"""tests/test_point_ledger.py

Phase S3: ISSUE-0001（point 残高の source of truth）の決着 = ADR-0013 を固定する回帰テスト。

ISSUE-0001 の Regression Test 節で予告されていた 4 本:
- test_balance_matches_fold_of_entries        … 残高 = entry 列の fold（A 案）
- test_insufficient_points_falls_back_to_cash … point 不足分は cash で補完（業務ルール 3）
- test_entry_fee_rejects_points               … entry fee は cash only（業務ルール 1）
- test_grant_idempotency                      … manual/campaign grant の重複防止

加えて ISSUE-0001 Reproduction の論点 3（同一 session 内 grant→spend 同居）を固定する。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core.ledger_repository import (
    DuplicateGrantError,
    EntryFeeRequiresCashError,
    InsufficientPointsError,
    LedgerRepository,
)
from core.player_repository import PlayerRepository
from core.session_repository import SessionRepository


@pytest.fixture
def players(tmp_path: Path) -> PlayerRepository:
    repo = PlayerRepository(path=tmp_path / "players.json")
    repo.create_player("Alice")
    return repo


@pytest.fixture
def sessions(tmp_path: Path, players: PlayerRepository) -> SessionRepository:
    repo = SessionRepository(path=tmp_path / "sessions.json", player_repo=players)
    repo.create_session()
    return repo


@pytest.fixture
def repo(
    tmp_path: Path, players: PlayerRepository, sessions: SessionRepository
) -> LedgerRepository:
    return LedgerRepository(
        path=tmp_path / "ledger.json", session_repo=sessions, player_repo=players
    )


def _alice(players: PlayerRepository) -> str:
    return players.list_players()[0].player_id


def _sid(sessions: SessionRepository) -> str:
    return sessions.list_sessions()[0].session_id


def test_balance_matches_fold_of_entries(repo, players, sessions):
    """残高は point_ledger_entry の fold と常に一致する（cached 残高は存在しない）。"""
    alice = _alice(players)
    assert repo.point_balance(alice) == 0

    repo.grant_points(alice, 5000, "manual_grant")
    repo.grant_points(alice, 2000, "campaign_grant")
    repo.add_entry(_sid(sessions), alice, "buy_in", cash_amount=4000, point_amount=3000)
    repo.adjust_points(alice, -500)

    fold = sum(e.delta_points for e in repo.list_point_entries(alice))
    assert repo.point_balance(alice) == fold == 3500


def test_insufficient_points_falls_back_to_cash(repo, players, sessions):
    """残高不足の strict reject + plan_payment による cash 補完（業務ルール 3）。"""
    alice = _alice(players)
    repo.grant_points(alice, 2000, "manual_grant")

    # strict API は不足を reject する（黙って残高を超えない）
    with pytest.raises(InsufficientPointsError):
        repo.add_entry(_sid(sessions), alice, "buy_in", cash_amount=0, point_amount=5000)

    # core が分割を計算: point は残高まで、不足分は cash
    cash, points = repo.plan_payment(alice, 5000)
    assert (cash, points) == (3000, 2000)
    entry = repo.add_entry(
        _sid(sessions), alice, "buy_in", cash_amount=cash, point_amount=points
    )
    assert (entry.cash_amount, entry.point_amount) == (3000, 2000)
    assert repo.point_balance(alice) == 0

    # 残高 0 になった後は全額 cash
    assert repo.plan_payment(alice, 1000) == (1000, 0)
    # use_points=False は残高があっても cash only
    repo.grant_points(alice, 100, "manual_grant")
    assert repo.plan_payment(alice, 1000, use_points=False) == (1000, 0)


def test_entry_fee_rejects_points(repo, players, sessions):
    """entry fee は cash only（業務ルール 1, error code: entry_fee_requires_cash）。"""
    alice = _alice(players)
    repo.grant_points(alice, 5000, "manual_grant")
    with pytest.raises(EntryFeeRequiresCashError):
        repo.add_entry(_sid(sessions), alice, "entry_fee", cash_amount=0, point_amount=1000)
    # 残高は減っていない（reject は mutate しない）
    assert repo.point_balance(alice) == 5000
    # cash only なら通る
    entry = repo.add_entry(_sid(sessions), alice, "entry_fee", cash_amount=1000)
    assert (entry.cash_amount, entry.point_amount) == (1000, 0)


def test_grant_idempotency(repo, players):
    """同一 idempotency_key の grant は重複記録されない（ADR-0013）。"""
    alice = _alice(players)
    repo.grant_points(alice, 3000, "campaign_grant", idempotency_key="camp-2026-06")
    with pytest.raises(DuplicateGrantError):
        repo.grant_points(alice, 3000, "campaign_grant", idempotency_key="camp-2026-06")
    assert repo.point_balance(alice) == 3000
    # キーなし grant は重複チェック対象外（手入力 2 回は別 entry）
    repo.grant_points(alice, 1000, "manual_grant")
    repo.grant_points(alice, 1000, "manual_grant")
    assert repo.point_balance(alice) == 5000


def test_grant_and_spend_in_same_session(repo, players, sessions):
    """ISSUE-0001 論点 3: 同一 session 内で result_credit → buy-in 充当が同居できる。"""
    alice = _alice(players)
    sid = _sid(sessions)
    repo.add_entry(sid, alice, "buy_in", cash_amount=10000)
    repo.grant_points(alice, 4000, "result_credit", idempotency_key=f"result:{sid}:{alice}")
    entry = repo.add_entry(sid, alice, "rebuy", cash_amount=6000, point_amount=4000)
    assert entry.point_amount == 4000
    assert repo.point_balance(alice) == 0
