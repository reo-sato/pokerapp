"""core/sync.py

Phase S5 (ADR-0022): 双方向 sync の state-based merge（純粋関数）。

各ストアのレコード列（dict）に対して動作する **純粋・決定的** なマージ関数群を提供する。
リポジトリには触らず（in-memory list/dict のみ）単体テスト可能にしてある。マージは
ADR-0022 §Decision.2 の per-store ルールに従い、UUID union + 単調フィールド解決により

- **冪等** ``merge(A, A) == A`` / ``merge(merge(A, B), B) == merge(A, B)``
- **可換**（順序非依存の等価）``merge(A, B) ≅ merge(B, A)``

を満たす。これにより複数ノードがどの順で何度マージしても同じ状態に収束する。

ここでは wall-clock を一切使わず、レコード自身のタイムスタンプ（``occurred_at`` /
``created_at`` / ``resolved_at`` / ``settled_at`` / ``started_at``）だけで解決する。

`build_snapshot` / `merge_snapshot_into` はファイル I/O 境界（各ストア JSON を読み、
ピア snapshot を上記関数で merge し、アトミックに書き戻す）。各ファイルの既存 top-level 構造
（``{"players": [...]}`` / ``{"sessions": [...]}`` / ledger の3キー / ``{"requests": [...]}``）を
保つ。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

# ――― 終端 status の優先度（order_request）―――
# pending < {confirmed, rejected}。両終端で異なれば confirmed を優先（会計影響あり）。
_ORDER_STATUS_RANK = {"pending": 0, "rejected": 1, "confirmed": 2}


def _index_by(records: list[dict], key: str) -> dict:
    """key フィールドで dict 索引を作る（後勝ちではなく呼び出し側で衝突を解決する）。"""
    return {r[key]: r for r in records}


# ――― players（key player_id, create-only union, local 優先）―――


def merge_players(local: list[dict], remote: list[dict]) -> list[dict]:
    """player を player_id で union する（create-only）。

    既存 id は **local** レコードを保持（rename は v1 では伝播しない, ADR-0022）。新規 remote id
    のみ追加する。決定的順序: ``(created_at, player_id)``。
    """
    merged = dict(_index_by(local, "player_id"))
    for r in remote:
        pid = r["player_id"]
        if pid not in merged:
            merged[pid] = r
    return sorted(merged.values(), key=lambda p: (p.get("created_at", ""), p["player_id"]))


# ――― ledger / point entries（key entry_id, 衝突なし union）―――


def _merge_entries(local: list[dict], remote: list[dict]) -> list[dict]:
    merged = dict(_index_by(local, "entry_id"))
    for r in remote:
        eid = r["entry_id"]
        if eid not in merged:
            merged[eid] = r
    return sorted(
        merged.values(),
        key=lambda e: (e.get("occurred_at", ""), e["entry_id"]),
    )


def merge_ledger_entries(local: list[dict], remote: list[dict]) -> list[dict]:
    """ledger_entry を entry_id で union する（append-only ⇒ 同一 id は同一内容, 衝突なし）。

    決定的順序: ``(occurred_at, entry_id)``。
    """
    return _merge_entries(local, remote)


def merge_point_entries(local: list[dict], remote: list[dict]) -> list[dict]:
    """point_ledger_entry を entry_id で union する（append-only, 衝突なし）。

    決定的順序: ``(occurred_at, entry_id)``。
    """
    return _merge_entries(local, remote)


# ――― order_requests（key request_id, union + status 解決）―――


def _resolve_order(a: dict, b: dict) -> dict:
    """同一 request_id の 2 レコードを ADR-0022 の status 解決ルールで 1 つに畳む。

    pending < {confirmed, rejected}。一方が終端・他方 pending → 終端（resolved_at /
    ledger_entry_id ごと）。両終端で status が異なる（confirmed vs rejected）→ confirmed 優先。
    両 confirmed → resolved_at の早い方。可換になるよう順序非依存に決める。
    """
    ra = _ORDER_STATUS_RANK.get(a.get("status", "pending"), 0)
    rb = _ORDER_STATUS_RANK.get(b.get("status", "pending"), 0)
    if ra != rb:
        # rank の高い方（confirmed > rejected > pending）を採用。
        return a if ra > rb else b
    # 同 rank。pending 同士 → どちらでも同等（local=a を採用）。
    if a.get("status") == "confirmed" and b.get("status") == "confirmed":
        # 両 confirmed → resolved_at の早い方（同値なら request_id 比較で決定的に）。
        ka = (a.get("resolved_at") or "", a.get("request_id", ""))
        kb = (b.get("resolved_at") or "", b.get("request_id", ""))
        return a if ka <= kb else b
    return a


def merge_order_requests(local: list[dict], remote: list[dict]) -> list[dict]:
    """order_request を request_id で union + status 解決する（ADR-0022）。

    決定的順序: ``(requested_at, request_id)``。
    """
    by_id: dict[str, dict] = {}
    for r in local:
        by_id[r["request_id"]] = r
    for r in remote:
        rid = r["request_id"]
        if rid in by_id:
            by_id[rid] = _resolve_order(by_id[rid], r)
        else:
            by_id[rid] = r
    return sorted(
        by_id.values(),
        key=lambda r: (r.get("requested_at", ""), r["request_id"]),
    )


# ――― settlements（key (session_id, player_id), 単調解決）―――


def _settlement_committed(s: dict) -> bool:
    return bool(s.get("settled_at"))


def _resolve_settlement(a: dict, b: dict) -> dict:
    """同一 (session_id, player_id) の settlement を単調ルールで畳む（ADR-0022）。

    committed（settled_at 非 null）が uncommitted に勝つ。両 committed なら paid が unpaid に
    勝つ（支払いは取り消されない単調性）、settled_at は早い方、金額等は採用側の値を保持。
    可換になるよう順序非依存に決める。
    """
    ca, cb = _settlement_committed(a), _settlement_committed(b)
    if ca != cb:
        return a if ca else b
    if not ca:
        # 両方 uncommitted → どちらでも同等（local=a）。
        return a
    # 両方 committed。
    pa = a.get("payment_status") == "paid"
    pb = b.get("payment_status") == "paid"
    if pa != pb:
        return a if pa else b
    # payment_status が同じ → settled_at の早い方（同値なら local=a）。
    sa = a.get("settled_at") or ""
    sb = b.get("settled_at") or ""
    if sa != sb:
        return a if sa < sb else b
    return a


def merge_settlements(local: list[dict], remote: list[dict]) -> list[dict]:
    """settlement を (session_id, player_id) で union + 単調解決する（ADR-0022）。

    決定的順序: ``(session_id, player_id)``。
    """
    def _key(s: dict) -> tuple[str, str]:
        return (s["session_id"], s["player_id"])

    by_key: dict[tuple[str, str], dict] = {}
    for s in local:
        by_key[_key(s)] = s
    for s in remote:
        k = _key(s)
        if k in by_key:
            by_key[k] = _resolve_settlement(by_key[k], s)
        else:
            by_key[k] = s
    return sorted(by_key.values(), key=_key)


# ――― sessions（key session_id, 入れ子 hands/seats union）―――


def _merge_seats(local: list[dict], remote: list[dict]) -> list[dict]:
    """ある hand の seats（key seat_no）を union する。同一 seat_no の衝突は local 優先。

    決定的順序: ``seat_no``。
    """
    by_seat: dict[int, dict] = {}
    for s in local:
        by_seat[s["seat_no"]] = s
    for s in remote:
        if s["seat_no"] not in by_seat:
            by_seat[s["seat_no"]] = s
    return sorted(by_seat.values(), key=lambda s: s["seat_no"])


def _merge_hands(local: dict, remote: dict) -> dict:
    """入れ子 hands（key hand_id）を union する。各 hand は started_at（早い方）+ seats union。

    キーは sessions.json の形に合わせ文字列（hand_id の str）。
    """
    out: dict = {}
    for hand_id in set(local) | set(remote):
        lh = local.get(hand_id)
        rh = remote.get(hand_id)
        if lh is None:
            out[hand_id] = rh
            continue
        if rh is None:
            out[hand_id] = lh
            continue
        # 両方にある → started_at は早い方、seats は union（local 優先）。
        l_started = lh.get("started_at", "")
        r_started = rh.get("started_at", "")
        started = min(l_started, r_started) if (l_started and r_started) else (l_started or r_started)
        out[hand_id] = {
            "started_at": started,
            "seats": _merge_seats(lh.get("seats", []), rh.get("seats", [])),
        }
    return out


def _resolve_session(a: dict, b: dict) -> dict:
    """同一 session_id の session を ADR-0022 のルールで畳む（local=a 優先, 単調 status）。

    status は closed が open に勝つ。ended_at は closed 側。label/blinds は local 優先
    （peer のみにあれば採用）。入れ子 hands は union。
    """
    a_closed = a.get("status") == "closed"
    b_closed = b.get("status") == "closed"

    merged: dict = {
        "session_id": a["session_id"],
        "started_at": min(a.get("started_at", ""), b.get("started_at", ""))
        if (a.get("started_at") and b.get("started_at"))
        else (a.get("started_at") or b.get("started_at", "")),
    }

    if a_closed or b_closed:
        merged["status"] = "closed"
        # ended_at は closed 側から採用（両 closed なら local=a 優先）。
        if a_closed and a.get("ended_at") is not None:
            merged["ended_at"] = a["ended_at"]
        elif b_closed and b.get("ended_at") is not None:
            merged["ended_at"] = b["ended_at"]
    else:
        merged["status"] = "open"

    # label/blinds は local 優先、無ければ peer。
    for field in ("label", "blinds"):
        if a.get(field) is not None:
            merged[field] = a[field]
        elif b.get(field) is not None:
            merged[field] = b[field]

    merged["hands"] = _merge_hands(a.get("hands", {}) or {}, b.get("hands", {}) or {})
    return merged


def merge_sessions(local: list[dict], remote: list[dict]) -> list[dict]:
    """session を session_id で union + per-session merge する（ADR-0022）。

    入れ子 hands / seats も union（seat 衝突は local 優先）。決定的順序: ``session_id``。
    各 session dict は full nested 形（``hands`` を常に含む）。
    """
    by_id: dict[str, dict] = {}
    for s in local:
        s = dict(s)
        s.setdefault("hands", {})
        by_id[s["session_id"]] = s
    for s in remote:
        sid = s["session_id"]
        s = dict(s)
        s.setdefault("hands", {})
        if sid in by_id:
            by_id[sid] = _resolve_session(by_id[sid], s)
        else:
            by_id[sid] = s
    return sorted(by_id.values(), key=lambda s: s["session_id"])


# ――― snapshot I/O 境界 ―――


def _read_json(path: str | Path) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        with p.open(encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _atomic_write(path: str | Path, data: dict) -> None:
    """各 repository と同じアトミックリネーム（tmp + os.replace）で書き込む。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def build_snapshot(
    *,
    players_path: str | Path,
    sessions_path: str | Path,
    ledger_path: str | Path,
    orders_path: str | Path,
) -> dict:
    """このノードの全ストアを読み、ピアに渡す snapshot dict を作る（missing → 空 list）。

    session レコードは full nested 形（``hands`` 込み）のまま入れる。
    """
    players = _read_json(players_path).get("players", [])
    sessions = _read_json(sessions_path).get("sessions", [])
    ledger = _read_json(ledger_path)
    orders = _read_json(orders_path).get("requests", [])
    return {
        "players": list(players),
        "sessions": list(sessions),
        "ledger_entries": list(ledger.get("ledger_entries", [])),
        "point_entries": list(ledger.get("point_ledger_entries", [])),
        "settlements": list(ledger.get("settlements", [])),
        "order_requests": list(orders),
    }


def _count_changed(before: list[dict], after: list[dict], key) -> int:
    """after に新規追加 or 内容変更されたレコード数（new-or-changed）。"""
    before_map = {key(r): r for r in before}
    changed = 0
    for r in after:
        k = key(r)
        if k not in before_map or before_map[k] != r:
            changed += 1
    return changed


def merge_snapshot_into(
    *,
    players_path: str | Path,
    sessions_path: str | Path,
    ledger_path: str | Path,
    orders_path: str | Path,
    peer: dict,
) -> dict:
    """ピア snapshot をこのノードの各ストアファイルに merge し、アトミックに書き戻す。

    各ファイルの既存 top-level 構造を保つ（players/sessions/ledger 3 キー/requests）。
    返り値は new-or-changed のカウント summary。
    """
    # players
    local_players_doc = _read_json(players_path)
    local_players = local_players_doc.get("players", [])
    merged_players = merge_players(local_players, peer.get("players", []))
    players_added = _count_changed(local_players, merged_players, lambda r: r["player_id"])

    # sessions
    local_sessions_doc = _read_json(sessions_path)
    local_sessions = local_sessions_doc.get("sessions", [])
    merged_sessions = merge_sessions(local_sessions, peer.get("sessions", []))
    sessions_added = _count_changed(local_sessions, merged_sessions, lambda r: r["session_id"])

    # ledger（3 キーをまとめて 1 ファイルに）
    local_ledger_doc = _read_json(ledger_path)
    local_entries = local_ledger_doc.get("ledger_entries", [])
    local_points = local_ledger_doc.get("point_ledger_entries", [])
    local_settlements = local_ledger_doc.get("settlements", [])
    merged_entries = merge_ledger_entries(local_entries, peer.get("ledger_entries", []))
    merged_points = merge_point_entries(local_points, peer.get("point_entries", []))
    merged_settlements = merge_settlements(local_settlements, peer.get("settlements", []))
    entries_added = _count_changed(local_entries, merged_entries, lambda r: r["entry_id"])
    points_added = _count_changed(local_points, merged_points, lambda r: r["entry_id"])
    settlements_added = _count_changed(
        local_settlements, merged_settlements, lambda r: (r["session_id"], r["player_id"])
    )

    # orders
    local_orders_doc = _read_json(orders_path)
    local_orders = local_orders_doc.get("requests", [])
    merged_orders = merge_order_requests(local_orders, peer.get("order_requests", []))
    orders_added = _count_changed(local_orders, merged_orders, lambda r: r["request_id"])

    # ――― 書き戻し（既存 top-level 構造を保つ）―――
    new_players_doc = dict(local_players_doc)
    new_players_doc["players"] = merged_players
    _atomic_write(players_path, new_players_doc)

    new_sessions_doc = dict(local_sessions_doc)
    new_sessions_doc["sessions"] = merged_sessions
    _atomic_write(sessions_path, new_sessions_doc)

    new_ledger_doc = dict(local_ledger_doc)
    new_ledger_doc.setdefault("schema_version", "0.1")
    new_ledger_doc["ledger_entries"] = merged_entries
    new_ledger_doc["point_ledger_entries"] = merged_points
    new_ledger_doc["settlements"] = merged_settlements
    _atomic_write(ledger_path, new_ledger_doc)

    new_orders_doc = dict(local_orders_doc)
    new_orders_doc["requests"] = merged_orders
    _atomic_write(orders_path, new_orders_doc)

    return {
        "players_added": players_added,
        "sessions_added": sessions_added,
        "ledger_entries_added": entries_added,
        "point_entries_added": points_added,
        "settlements_added": settlements_added,
        "order_requests_added": orders_added,
    }
