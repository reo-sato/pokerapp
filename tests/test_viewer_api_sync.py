"""tests/test_viewer_api_sync.py

Phase S5 (ADR-0022): 双方向 sync の 2 ノード round-trip test。

2 つの独立した app インスタンス（別 tmp dir / 別 repo セット、同じ staff_token）に divergent な
データを入れ、`ViewerApiClient.sync_bidirectional` で収束させる。両ノードが read endpoint 経由で
union を返すことを確認する。auth（no token → 401 / read-only process の merge → 503）も検証する。

fastapi / httpx 未導入環境では skip。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from api.client import ViewerApiClient, ViewerApiError  # noqa: E402
from api.server import create_app  # noqa: E402
from core.ledger_repository import LedgerRepository  # noqa: E402
from core.menu import MenuMaster  # noqa: E402
from core.order_request_repository import OrderRequestRepository  # noqa: E402
from core.player_repository import PlayerRepository  # noqa: E402
from core.session_repository import SessionRepository  # noqa: E402

_TOKEN = "s3cr3t-staff-token"
_SID = "session-shared-0001"


def _write_menu(tmp_path: Path) -> Path:
    p = tmp_path / "menu.json"
    p.write_text(json.dumps({"items": [{"item_name": "ビール", "unit_amount": 700}]},
                            ensure_ascii=False), encoding="utf-8")
    return p


def _seed_session(sessions: SessionRepository, sid: str) -> None:
    """両ノードが同じ session_id を持つよう、sessions.json を直接書いて reload する。"""
    sessions.path.write_text(json.dumps({"sessions": [
        {"session_id": sid, "started_at": "2026-06-13T10:00:00",
         "status": "open", "label": "Friday", "hands": {}},
    ]}, ensure_ascii=False), encoding="utf-8")
    sessions.reload()


def _build_node(tmp_path: Path, *, orders_writable: bool = True,
                staff_token: str | None = _TOKEN) -> dict:
    tmp_path.mkdir(parents=True, exist_ok=True)
    players = PlayerRepository(path=tmp_path / "players.json")
    sessions = SessionRepository(path=tmp_path / "sessions.json", player_repo=players)
    _seed_session(sessions, _SID)
    log_dir = tmp_path / "logs"
    log_dir.mkdir(exist_ok=True)
    ledger = LedgerRepository(path=tmp_path / "ledger.json", session_repo=sessions)
    menu = MenuMaster(path=_write_menu(tmp_path))
    orders = OrderRequestRepository(path=tmp_path / "order_requests.json", session_repo=sessions)
    app = create_app(players, sessions, log_dir, ledger_repo=ledger,
                     order_repo=orders, menu=menu, orders_writable=orders_writable,
                     staff_token=staff_token)
    http = TestClient(app)
    return {
        "players": players, "sessions": sessions, "ledger": ledger, "orders": orders,
        "http": http,
        "client": ViewerApiClient(client=http, staff_token=staff_token),
    }


def test_bidirectional_sync_converges(tmp_path: Path):
    a = _build_node(tmp_path / "A")
    b = _build_node(tmp_path / "B")

    # ノード A: Alice を作って buy_in + pending 注文。
    alice = a["players"].create_player("Alice")
    a["ledger"].add_entry(_SID, alice.player_id, "buy_in", cash_amount=10000)
    a["orders"].create_request(_SID, alice.player_id, "ビール", 2)

    # ノード B: Bob を作って別の ledger entry + 確定済み注文（ledger order entry 込み）。
    bob = b["players"].create_player("Bob")
    b["ledger"].add_entry(_SID, bob.player_id, "rebuy", cash_amount=5000)
    req_b = b["orders"].create_request(_SID, bob.player_id, "ビール", 1)
    b["orders"].confirm_request(req_b.request_id, 700, b["ledger"])

    # 双方向 sync。
    result = a["client"].sync_bidirectional(b["client"])
    assert "into_self" in result and "into_peer" in result
    # A は B 由来の player(1) + ledger(rebuy + order = 2) を取り込む。
    assert result["into_self"]["players_added"] == 1
    assert result["into_self"]["ledger_entries_added"] == 2

    # 収束確認: 両ノードの player 一覧が union（Alice + Bob）。
    a_players = {p["player_id"] for p in a["client"].list_players()}
    b_players = {p["player_id"] for p in b["client"].list_players()}
    assert a_players == b_players == {alice.player_id, bob.player_id}

    # 収束確認: 両ノードで Alice / Bob の session が見える（read model は seat_assignment 起点だが
    # ここでは ledger / order の union を直接読む）。
    a_ledger = {e.entry_id for e in a["ledger"].list_entries(_SID)}
    b_ledger = {e.entry_id for e in b["ledger"].list_entries(_SID)}
    assert a_ledger == b_ledger
    # buy_in + rebuy + order = 3 entries。
    assert len(a_ledger) == 3

    # 注文 queue も union（A の pending + B の confirmed）。
    a_orders = a["client"].list_session_order_requests(_SID)
    b_orders = b["client"].list_session_order_requests(_SID)
    assert {r["request_id"] for r in a_orders} == {r["request_id"] for r in b_orders}
    assert {r["status"] for r in a_orders} == {"pending", "confirmed"}

    a["client"].close()
    b["client"].close()


def test_sync_snapshot_and_merge_require_token(tmp_path: Path):
    a = _build_node(tmp_path / "A")
    no_token = ViewerApiClient(client=a["http"])  # token なし
    with pytest.raises(ViewerApiError) as ei:
        no_token.pull_sync_snapshot()
    assert (ei.value.code, ei.value.status_code) == ("unauthorized", 401)
    with pytest.raises(ViewerApiError) as ei2:
        no_token.push_sync_merge({"players": []})
    assert (ei2.value.code, ei2.value.status_code) == ("unauthorized", 401)
    a["client"].close()
    no_token.close()


def test_readonly_process_rejects_merge(tmp_path: Path):
    # read-only（orders_writable=False）: snapshot read は可、merge write は 503。
    a = _build_node(tmp_path / "A", orders_writable=False)
    client = a["client"]
    snap = client.pull_sync_snapshot()  # read OK
    assert "players" in snap
    with pytest.raises(ViewerApiError) as ei:
        client.push_sync_merge(snap)
    assert (ei.value.code, ei.value.status_code) == ("orders_unavailable", 503)
    client.close()
