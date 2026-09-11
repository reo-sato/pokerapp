"""tests/test_viewer_api_correction.py

ADR-0036 (B4): ハンド訂正の staff API + viewer オーバーレイ E2E。fastapi/httpx 未導入は skip。
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
from core.hand_correction_repository import HandCorrectionRepository  # noqa: E402
from core.ledger_repository import LedgerRepository  # noqa: E402
from core.player_repository import PlayerRepository  # noqa: E402
from core.session_repository import SessionRepository  # noqa: E402

_TOKEN = "staff-token"


def _build(tmp_path: Path) -> dict:
    players = PlayerRepository(path=tmp_path / "players.json")
    alice = players.create_player("Alice")
    sessions = SessionRepository(path=tmp_path / "sessions.json", player_repo=players)
    s = sessions.create_session(label="Fri")
    sessions.assign_seat(s.session_id, 1, 1, alice.player_id)

    log_dir = tmp_path / "logs"
    log_dir.mkdir(exist_ok=True)
    (log_dir / f"{s.session_id}.json").write_text(json.dumps({"hands": [{
        "hand_id": 1, "session_id": s.session_id, "winner_seat": 1, "review_required": True,
        "actions": [
            {"action": "check", "amount": 0, "needs_review": True, "confidence": 0.4, "seat": 1},
            {"action": "call", "amount": 200, "needs_review": False, "confidence": 0.9, "seat": 1},
        ],
    }]}), encoding="utf-8")

    ledger = LedgerRepository(path=tmp_path / "ledger.json", session_repo=sessions)
    corrections = HandCorrectionRepository(path=tmp_path / "hand_corrections.json")
    app = create_app(players, sessions, log_dir, ledger_repo=ledger, orders_writable=True,
                     staff_token=_TOKEN, correction_repo=corrections)
    http = TestClient(app)
    return {"http": http, "alice": alice, "session": s, "log_dir": log_dir}


def test_staff_correction_overlays_on_viewer(tmp_path: Path):
    env = _build(tmp_path)
    sid, alice = env["session"].session_id, env["alice"]
    staff = ViewerApiClient(client=env["http"], staff_token=_TOKEN)
    player = ViewerApiClient(client=env["http"])

    # 訂正前: action[0] は check / needs_review=True。
    before = player.get_hand(sid, 1)
    assert before["actions"][0]["action"] == "check"
    assert before["actions"][0]["needs_review"] is True

    # staff が action[0] を check → bet に訂正。
    c = staff.add_hand_correction(sid, 1, "action", "bet", action_index=0, note="誤認識")
    assert c["field"] == "action" and c["new_value"] == "bet"

    # 訂正後の viewer ビュー: bet / 元値保持 / needs_review 解除。
    after = player.get_hand(sid, 1)
    a0 = after["actions"][0]
    assert a0["action"] == "bet"
    assert a0["_original"]["action"] == "check"
    assert a0["needs_review"] is False
    assert after["review_required"] is False
    # player hands 一覧にもオーバーレイが乗る。
    hands = player.list_player_hands(alice.player_id, sid)
    assert hands[0]["actions"][0]["action"] == "bet"
    # 元 hand log ファイルは不変（append-only, 監査）。
    raw = json.loads((env["log_dir"] / f"{sid}.json").read_text())
    assert raw["hands"][0]["actions"][0]["action"] == "check"


def test_staff_correction_out_of_range(tmp_path: Path):
    env = _build(tmp_path)
    staff = ViewerApiClient(client=env["http"], staff_token=_TOKEN)
    with pytest.raises(ViewerApiError) as ei:
        staff.add_hand_correction(env["session"].session_id, 1, "action", "bet", action_index=9)
    assert (ei.value.code, ei.value.status_code) == ("invalid_correction", 400)


def test_staff_correction_unknown_hand(tmp_path: Path):
    env = _build(tmp_path)
    staff = ViewerApiClient(client=env["http"], staff_token=_TOKEN)
    with pytest.raises(ViewerApiError) as ei:
        staff.add_hand_correction(env["session"].session_id, 99, "winner_seat", 2)
    assert (ei.value.code, ei.value.status_code) == ("not_found", 404)


def test_correction_requires_staff_token(tmp_path: Path):
    env = _build(tmp_path)
    no_token = ViewerApiClient(client=env["http"])
    with pytest.raises(ViewerApiError) as ei:
        no_token.add_hand_correction(env["session"].session_id, 1, "action", "bet", action_index=0)
    assert (ei.value.code, ei.value.status_code) == ("unauthorized", 401)


def test_staff_correction_invalid_field(tmp_path: Path):
    env = _build(tmp_path)
    staff = ViewerApiClient(client=env["http"], staff_token=_TOKEN)
    with pytest.raises(ViewerApiError) as ei:
        staff.add_hand_correction(env["session"].session_id, 1, "bogus", "x", action_index=0)
    assert (ei.value.code, ei.value.status_code) == ("invalid_correction", 400)
