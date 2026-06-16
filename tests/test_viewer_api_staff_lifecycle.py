"""tests/test_viewer_api_staff_lifecycle.py

ADR-0038 §A/§B — staff API 拡張（会計 reversal / point grant + session/座席/player
ライフサイクル）の viewer API ↔ Python staff client round-trip test。

`api/server.py` の新 `/api/staff/...` エンドポイントを staff token 付きで in-process
（TestClient）round-trip し、happy path と error code（error-shapes.md の session / ledger /
player セクションと 1:1）、認可（401/403/503）を検証する。fastapi / httpx 未導入は skip。
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from api.client import ViewerApiClient, ViewerApiError  # noqa: E402
from api.server import create_app  # noqa: E402
from core.ledger_repository import LedgerRepository  # noqa: E402
from core.order_request_repository import OrderRequestRepository  # noqa: E402
from core.player_repository import PlayerRepository  # noqa: E402
from core.session_repository import SessionRepository  # noqa: E402

_TOKEN = "s3cr3t-staff-token"


def _build(tmp_path: Path, *, orders_writable: bool = True, staff_token: str = _TOKEN) -> dict:
    players = PlayerRepository(path=tmp_path / "players.json")
    sessions = SessionRepository(path=tmp_path / "sessions.json", player_repo=players)
    ledger = LedgerRepository(path=tmp_path / "ledger.json", session_repo=sessions,
                              player_repo=players)
    orders = OrderRequestRepository(path=tmp_path / "order_requests.json",
                                    session_repo=sessions, player_repo=players)
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    app = create_app(players, sessions, log_dir, ledger_repo=ledger, order_repo=orders,
                     orders_writable=orders_writable, staff_token=staff_token,
                     buyin_presets=[10000])
    http = TestClient(app)
    return {
        "staff": ViewerApiClient(client=http, staff_token=_TOKEN),
        "players": players, "sessions": sessions, "ledger": ledger,
    }


@pytest.fixture
def env(tmp_path: Path):
    e = _build(tmp_path)
    yield e
    e["staff"].close()


# ――― §A: reversal / point grant ―――

def test_reverse_entry(env: dict):
    staff, sessions = env["staff"], env["sessions"]
    alice = env["players"].create_player("Alice")
    s = sessions.create_session(label="F1")
    entry = staff.add_ledger_entry(s.session_id, alice.player_id, "buy_in", cash_amount=10000)
    spec = staff.compute_settlement(s.session_id)
    assert next(r for r in spec if r["player_id"] == alice.player_id)["net_due_to_store"] == 10000

    # reversal UI 用の entry 一覧 read
    entries = staff.list_session_ledger_entries(s.session_id)
    assert [e["entry_id"] for e in entries] == [entry["entry_id"]]

    rev = staff.reverse_entry(entry["entry_id"])
    assert rev["reverses_entry_id"] == entry["entry_id"]
    spec = staff.compute_settlement(s.session_id)
    assert next(r for r in spec if r["player_id"] == alice.player_id)["net_due_to_store"] == 0
    # 一覧に reversal が増える
    entries = staff.list_session_ledger_entries(s.session_id)
    assert len(entries) == 2 and any(e.get("reverses_entry_id") == entry["entry_id"] for e in entries)

    with pytest.raises(ViewerApiError) as ei:
        staff.reverse_entry("nonexistent")
    assert ei.value.code == "not_found"


def test_grant_points(env: dict):
    staff, ledger = env["staff"], env["ledger"]
    alice = env["players"].create_player("Alice")
    staff.grant_points(alice.player_id, 500)
    assert ledger.point_balance(alice.player_id) == 500

    # idempotency_key 重複 → duplicate_grant
    staff.grant_points(alice.player_id, 100, idempotency_key="k1")
    with pytest.raises(ViewerApiError) as ei:
        staff.grant_points(alice.player_id, 100, idempotency_key="k1")
    assert ei.value.code == "duplicate_grant"

    # unknown player → unknown_player
    with pytest.raises(ViewerApiError) as ei:
        staff.grant_points("f" * 32, 100)
    assert ei.value.code == "unknown_player"

    # 非正 → invalid_amount
    with pytest.raises(ViewerApiError) as ei:
        staff.grant_points(alice.player_id, 0)
    assert ei.value.code == "invalid_amount"


# ――― §B: session lifecycle ―――

def test_session_lifecycle(env: dict):
    staff = env["staff"]
    assert staff.list_sessions() == []
    created = staff.create_session(label="土曜 #1", blinds={"sb": 100, "bb": 200})
    assert created["status"] == "open" and created["label"] == "土曜 #1"
    assert len(created["session_id"]) == 32

    listed = staff.list_sessions()
    assert [s["session_id"] for s in listed] == [created["session_id"]]

    closed = staff.close_session(created["session_id"])
    assert closed["status"] == "closed" and closed["ended_at"]

    with pytest.raises(ViewerApiError) as ei:
        staff.close_session(created["session_id"])
    assert ei.value.code == "already_closed"

    with pytest.raises(ViewerApiError) as ei:
        staff.close_session("f" * 32)
    assert ei.value.code == "not_found"


# ――― §B: player lifecycle ―――

def test_player_lifecycle(env: dict):
    staff = env["staff"]
    alice = staff.create_player("Alice")
    assert alice["display_name"] == "Alice"
    assert [p["player_id"] for p in staff.staff_list_players()] == [alice["player_id"]]

    renamed = staff.rename_player(alice["player_id"], "Alice2")
    assert renamed["display_name"] == "Alice2"

    with pytest.raises(ViewerApiError) as ei:
        staff.create_player("   ")
    assert ei.value.code == "empty_display_name"

    staff.create_player("Bob")
    with pytest.raises(ViewerApiError) as ei:
        staff.create_player("Bob")
    assert ei.value.code == "duplicate_display_name"

    with pytest.raises(ViewerApiError) as ei:
        staff.rename_player("f" * 32, "X")
    assert ei.value.code == "not_found"


# ――― §B: seat assignment（batch）―――

def test_seat_assignment_and_errors(env: dict):
    staff = env["staff"]
    alice = staff.create_player("Alice")
    bob = staff.create_player("Bob")
    s = staff.create_session(label="F")
    sid = s["session_id"]

    res = staff.assign_seats(sid, 1, [
        {"seat_no": 1, "player_id": alice["player_id"]},
        {"seat_no": 2, "player_id": bob["player_id"]},
    ])
    assert [r["seat_no"] for r in res] == [1, 2]

    seating = staff.get_seating(sid)
    assert {sa["player_id"] for sa in seating["seating"]} == {alice["player_id"], bob["player_id"]}
    assert seating["hand_ids"] == [1]

    # seat_taken（同一 hand で席重複）
    with pytest.raises(ViewerApiError) as ei:
        staff.assign_seats(sid, 2, [
            {"seat_no": 1, "player_id": alice["player_id"]},
            {"seat_no": 1, "player_id": bob["player_id"]},
        ])
    assert ei.value.code == "seat_taken"

    # player_already_seated
    with pytest.raises(ViewerApiError) as ei:
        staff.assign_seats(sid, 3, [
            {"seat_no": 1, "player_id": alice["player_id"]},
            {"seat_no": 2, "player_id": alice["player_id"]},
        ])
    assert ei.value.code == "player_already_seated"

    # invalid_seat（範囲外）
    with pytest.raises(ViewerApiError) as ei:
        staff.assign_seats(sid, 4, [{"seat_no": 99, "player_id": alice["player_id"]}])
    assert ei.value.code == "invalid_seat"

    # unknown_player
    with pytest.raises(ViewerApiError) as ei:
        staff.assign_seats(sid, 5, [{"seat_no": 1, "player_id": "f" * 32}])
    assert ei.value.code == "unknown_player"

    # closed session → session_closed
    staff.close_session(sid)
    with pytest.raises(ViewerApiError) as ei:
        staff.assign_seats(sid, 6, [{"seat_no": 1, "player_id": alice["player_id"]}])
    assert ei.value.code == "session_closed"


# ――― §C: hand logger 遠隔制御（control queue）―――

def test_hand_control_appends_commands(env: dict, tmp_path: Path):
    from core.control_queue import ControlCommandLog

    staff = env["staff"]
    s = staff.create_session(label="Control")
    sid = s["session_id"]

    cmd = staff.send_control(sid, "new_hand")
    assert cmd["type"] == "new_hand" and cmd["command_id"]
    staff.send_control(sid, "winner", seat=3)
    staff.send_control(sid, "rebuy", seat=1, amount=5000)

    # control queue ファイルに 3 件 append されている（hand logger が tail する対象）。
    log = ControlCommandLog(tmp_path / "logs" / f"{sid}.control.jsonl")
    cmds, _ = log.read_from(0)
    assert [c.type for c in cmds] == ["new_hand", "winner", "rebuy"]
    assert cmds[1].args == {"seat": 3}
    assert cmds[2].args == {"seat": 1, "amount": 5000}

    # 不正 control は invalid_control
    with pytest.raises(ViewerApiError) as ei:
        staff.send_control(sid, "winner")  # seat 無し
    assert ei.value.code == "invalid_control"
    with pytest.raises(ViewerApiError) as ei:
        staff.send_control(sid, "nope")
    assert ei.value.code == "invalid_control"


# ――― 認可 ―――

def test_authz_for_new_endpoints(tmp_path: Path):
    e = _build(tmp_path)
    try:
        http = e["staff"]._client
        no_token = ViewerApiClient(client=http)
        with pytest.raises(ViewerApiError) as ei:
            no_token.list_sessions()
        assert (ei.value.code, ei.value.status_code) == ("unauthorized", 401)
    finally:
        e["staff"].close()

    ro = _build(tmp_path / "ro", orders_writable=False)
    try:
        staff = ro["staff"]
        # read は通る
        assert staff.list_sessions() == []
        # write（session 作成）は 503
        with pytest.raises(ViewerApiError) as ei:
            staff.create_session(label="x")
        assert (ei.value.code, ei.value.status_code) == ("orders_unavailable", 503)
    finally:
        ro["staff"].close()
