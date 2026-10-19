"""tests/test_viewer_today_only.py

お客さんの画面に今日の分だけを出す（config `viewer_api.today_only`, 2026-10-19）: 名前の一覧とセッションの一覧は、
今日（営業日 = 朝 6 時から）始まったセッションの分だけ。スタッフの読み取りは変えない。
"""
from __future__ import annotations

import json
from datetime import datetime

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from api.read_models import business_day_start  # noqa: E402
from api.server import auth_kwargs_from_config, create_app  # noqa: E402
from core.player_repository import PlayerRepository  # noqa: E402
from core.session_repository import SessionRepository  # noqa: E402

NOW = datetime(2026, 10, 19, 21, 0)


def test_business_day_starts_at_six():
    assert business_day_start(NOW) == datetime(2026, 10, 19, 6, 0)
    # 深夜 0 時をまたいでも、朝 6 時までは前の日の営業日
    assert business_day_start(datetime(2026, 10, 20, 1, 30)) == datetime(2026, 10, 19, 6, 0)


def _setup(tmp_path):
    players = PlayerRepository(tmp_path / "players.json")
    sessions = SessionRepository(tmp_path / "sessions.json", player_repo=players)
    old_p, both_p, new_p = (players.create_player(n).player_id for n in ("昨日だけ", "両方", "今日だけ"))
    old = sessions.create_session(label="昨日").session_id
    new = sessions.create_session(label="今日").session_id
    sessions.assign_seat(old, 1, 1, old_p)
    sessions.assign_seat(old, 1, 2, both_p)
    sessions.assign_seat(new, 1, 1, both_p)
    sessions.assign_seat(new, 1, 2, new_p)
    # 始まった時刻を昨日の夜と今日の夜にする（ファイルを書き換えて読み直す）
    path = tmp_path / "sessions.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    text = json.dumps(data, ensure_ascii=False)
    for s in (data.get("sessions") or []):
        s["started_at"] = "2026-10-18T19:00:00" if s["session_id"] == old else "2026-10-19T19:00:00"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    assert text != json.dumps(data, ensure_ascii=False)
    sessions = SessionRepository(path, player_repo=players)
    return players, sessions, old, new, (old_p, both_p, new_p)


def _client(tmp_path, players, sessions, today_only: bool) -> TestClient:
    return TestClient(create_app(players, sessions, tmp_path, today_only=today_only, clock=lambda: NOW))


def test_only_today_players_and_sessions_are_listed(tmp_path):
    players, sessions, old, new, (old_p, both_p, new_p) = _setup(tmp_path)
    c = _client(tmp_path, players, sessions, True)
    names = [p["display_name"] for p in c.get("/api/players").json()["players"]]
    assert sorted(names) == ["両方", "今日だけ"]
    listed = [s["session_id"] for s in c.get(f"/api/players/{both_p}/sessions").json()["sessions"]]
    assert listed == [new]
    assert c.get(f"/api/players/{both_p}/sessions/{old}/hands").json() == {"hands": []}


def test_off_lists_everything(tmp_path):
    players, sessions, old, new, (_, both_p, _) = _setup(tmp_path)
    c = _client(tmp_path, players, sessions, False)
    assert len(c.get("/api/players").json()["players"]) == 3
    assert len(c.get(f"/api/players/{both_p}/sessions").json()["sessions"]) == 2


def test_config_key():
    assert auth_kwargs_from_config({})["today_only"] is False
    assert auth_kwargs_from_config({"today_only": True})["today_only"] is True
