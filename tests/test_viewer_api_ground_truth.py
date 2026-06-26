"""tests/test_viewer_api_ground_truth.py

ADR-0043: Phase A 計測 ground truth の staff API E2E。fastapi/httpx 未導入は skip。
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
from core.ground_truth_repository import GroundTruthRepository  # noqa: E402
from core.hand_correction_repository import HandCorrectionRepository  # noqa: E402
from core.ledger_repository import LedgerRepository  # noqa: E402
from core.player_repository import PlayerRepository  # noqa: E402
from core.session_repository import SessionRepository  # noqa: E402

_TOKEN = "staff-token"


def _make_hand(hand_id: int, winner_seat: int = 1, needs_review: bool = False) -> dict:
    return {
        "hand_id": hand_id,
        "session_id": "_placeholder",
        "winner_seat": winner_seat,
        "review_required": needs_review,
        "board": ["As", "Kc", "Qd", "5h", "2s"],
        "blinds": {"sb": 100, "bb": 200},
        "players": [
            {"seat": 1, "name": "Alice", "hole_cards": ["Ah", "Ad"], "result": 800},
            {"seat": 2, "name": "Bob", "hole_cards": None, "result": -800},
        ],
        "pot_total": 1600,
        "actions": [
            {"action": "raise", "amount": 200, "needs_review": needs_review,
             "confidence": 0.9, "seat": 1, "street": "preflop"},
            {"action": "call", "amount": 200, "needs_review": False,
             "confidence": 0.95, "seat": 2, "street": "preflop"},
        ],
    }


def _build(tmp_path: Path, *, needs_review: bool = False) -> dict:
    players = PlayerRepository(path=tmp_path / "players.json")
    alice = players.create_player("Alice")
    bob = players.create_player("Bob")
    sessions = SessionRepository(path=tmp_path / "sessions.json", player_repo=players)
    s = sessions.create_session(label="Fri")
    sessions.assign_seat(s.session_id, 1, 1, alice.player_id)
    sessions.assign_seat(s.session_id, 1, 2, bob.player_id)
    sessions.assign_seat(s.session_id, 2, 1, alice.player_id)
    sessions.assign_seat(s.session_id, 2, 2, bob.player_id)

    log_dir = tmp_path / "logs"
    log_dir.mkdir(exist_ok=True)
    h1 = _make_hand(1, winner_seat=1, needs_review=needs_review)
    h2 = _make_hand(2, winner_seat=2)
    (log_dir / f"{s.session_id}.json").write_text(
        json.dumps({"session_id": s.session_id, "hands": [h1, h2]}), encoding="utf-8"
    )

    ledger = LedgerRepository(path=tmp_path / "ledger.json", session_repo=sessions)
    corrections = HandCorrectionRepository(path=tmp_path / "hand_corrections.json")
    gt_repo = GroundTruthRepository(log_dir)
    app = create_app(
        players, sessions, log_dir, ledger_repo=ledger, orders_writable=True,
        staff_token=_TOKEN, correction_repo=corrections, ground_truth_repo=gt_repo,
    )
    http = TestClient(app)
    return {
        "http": http, "alice": alice, "bob": bob, "session": s,
        "log_dir": log_dir, "gt_repo": gt_repo,
    }


def test_measurement_rows_returns_winner_and_review_status(tmp_path: Path):
    env = _build(tmp_path, needs_review=True)
    staff = ViewerApiClient(client=env["http"], staff_token=_TOKEN)
    rows = staff.list_measurement_rows(env["session"].session_id)

    assert [r["hand_id"] for r in rows] == [1, 2]
    r1, r2 = rows
    assert r1["winner_seat"] == 1 and r1["winner_result"] == 800
    assert r1["review_required"] is True and r1["has_needs_review"] is True
    assert r1["ground_truth"] is None
    assert r2["winner_seat"] == 2 and r2["winner_result"] == -800
    assert r2["has_needs_review"] is False


def test_pass_through_writes_captured_as_gt(tmp_path: Path):
    env = _build(tmp_path)
    sid = env["session"].session_id
    staff = ViewerApiClient(client=env["http"], staff_token=_TOKEN)

    body = staff.pass_through_ground_truth(sid, 1, annotator="staff1")
    assert body["source"] == "captured-passthrough"
    assert body["annotator"] == "staff1"
    assert body["winner_seat"] == 1
    assert body["board"] == ["As", "Kc", "Qd", "5h", "2s"]

    rows = staff.list_measurement_rows(sid)
    assert rows[0]["ground_truth"]["source"] == "captured-passthrough"
    assert rows[0]["ground_truth"]["annotator"] == "staff1"

    gt_file = env["log_dir"] / f"{sid}.ground_truth.json"
    assert gt_file.exists()
    payload = json.loads(gt_file.read_text("utf-8"))
    assert payload["session_id"] == sid
    assert payload["hands"][0]["winner_seat"] == 1


def test_pass_through_rejected_for_needs_review_hand(tmp_path: Path):
    """ADR-0043 §3 C-2 ガード: needs_review を含むハンドは passthrough できない。"""
    env = _build(tmp_path, needs_review=True)
    staff = ViewerApiClient(client=env["http"], staff_token=_TOKEN)
    with pytest.raises(ViewerApiError) as ei:
        staff.pass_through_ground_truth(env["session"].session_id, 1)
    assert (ei.value.code, ei.value.status_code) == ("invalid_amount", 400)
    assert "needs_review" in ei.value.message


def test_pass_through_after_correction_clears_needs_review(tmp_path: Path):
    """訂正で needs_review が解除されたハンドは passthrough できる（C-2 が訂正適用後を見る）。"""
    env = _build(tmp_path, needs_review=True)
    sid = env["session"].session_id
    staff = ViewerApiClient(client=env["http"], staff_token=_TOKEN)

    # 訂正で action[0] を直す → needs_review=False になり review_required も解除される。
    staff.add_hand_correction(sid, 1, "action", "bet", action_index=0, note="誤認識")

    body = staff.pass_through_ground_truth(sid, 1, annotator="staff1")
    assert body["source"] == "captured-passthrough"
    # 訂正適用後の actions[0]=bet が GT に乗る。
    assert body["actions"][0]["action"] == "bet"


def test_manual_edit_overrides_captured(tmp_path: Path):
    env = _build(tmp_path)
    sid = env["session"].session_id
    staff = ViewerApiClient(client=env["http"], staff_token=_TOKEN)

    edited = {
        "hand_id": 1,
        "board": ["As", "Kc", "Qd", "5h", "3s"],  # river を訂正
        "actions": [
            {"street": "preflop", "seat": 1, "action": "raise", "amount": 300},
        ],
        "players": [{"seat": 1, "hole_cards": ["Ah", "Ad"], "showed_down": True}],
        "winner_seat": 1,
        "notes": "river 2s ではなく 3s だった",
    }
    body = staff.submit_ground_truth_edit(sid, 1, edited, annotator="staff2")
    assert body["source"] == "manual-edit"
    assert body["annotator"] == "staff2"
    assert body["board"] == ["As", "Kc", "Qd", "5h", "3s"]
    assert body["notes"] == "river 2s ではなく 3s だった"


def test_manual_edit_requires_hand_body(tmp_path: Path):
    env = _build(tmp_path)
    sid = env["session"].session_id
    staff = ViewerApiClient(client=env["http"], staff_token=_TOKEN)
    with pytest.raises(ViewerApiError) as ei:
        staff._request(  # type: ignore[attr-defined]
            "PUT", f"/api/staff/sessions/{sid}/ground-truth/1",
            json={"source": "manual-edit", "annotator": "x"},
            headers=staff._staff_headers(),  # type: ignore[attr-defined]
        )
    assert (ei.value.code, ei.value.status_code) == ("invalid_amount", 400)


def test_lwq_overwrite(tmp_path: Path):
    env = _build(tmp_path)
    sid = env["session"].session_id
    staff = ViewerApiClient(client=env["http"], staff_token=_TOKEN)

    staff.pass_through_ground_truth(sid, 1, annotator="staff1")
    edited = {"hand_id": 1, "board": [], "actions": [], "players": [], "winner_seat": 2}
    staff.submit_ground_truth_edit(sid, 1, edited, annotator="staff2")

    got = staff.get_ground_truth(sid, 1)
    assert got["source"] == "manual-edit"
    assert got["annotator"] == "staff2"
    assert got["winner_seat"] == 2


def test_get_ground_truth_not_found(tmp_path: Path):
    env = _build(tmp_path)
    staff = ViewerApiClient(client=env["http"], staff_token=_TOKEN)
    with pytest.raises(ViewerApiError) as ei:
        staff.get_ground_truth(env["session"].session_id, 1)
    assert (ei.value.code, ei.value.status_code) == ("not_found", 404)


def test_pass_through_unknown_hand_returns_404(tmp_path: Path):
    env = _build(tmp_path)
    staff = ViewerApiClient(client=env["http"], staff_token=_TOKEN)
    with pytest.raises(ViewerApiError) as ei:
        staff.pass_through_ground_truth(env["session"].session_id, 99)
    assert (ei.value.code, ei.value.status_code) == ("not_found", 404)


def test_unknown_source_rejected(tmp_path: Path):
    env = _build(tmp_path)
    sid = env["session"].session_id
    staff = ViewerApiClient(client=env["http"], staff_token=_TOKEN)
    with pytest.raises(ViewerApiError) as ei:
        staff._request(  # type: ignore[attr-defined]
            "PUT", f"/api/staff/sessions/{sid}/ground-truth/1",
            json={"source": "free-form", "annotator": "x"},
            headers=staff._staff_headers(),  # type: ignore[attr-defined]
        )
    assert (ei.value.code, ei.value.status_code) == ("invalid_amount", 400)


def test_requires_staff_token(tmp_path: Path):
    env = _build(tmp_path)
    sid = env["session"].session_id
    no_token = ViewerApiClient(client=env["http"])
    with pytest.raises(ViewerApiError) as ei:
        no_token.list_measurement_rows(sid)
    assert (ei.value.code, ei.value.status_code) == ("unauthorized", 401)
    with pytest.raises(ViewerApiError) as ei:
        no_token._request(  # type: ignore[attr-defined]
            "PUT", f"/api/staff/sessions/{sid}/ground-truth/1",
            json={"source": "captured-passthrough", "annotator": "x"},
        )
    assert (ei.value.code, ei.value.status_code) == ("unauthorized", 401)


def test_write_blocked_when_not_owner(tmp_path: Path):
    """orders_writable=False（単独 --viewer-api）は GT write を 503 で拒否する。"""
    players = PlayerRepository(path=tmp_path / "players.json")
    sessions = SessionRepository(path=tmp_path / "sessions.json", player_repo=players)
    s = sessions.create_session(label="Fri")
    log_dir = tmp_path / "logs"
    log_dir.mkdir(exist_ok=True)
    (log_dir / f"{s.session_id}.json").write_text(
        json.dumps({"session_id": s.session_id, "hands": [_make_hand(1)]}),
        encoding="utf-8",
    )
    app = create_app(
        players, sessions, log_dir, orders_writable=False, staff_token=_TOKEN,
    )
    http = TestClient(app)
    staff = ViewerApiClient(client=http, staff_token=_TOKEN)
    with pytest.raises(ViewerApiError) as ei:
        staff.pass_through_ground_truth(s.session_id, 1)
    assert (ei.value.code, ei.value.status_code) == ("orders_unavailable", 503)
    rows = staff.list_measurement_rows(s.session_id)
    assert len(rows) == 1
