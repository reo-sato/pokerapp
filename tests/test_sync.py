"""tests/test_sync.py

Phase S5 (ADR-0022): state-based merge の純粋関数 + 収束性テスト。

各ストアの per-store ルール（players create-only union / ledger・point union /
order_request status 解決 / settlement 単調 / session closed>open + 入れ子 hands/seats union）と、
全体の **冪等 / 可換 / `merge(merge(A,B),B)==merge(A,B)`** 性質を検証する。最後に
file-level round-trip（2 つの divergent な store ディレクトリが双方向 merge で同一に収束）。
"""
from __future__ import annotations

import json
from pathlib import Path

from core.sync import (
    build_snapshot,
    merge_hand_logs,
    merge_ledger_entries,
    merge_order_requests,
    merge_players,
    merge_point_entries,
    merge_sessions,
    merge_settlements,
    merge_snapshot_into,
)

# ――― fixtures（小さいレコード列）―――


def _player(pid: str, name: str, created: str, updated: str | None = None) -> dict:
    return {
        "player_id": pid, "display_name": name, "created_at": created,
        "updated_at": updated if updated is not None else created,
    }


def _entry(eid: str, occurred: str, kind: str = "buy_in", cash: int = 100) -> dict:
    return {
        "entry_id": eid, "session_id": "s1", "player_id": "p1", "kind": kind,
        "occurred_at": occurred, "cash_amount": cash, "point_amount": 0,
    }


def _point(eid: str, occurred: str, delta: int = 10) -> dict:
    return {
        "entry_id": eid, "player_id": "p1", "delta_points": delta,
        "reason": "manual_grant", "occurred_at": occurred,
    }


def _order(rid: str, status: str = "pending", requested: str = "t0",
           resolved: str | None = None, ledger_entry_id: str | None = None) -> dict:
    d: dict = {
        "request_id": rid, "session_id": "s1", "player_id": "p1",
        "item_name": "beer", "quantity": 1, "status": status, "requested_at": requested,
    }
    if resolved is not None:
        d["resolved_at"] = resolved
    if ledger_entry_id is not None:
        d["ledger_entry_id"] = ledger_entry_id
    return d


def _settlement(sid: str, pid: str, settled: str | None = None,
                net: int = 100, paid: int = 0) -> dict:
    # payment_status は paid_amount / net から導出（core ADR-0023 と同一規則で自己整合に）。
    if net <= 0 or paid >= net:
        status = "paid"
    elif paid <= 0:
        status = "unpaid"
    else:
        status = "partial"
    return {
        "session_id": sid, "player_id": pid, "cash_in_total": net,
        "point_spent_total": 0, "order_total": 0, "entry_fee": 0,
        "point_credited_total": 0, "net_due_to_store": net,
        "paid_amount": paid, "payment_status": status, "settled_at": settled or "",
    }


def _seat(seat_no: int, pid: str) -> dict:
    return {"seat_no": seat_no, "player_id": pid}


def _session(sid: str, status: str = "open", started: str = "t0",
             ended: str | None = None, label: str | None = None,
             hands: dict | None = None) -> dict:
    d: dict = {"session_id": sid, "started_at": started, "status": status}
    if ended is not None:
        d["ended_at"] = ended
    if label is not None:
        d["label"] = label
    d["hands"] = hands or {}
    return d


# ――― players ―――


def test_players_rename_propagates_by_updated_at():
    # ADR-0032: rename は updated_at の later-wins（LWW）で伝播する。
    local = [_player("a", "Alice", "t1", "t1")]
    remote = [_player("a", "Alice-renamed", "t1", "t2"), _player("b", "Bob", "t2")]
    merged = merge_players(local, remote)
    by_id = {p["player_id"]: p for p in merged}
    assert by_id["a"]["display_name"] == "Alice-renamed"  # 新しい updated_at が勝つ
    assert by_id["a"]["updated_at"] == "t2"
    assert by_id["b"]["display_name"] == "Bob"  # 新規は追加
    assert [p["player_id"] for p in merged] == ["a", "b"]  # (created_at, player_id) 順
    # 逆向きでも同じ結果（可換）。
    rev = {p["player_id"]: p for p in merge_players(remote, local)}
    assert rev["a"]["display_name"] == "Alice-renamed"


def test_players_rename_older_does_not_overwrite_newer():
    # 古い updated_at は新しい表示名を上書きしない。
    local = [_player("a", "Alice-new", "t1", "t5")]
    remote = [_player("a", "Alice-old", "t1", "t2")]
    assert merge_players(local, remote)[0]["display_name"] == "Alice-new"
    assert merge_players(remote, local)[0]["display_name"] == "Alice-new"


def _merged_player(pid: str, name: str, created: str, into: str) -> dict:
    return {**_player(pid, name, created), "merged_into": into, "merged_at": "tm"}


def test_player_merge_propagates_monotonically():
    # ADR-0030 D5: 片方だけ merged なら merged 版が両方向で勝つ（収束）。
    unmerged = [_player("a", "A", "t1")]
    merged = [_merged_player("a", "A", "t1", "b")]
    fwd = {p["player_id"]: p for p in merge_players(unmerged, merged)}
    rev = {p["player_id"]: p for p in merge_players(merged, unmerged)}
    assert fwd["a"]["merged_into"] == "b"
    assert rev["a"]["merged_into"] == "b"


def test_player_merge_conflict_deterministic_tiebreak():
    # 両ノードが a を別 survivor に merge → survivor 最小で全ノード収束。
    into_c = [_merged_player("a", "A", "t1", "c")]
    into_b = [_merged_player("a", "A", "t1", "b")]
    fwd = {p["player_id"]: p for p in merge_players(into_c, into_b)}
    rev = {p["player_id"]: p for p in merge_players(into_b, into_c)}
    assert fwd["a"]["merged_into"] == "b"  # min("b","c")
    assert rev["a"]["merged_into"] == "b"  # 可換


# ――― ledger / point ―――


def test_ledger_union_conflict_free():
    local = [_entry("e1", "t1"), _entry("e2", "t2")]
    remote = [_entry("e2", "t2"), _entry("e3", "t3")]
    merged = merge_ledger_entries(local, remote)
    assert [e["entry_id"] for e in merged] == ["e1", "e2", "e3"]


def test_point_union_sorted_by_occurred():
    local = [_point("e2", "t2")]
    remote = [_point("e1", "t1")]
    merged = merge_point_entries(local, remote)
    assert [e["entry_id"] for e in merged] == ["e1", "e2"]


# ――― order_request status 解決 ―――


def test_order_pending_loses_to_confirmed():
    local = [_order("r1", "pending")]
    remote = [_order("r1", "confirmed", resolved="t5", ledger_entry_id="le1")]
    merged = merge_order_requests(local, remote)
    assert merged[0]["status"] == "confirmed"
    assert merged[0]["ledger_entry_id"] == "le1"


def test_order_confirmed_beats_rejected():
    local = [_order("r1", "rejected", resolved="t4")]
    remote = [_order("r1", "confirmed", resolved="t5", ledger_entry_id="le1")]
    merged = merge_order_requests(local, remote)
    assert merged[0]["status"] == "confirmed"
    # 可換
    assert merge_order_requests(remote, local)[0]["status"] == "confirmed"


def test_order_both_confirmed_earlier_resolved_wins():
    a = [_order("r1", "confirmed", resolved="t9", ledger_entry_id="late")]
    b = [_order("r1", "confirmed", resolved="t1", ledger_entry_id="early")]
    assert merge_order_requests(a, b)[0]["ledger_entry_id"] == "early"
    assert merge_order_requests(b, a)[0]["ledger_entry_id"] == "early"


# ――― settlement 単調 ―――


def test_settlement_committed_beats_uncommitted():
    local = [_settlement("s1", "p1")]  # uncommitted (settled_at="")
    remote = [_settlement("s1", "p1", settled="t5")]
    merged = merge_settlements(local, remote)
    assert merged[0]["settled_at"] == "t5"
    assert merge_settlements(remote, local)[0]["settled_at"] == "t5"


def test_settlement_max_paid_amount_wins():
    """両 committed は paid_amount の max を採り payment_status を導出する（ADR-0024）。"""
    local = [_settlement("s1", "p1", settled="t5", net=10000, paid=3000)]   # partial
    remote = [_settlement("s1", "p1", settled="t6", net=10000, paid=10000)]  # paid
    merged = merge_settlements(local, remote)[0]
    assert merged["paid_amount"] == 10000
    assert merged["payment_status"] == "paid"
    # 可換
    rev = merge_settlements(remote, local)[0]
    assert (rev["paid_amount"], rev["payment_status"]) == (10000, "paid")


def test_settlement_partial_not_overwritten_by_unpaid():
    """ADR-0024 の核心: partial（高い paid_amount）が、早い settled_at の unpaid に上書きされない。"""
    partial = [_settlement("s1", "p1", settled="t9", net=10000, paid=4000)]  # partial
    unpaid = [_settlement("s1", "p1", settled="t1", net=10000, paid=0)]      # unpaid, 早い
    for merged in (merge_settlements(partial, unpaid), merge_settlements(unpaid, partial)):
        assert merged[0]["paid_amount"] == 4000
        assert merged[0]["payment_status"] == "partial"


def test_settlement_merge_idempotent():
    rows = [_settlement("s1", "p1", settled="t1", net=10000, paid=4000)]
    once = merge_settlements(rows, rows)
    assert merge_settlements(once, rows) == once
    assert once[0] == rows[0]  # 自己整合な record は merge(A,A)=A


def test_settlement_both_committed_same_paid_keeps_earliest():
    local = [_settlement("s1", "p1", settled="t9", net=100, paid=0)]
    remote = [_settlement("s1", "p1", settled="t1", net=100, paid=0)]
    assert merge_settlements(local, remote)[0]["settled_at"] == "t1"
    assert merge_settlements(remote, local)[0]["settled_at"] == "t1"


# ――― session closed>open + 入れ子 ―――


def test_session_closed_beats_open():
    local = [_session("s1", "open", started="t0")]
    remote = [_session("s1", "closed", started="t0", ended="t9")]
    merged = merge_sessions(local, remote)
    assert merged[0]["status"] == "closed"
    assert merged[0]["ended_at"] == "t9"
    assert merge_sessions(remote, local)[0]["status"] == "closed"


def test_session_label_local_priority():
    local = [_session("s1", label="local-label")]
    remote = [_session("s1", label="remote-label")]
    assert merge_sessions(local, remote)[0]["label"] == "local-label"
    # peer のみにあれば採用
    local2 = [_session("s1")]
    remote2 = [_session("s1", label="remote-label")]
    assert merge_sessions(local2, remote2)[0]["label"] == "remote-label"


def test_session_hands_and_seats_union_local_priority():
    local = [_session("s1", hands={
        "1": {"started_at": "t1", "seats": [_seat(1, "pA")]},
    })]
    remote = [_session("s1", hands={
        "1": {"started_at": "t0", "seats": [_seat(1, "pB"), _seat(2, "pC")]},
        "2": {"started_at": "t2", "seats": [_seat(1, "pD")]},
    })]
    merged = merge_sessions(local, remote)[0]
    h1 = merged["hands"]["1"]
    assert h1["started_at"] == "t0"  # 早い方
    seats1 = {s["seat_no"]: s["player_id"] for s in h1["seats"]}
    assert seats1[1] == "pA"  # 同一 seat_no 衝突は local 優先
    assert seats1[2] == "pC"  # remote 由来の新席は追加
    assert "2" in merged["hands"]  # remote のみの hand も union


# ――― 全体 property: 冪等 / 可換 / merge(merge(A,B),B) ―――


def _norm_snapshot(snap: dict) -> dict:
    """snapshot を merge した結果を JSON 文字列化（順序込み等価判定用）。"""
    players = merge_players(snap["players"], [])
    sessions = merge_sessions(snap["sessions"], [])
    entries = merge_ledger_entries(snap["ledger_entries"], [])
    points = merge_point_entries(snap["point_entries"], [])
    settlements = merge_settlements(snap["settlements"], [])
    orders = merge_order_requests(snap["order_requests"], [])
    return {
        "players": players, "sessions": sessions, "ledger_entries": entries,
        "point_entries": points, "settlements": settlements, "order_requests": orders,
    }


def _merge_snaps(a: dict, b: dict) -> dict:
    return {
        "players": merge_players(a["players"], b["players"]),
        "sessions": merge_sessions(a["sessions"], b["sessions"]),
        "ledger_entries": merge_ledger_entries(a["ledger_entries"], b["ledger_entries"]),
        "point_entries": merge_point_entries(a["point_entries"], b["point_entries"]),
        "settlements": merge_settlements(a["settlements"], b["settlements"]),
        "order_requests": merge_order_requests(a["order_requests"], b["order_requests"]),
    }


def _snap_A() -> dict:
    return {
        "players": [_player("a", "Alice", "t1")],
        "sessions": [_session("s1", "open", started="t0",
                              hands={"1": {"started_at": "t1", "seats": [_seat(1, "a")]}})],
        "ledger_entries": [_entry("e1", "t1")],
        "point_entries": [_point("pe1", "t1")],
        "settlements": [_settlement("s1", "a")],
        "order_requests": [_order("r1", "pending")],
    }


def _snap_B() -> dict:
    # 注: player "a" は rename しない。rename 非伝播（local 優先, ADR-0022）は意図的な非収束
    # caveat なので、収束 property テストでは conflicting rename を入れない（別途
    # test_players_create_only_union_keeps_local_rename が rename 非伝播を検証する）。
    return {
        "players": [_player("a", "Alice", "t1"), _player("b", "Bob", "t2")],
        "sessions": [_session("s1", "closed", started="t0", ended="t9",
                              hands={"1": {"started_at": "t0", "seats": [_seat(2, "b")]},
                                     "2": {"started_at": "t2", "seats": [_seat(1, "b")]}})],
        "ledger_entries": [_entry("e2", "t2")],
        "point_entries": [_point("pe2", "t2")],
        "settlements": [_settlement("s1", "a", settled="t5", net=100, paid=100)],
        "order_requests": [_order("r1", "confirmed", resolved="t5", ledger_entry_id="le1"),
                           _order("r2", "pending")],
    }


def test_idempotent_merge_A_A():
    a = _snap_A()
    assert _merge_snaps(a, a) == _norm_snapshot(a)


def test_merge_merge_AB_B_equals_merge_AB():
    a, b = _snap_A(), _snap_B()
    ab = _merge_snaps(a, b)
    assert _merge_snaps(ab, b) == ab


def test_commutative_up_to_ordering():
    a, b = _snap_A(), _snap_B()
    ab = _merge_snaps(a, b)
    ba = _merge_snaps(b, a)
    assert ab == ba


# ――― file-level round-trip 収束 ―――


def _write_stores(d: Path, snap: dict) -> dict:
    """snapshot を 4 ストアファイルとして書き、paths dict を返す。"""
    d.mkdir(parents=True, exist_ok=True)
    paths = {
        "players_path": d / "players.json",
        "sessions_path": d / "sessions.json",
        "ledger_path": d / "ledger.json",
        "orders_path": d / "order_requests.json",
    }
    paths["players_path"].write_text(
        json.dumps({"players": snap["players"]}, ensure_ascii=False), encoding="utf-8")
    paths["sessions_path"].write_text(
        json.dumps({"sessions": snap["sessions"]}, ensure_ascii=False), encoding="utf-8")
    paths["ledger_path"].write_text(json.dumps({
        "schema_version": "0.1",
        "ledger_entries": snap["ledger_entries"],
        "point_ledger_entries": snap["point_entries"],
        "settlements": snap["settlements"],
    }, ensure_ascii=False), encoding="utf-8")
    paths["orders_path"].write_text(
        json.dumps({"requests": snap["order_requests"]}, ensure_ascii=False), encoding="utf-8")
    return paths


def _read_all(paths: dict) -> dict:
    return {k: json.loads(Path(v).read_text(encoding="utf-8")) for k, v in paths.items()}


def test_file_level_roundtrip_converges(tmp_path: Path):
    dir_a = tmp_path / "nodeA"
    dir_b = tmp_path / "nodeB"
    paths_a = _write_stores(dir_a, _snap_A())
    paths_b = _write_stores(dir_b, _snap_B())

    snap_a = build_snapshot(**paths_a)
    snap_b = build_snapshot(**paths_b)

    # 双方向 merge
    merge_snapshot_into(**paths_a, peer=snap_b)
    merge_snapshot_into(**paths_b, peer=snap_a)

    after_a = _read_all(paths_a)
    after_b = _read_all(paths_b)
    assert after_a == after_b, "両ノードが同一の merged 状態に収束していない"

    # 既存 top-level 構造の保持
    assert "schema_version" in after_a["ledger_path"]
    assert set(after_a["ledger_path"]) >= {
        "ledger_entries", "point_ledger_entries", "settlements"}
    assert "requests" in after_a["orders_path"]
    assert "players" in after_a["players_path"]
    assert "sessions" in after_a["sessions_path"]


def test_repeated_merge_is_stable(tmp_path: Path):
    dir_a = tmp_path / "nodeA"
    dir_b = tmp_path / "nodeB"
    paths_a = _write_stores(dir_a, _snap_A())
    paths_b = _write_stores(dir_b, _snap_B())
    snap_b = build_snapshot(**paths_b)
    merge_snapshot_into(**paths_a, peer=snap_b)
    first = _read_all(paths_a)
    # 同じ peer snapshot を再度 merge → 不変（冪等）
    summary = merge_snapshot_into(**paths_a, peer=snap_b)
    assert _read_all(paths_a) == first
    assert all(v == 0 for v in summary.values()), "再 merge で new-or-changed は 0 のはず"


# ――― hand logs（file-level union, ADR-0032）―――


def _hand(hid: int) -> dict:
    return {"hand_id": hid, "players": []}


def test_merge_hand_logs_union_by_hand_id():
    local = {"s1": {"session_id": "s1", "hands": [_hand(1), _hand(2)]}}
    remote = {"s1": {"session_id": "s1", "hands": [_hand(2), _hand(3)]},
              "s2": {"session_id": "s2", "hands": [_hand(1)]}}
    merged = merge_hand_logs(local, remote)
    assert [h["hand_id"] for h in merged["s1"]["hands"]] == [1, 2, 3]  # union, 昇順
    assert "s2" in merged  # remote のみの session も union
    # 可換・冪等
    rev = merge_hand_logs(remote, local)
    assert rev["s1"]["hands"] == merged["s1"]["hands"]
    assert merge_hand_logs(merged, merged) == merged


def test_hand_logs_propagate_through_snapshot(tmp_path: Path):
    # node A は s1 の hand1、node B は s1 の hand2 + s2 を持つ → 双方向 sync で両者が全 hand に収束。
    def _stores(d: Path) -> dict:
        d.mkdir(parents=True, exist_ok=True)
        (d / "players.json").write_text(json.dumps({"players": []}), encoding="utf-8")
        (d / "sessions.json").write_text(json.dumps({"sessions": []}), encoding="utf-8")
        (d / "ledger.json").write_text(json.dumps({}), encoding="utf-8")
        (d / "orders.json").write_text(json.dumps({"requests": []}), encoding="utf-8")
        logs = d / "logs"
        logs.mkdir(exist_ok=True)
        return {
            "players_path": d / "players.json", "sessions_path": d / "sessions.json",
            "ledger_path": d / "ledger.json", "orders_path": d / "orders.json",
            "log_dir": logs,
        }

    a = _stores(tmp_path / "A")
    b = _stores(tmp_path / "B")
    (a["log_dir"] / "s1.json").write_text(
        json.dumps({"session_id": "s1", "hands": [_hand(1)]}), encoding="utf-8")
    (b["log_dir"] / "s1.json").write_text(
        json.dumps({"session_id": "s1", "hands": [_hand(2)]}), encoding="utf-8")
    (b["log_dir"] / "s2.json").write_text(
        json.dumps({"session_id": "s2", "hands": [_hand(1)]}), encoding="utf-8")

    snap_b = build_snapshot(**b)
    summary = merge_snapshot_into(**a, peer=snap_b)
    assert summary["hand_logs_added"] == 2  # s1 の hand2 + s2 の hand1

    s1 = json.loads((a["log_dir"] / "s1.json").read_text())
    assert [h["hand_id"] for h in s1["hands"]] == [1, 2]
    assert (a["log_dir"] / "s2.json").exists()


def test_order_merge_cancelled_rank(tmp_path):
    """ADR-0045: cancelled の衝突解決 = confirmed > rejected > cancelled > pending。可換。"""
    from core.sync import merge_order_requests

    def req(status: str, **extra) -> dict:
        return {
            "request_id": "a" * 32, "session_id": "s" * 32, "player_id": "p" * 32,
            "item_name": "ビール", "quantity": 1, "status": status,
            "requested_at": "2026-07-12T20:00:00", **extra,
        }

    cancelled = req("cancelled", resolved_at="2026-07-12T20:01:00")
    confirmed = req("confirmed", resolved_at="2026-07-12T20:02:00",
                    ledger_entry_id="e" * 32)
    rejected = req("rejected", resolved_at="2026-07-12T20:03:00")
    pending = req("pending")

    # player キャンセル × スタッフ確定 → 確定が勝つ（会計影響を優先, 可換）。
    assert merge_order_requests([cancelled], [confirmed])[0]["status"] == "confirmed"
    assert merge_order_requests([confirmed], [cancelled])[0]["status"] == "confirmed"
    # cancelled × rejected → rejected（決定性のためスタッフ操作を上位）。
    assert merge_order_requests([cancelled], [rejected])[0]["status"] == "rejected"
    assert merge_order_requests([rejected], [cancelled])[0]["status"] == "rejected"
    # pending × cancelled → cancelled（終端が勝つ）。
    assert merge_order_requests([pending], [cancelled])[0]["status"] == "cancelled"
    assert merge_order_requests([cancelled], [pending])[0]["status"] == "cancelled"
