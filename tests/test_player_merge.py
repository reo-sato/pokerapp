"""tests/test_player_merge.py

ADR-0030: player merge（alias/tombstone + read-time canonicalization）の core テスト。

registry の merge/resolve/equivalence/unmerge と、player_id をキーにする read
（settlement 集計 / point 残高 / order フィルタ / viewer read）が survivor 視点で
合算・突合されることを検証する。歴史的レコードは書き換えないこと（append-only）も確認する。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core.ledger_repository import LedgerRepository
from core.order_request_repository import OrderRequestRepository
from core.player_repository import (
    PlayerMergeError,
    PlayerNotFoundError,
    PlayerRepository,
)
from core.session_repository import SessionRepository


def _registry(tmp_path: Path) -> PlayerRepository:
    return PlayerRepository(path=tmp_path / "players.json")


# ――― registry: merge / resolve / equivalence / unmerge ―――

def test_merge_sets_tombstone_and_resolves(tmp_path: Path):
    repo = _registry(tmp_path)
    survivor = repo.create_player("Alice (venue)")
    absorbed = repo.create_player("Alice (cloud)")
    repo.merge_players(survivor.player_id, absorbed.player_id)

    assert repo.get(absorbed.player_id).is_merged
    assert repo.resolve_canonical(absorbed.player_id) == survivor.player_id
    assert repo.resolve_canonical(survivor.player_id) == survivor.player_id
    assert repo.equivalence_class(survivor.player_id) == {survivor.player_id, absorbed.player_id}


def test_list_players_hides_tombstone(tmp_path: Path):
    repo = _registry(tmp_path)
    survivor = repo.create_player("Alice")
    absorbed = repo.create_player("Alice2")
    repo.merge_players(survivor.player_id, absorbed.player_id)
    ids = {p.player_id for p in repo.list_players()}
    assert ids == {survivor.player_id}
    ids_all = {p.player_id for p in repo.list_players(include_merged=True)}
    assert ids_all == {survivor.player_id, absorbed.player_id}


def test_merge_is_idempotent(tmp_path: Path):
    repo = _registry(tmp_path)
    s = repo.create_player("S")
    a = repo.create_player("A")
    repo.merge_players(s.player_id, a.player_id)
    first = repo.get(a.player_id).merged_at
    again = repo.merge_players(s.player_id, a.player_id)
    assert again.merged_at == first  # 再 merge で更新されない


def test_merge_chain_resolves_to_terminal(tmp_path: Path):
    repo = _registry(tmp_path)
    a = repo.create_player("A")
    b = repo.create_player("B")
    c = repo.create_player("C")
    repo.merge_players(b.player_id, a.player_id)  # a -> b
    repo.merge_players(c.player_id, b.player_id)  # b -> c
    assert repo.resolve_canonical(a.player_id) == c.player_id
    assert repo.equivalence_class(c.player_id) == {a.player_id, b.player_id, c.player_id}


def test_merge_into_survivors_canonical(tmp_path: Path):
    # survivor 自身が tombstone のときは canonical を実 survivor にする。
    repo = _registry(tmp_path)
    a = repo.create_player("A")
    b = repo.create_player("B")
    c = repo.create_player("C")
    repo.merge_players(c.player_id, b.player_id)  # b -> c
    repo.merge_players(b.player_id, a.player_id)  # a -> (canonical of b) = c
    assert repo.resolve_canonical(a.player_id) == c.player_id


def test_self_merge_and_cycle_rejected(tmp_path: Path):
    repo = _registry(tmp_path)
    a = repo.create_player("A")
    b = repo.create_player("B")
    with pytest.raises(PlayerMergeError):
        repo.merge_players(a.player_id, a.player_id)
    repo.merge_players(b.player_id, a.player_id)  # a -> b
    with pytest.raises(PlayerMergeError):
        repo.merge_players(a.player_id, b.player_id)  # b -> a だと a->b->a のサイクル


def test_merge_unknown_player(tmp_path: Path):
    repo = _registry(tmp_path)
    a = repo.create_player("A")
    with pytest.raises(PlayerNotFoundError):
        repo.merge_players(a.player_id, "f" * 32)
    with pytest.raises(PlayerNotFoundError):
        repo.merge_players("f" * 32, a.player_id)


def test_unmerge_is_reversible(tmp_path: Path):
    repo = _registry(tmp_path)
    s = repo.create_player("S")
    a = repo.create_player("A")
    repo.merge_players(s.player_id, a.player_id)
    repo.unmerge(a.player_id)
    assert not repo.get(a.player_id).is_merged
    assert repo.resolve_canonical(a.player_id) == a.player_id
    assert {p.player_id for p in repo.list_players()} == {s.player_id, a.player_id}


def test_merge_persists_across_reload(tmp_path: Path):
    repo = _registry(tmp_path)
    s = repo.create_player("S")
    a = repo.create_player("A")
    repo.merge_players(s.player_id, a.player_id)
    reopened = PlayerRepository(path=tmp_path / "players.json")
    assert reopened.resolve_canonical(a.player_id) == s.player_id


# ――― cross-repo read canonicalization ―――

def _world(tmp_path: Path):
    players = PlayerRepository(path=tmp_path / "players.json")
    sessions = SessionRepository(path=tmp_path / "sessions.json", player_repo=players)
    ledger = LedgerRepository(path=tmp_path / "ledger.json",
                              session_repo=sessions, player_repo=players)
    return players, sessions, ledger


def test_settlement_aggregates_across_merge(tmp_path: Path):
    players, sessions, ledger = _world(tmp_path)
    venue = players.create_player("Bob (venue)")
    cloud = players.create_player("Bob (cloud)")
    s = sessions.create_session()
    # 同一 session で 2 つの ID に buy_in が記帳された後で merge する。
    ledger.add_entry(s.session_id, venue.player_id, "buy_in", cash_amount=10000)
    ledger.add_entry(s.session_id, cloud.player_id, "buy_in", cash_amount=5000)

    before = ledger.compute_settlement(s.session_id)
    assert len(before) == 2  # merge 前は 2 行

    players.merge_players(venue.player_id, cloud.player_id)
    after = ledger.compute_settlement(s.session_id)
    assert len(after) == 1  # merge 後は survivor 1 行に合算
    row = after[0]
    assert row.player_id == venue.player_id
    assert row.cash_in_total == 15000
    # 歴史的 entry は書き換えられていない（append-only, ADR-0016/0030 D1）。
    raw_ids = {e.player_id for e in ledger.list_entries(s.session_id)}
    assert raw_ids == {venue.player_id, cloud.player_id}


def test_point_balance_aggregates_across_merge(tmp_path: Path):
    players, sessions, ledger = _world(tmp_path)
    venue = players.create_player("V")
    cloud = players.create_player("C")
    ledger.grant_points(venue.player_id, 300, "manual_grant", idempotency_key="g1")
    ledger.grant_points(cloud.player_id, 200, "manual_grant", idempotency_key="g2")
    players.merge_players(venue.player_id, cloud.player_id)
    assert ledger.point_balance(venue.player_id) == 500
    assert ledger.point_balance(cloud.player_id) == 500  # absorbed でも survivor 合算


def test_list_entries_filter_spans_merge(tmp_path: Path):
    players, sessions, ledger = _world(tmp_path)
    venue = players.create_player("V")
    cloud = players.create_player("C")
    s = sessions.create_session()
    ledger.add_entry(s.session_id, venue.player_id, "buy_in", cash_amount=10000)
    ledger.add_entry(s.session_id, cloud.player_id, "rebuy", cash_amount=5000)
    players.merge_players(venue.player_id, cloud.player_id)
    entries = ledger.list_entries(s.session_id, venue.player_id)
    assert len(entries) == 2  # survivor query が両 ID の entry を拾う


def test_order_list_filter_spans_merge(tmp_path: Path):
    import json
    players = PlayerRepository(path=tmp_path / "players.json")
    sessions = SessionRepository(path=tmp_path / "sessions.json", player_repo=players)
    venue = players.create_player("V")
    cloud = players.create_player("C")
    s = sessions.create_session()
    (tmp_path / "menu.json").write_text(json.dumps(
        {"items": [{"item_name": "beer", "unit_amount": 700}]}), encoding="utf-8")
    orders = OrderRequestRepository(path=tmp_path / "orders.json",
                                    session_repo=sessions, player_repo=players)
    orders.create_request(s.session_id, venue.player_id, "beer", 1)
    orders.create_request(s.session_id, cloud.player_id, "beer", 2)
    players.merge_players(venue.player_id, cloud.player_id)
    mine = orders.list_requests(s.session_id, player_id=venue.player_id)
    assert len(mine) == 2
