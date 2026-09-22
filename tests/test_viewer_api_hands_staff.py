"""tests/test_viewer_api_hands_staff.py

ADR-0044: staff hands read（GET /api/staff/sessions/{sid}/hands）の round-trip test。

- staff token で session の全 hand が hand_id 昇順で返る（seat 縛りなし）
- 訂正オーバーレイ（ADR-0036）が適用済みで返る
- log 不在 session は空 list（gracefully-empty）
- 認可（no token / wrong token → 401）

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
from core.hand_correction_repository import HandCorrectionRepository  # noqa: E402
from core.ledger_repository import LedgerRepository  # noqa: E402
from core.player_repository import PlayerRepository  # noqa: E402
from core.session_repository import SessionRepository  # noqa: E402

_TOKEN = "staff-token"


def _make_hand(hand_id: int, session_id: str) -> dict:
    return {
        "hand_id": hand_id,
        "session_id": session_id,
        "started_at": "2026-07-12T20:00:00",
        "ended_at": "2026-07-12T20:03:00",
        "winner_seat": 1,
        "board": ["As", "Kc", "Qd"],
        "blinds": {"sb": 100, "bb": 200},
        "players": [
            {"seat": 1, "name": "Alice", "hole_cards": ["Ah", "Ad"],
             "stack_start": 10000, "stack_end": 10800, "result": 800},
            {"seat": 2, "name": "Bob", "hole_cards": None,
             "stack_start": 10000, "stack_end": 9200, "result": -800},
        ],
        "pot_total": 1600,
        "actions": [
            {"action": "raise", "amount": 200, "needs_review": False,
             "confidence": 0.9, "seat": 1, "street": "preflop", "pot_after": 300},
            {"action": "call", "amount": 200, "needs_review": False,
             "confidence": 0.95, "seat": 2, "street": "preflop", "pot_after": 500},
        ],
    }


def _build(tmp_path: Path) -> dict:
    players = PlayerRepository(path=tmp_path / "players.json")
    alice = players.create_player("Alice")
    sessions = SessionRepository(path=tmp_path / "sessions.json", player_repo=players)
    s = sessions.create_session(label="Sat")
    sessions.assign_seat(s.session_id, 1, 1, alice.player_id)

    log_dir = tmp_path / "logs"
    log_dir.mkdir(exist_ok=True)
    # hand_id を書き込み順で逆順にし、応答の昇順ソートを検証する。
    hands = [_make_hand(2, s.session_id), _make_hand(1, s.session_id)]
    (log_dir / f"{s.session_id}.json").write_text(
        json.dumps({"session_id": s.session_id, "hands": hands}), encoding="utf-8"
    )

    ledger = LedgerRepository(path=tmp_path / "ledger.json", session_repo=sessions)
    corrections = HandCorrectionRepository(path=tmp_path / "hand_corrections.json")
    app = create_app(
        players, sessions, log_dir, ledger_repo=ledger, orders_writable=True,
        staff_token=_TOKEN, correction_repo=corrections,
    )
    http = TestClient(app)
    return {
        "http": http, "session": s, "corrections": corrections, "log_dir": log_dir,
    }


def test_staff_hands_returns_all_hands_sorted(tmp_path: Path):
    env = _build(tmp_path)
    staff = ViewerApiClient(client=env["http"], staff_token=_TOKEN)

    hands = staff.list_session_hands(env["session"].session_id)

    assert [h["hand_id"] for h in hands] == [1, 2]
    # seat 縛りなし（player read と違い、seat assignment の無い hand 2 も返る）。
    assert hands[1]["players"][0]["name"] == "Alice"
    assert hands[0]["board"] == ["As", "Kc", "Qd"]


def test_staff_hands_applies_correction_overlay(tmp_path: Path):
    env = _build(tmp_path)
    sid = env["session"].session_id
    env["corrections"].add_correction(
        sid, 1, "action", "bet", action_index=0, corrected_by="staff1"
    )
    staff = ViewerApiClient(client=env["http"], staff_token=_TOKEN)

    hands = staff.list_session_hands(sid)

    h1 = hands[0]
    assert h1["actions"][0]["action"] == "bet"
    assert h1["actions"][0]["corrected"] is True
    assert h1["actions"][0]["_original"]["action"] == "raise"
    # hand 2 は無訂正のまま。
    assert "corrected" not in hands[1]["actions"][0]


def test_staff_hands_unknown_session_is_empty(tmp_path: Path):
    env = _build(tmp_path)
    staff = ViewerApiClient(client=env["http"], staff_token=_TOKEN)

    assert staff.list_session_hands("f" * 32) == []


def test_staff_hands_requires_token(tmp_path: Path):
    env = _build(tmp_path)
    sid = env["session"].session_id

    no_token = ViewerApiClient(client=env["http"])
    with pytest.raises(ViewerApiError) as exc:
        no_token.list_session_hands(sid)
    assert exc.value.code == "unauthorized"
    assert exc.value.status_code == 401

    wrong = ViewerApiClient(client=env["http"], staff_token="wrong")
    with pytest.raises(ViewerApiError) as exc2:
        wrong.list_session_hands(sid)
    assert exc2.value.code == "unauthorized"
