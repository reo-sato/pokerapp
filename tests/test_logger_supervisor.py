"""店舗サーバーの見張り役（ADR-0073 段階 C, `core/logger_supervisor.py`）: iPad の「卓を始める」でロガーを子のプロセスとして
起動し、見張る。落ちたら、お知らせを足して live_hand を消す（セッションは閉じない）。"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from core import logger_status as ls
from core.logger_supervisor import CHILD_LOG, SETUP_FILE, LoggerBusy, LoggerSupervisor, TooSoon
from tools.session_setup_ui import SetupError

RING = {"mode": "ring", "seats": [1, 2, 3], "names": {"1": "太郎"}, "stacks": {}, "default_stack": 1000,
        "sb": 5, "bb": 10, "button": 3, "log_dir": "/etc"}


class _Popen:
    def __init__(self) -> None:
        self.calls: list = []

    def __call__(self, args, **kw):
        self.calls.append((args, kw))
        return type("P", (), {"pid": 4321, "poll": lambda self: None})()


def _sup(tmp_path: Path, popen=None, now=[1000.0]):
    return LoggerSupervisor(tmp_path / "logs", root=tmp_path, python="py", popen=popen or _Popen(),
                            clock=lambda: now[0], state_directory=ls.state_dir())


def test_start_validates_writes_the_setup_and_spawns_the_headless_logger(tmp_path):
    popen = _Popen()
    sup = _sup(tmp_path, popen)
    got = sup.start(dict(RING))
    assert got["pid"] == 4321
    (args, kw), = popen.calls
    setup = tmp_path / "logs" / SETUP_FILE
    assert args == ["py", str(tmp_path / "main.py"), "--headless", "--setup", str(setup.resolve())]
    assert kw["cwd"] == str(tmp_path) and kw["env"]["PYTHONUTF8"] == "1" and kw["stderr"] == subprocess.STDOUT
    assert "log_dir" not in json.loads(setup.read_text(encoding="utf-8"))          # ログのフォルダは受け取らない
    assert "iPad から起動" in (tmp_path / "logs" / CHILD_LOG).read_text(encoding="utf-8")


def test_a_bad_setup_returns_the_fields(tmp_path):
    popen = _Popen()
    with pytest.raises(SetupError) as e:
        _sup(tmp_path, popen).start(dict(RING, seats=[1]))
    assert any(err["field"] == "seats" for err in e.value.errors) and popen.calls == []


def test_a_running_logger_and_a_quick_second_start_are_refused(tmp_path):
    now = [1000.0]
    sup = _sup(tmp_path, now=now)
    sup.start(dict(RING))
    with pytest.raises(TooSoon):
        sup.start(dict(RING))
    now[0] += 30
    status = ls.LoggerStatus("cli")                   # PC で起動したロガーが動いている（このプロセスの pid）
    status.set(phase="running", session_id="sid")
    try:
        with pytest.raises(LoggerBusy):
            sup.start(dict(RING))
    finally:
        status.close()


def test_a_crashed_logger_is_cleaned_up_once(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    (logs / "sid1.live_hand.json").write_text(json.dumps({"session_id": "sid1", "hand": {"hand_id": 7}}),
                                              encoding="utf-8")
    state = {"phase": "running", "mode": "headless", "pid": dead.pid, "session_id": "sid1", "log_dir": str(logs),
             "updated_ts": 0}
    (ls.state_dir() / ls.STATE_FILE).write_text(json.dumps(state), encoding="utf-8")
    sup = _sup(tmp_path)
    record = sup.watch_once()
    assert record["hand_id"] == 7 and record["session_id"] == "sid1"
    assert not (logs / "sid1.live_hand.json").exists()
    texts = [r["text"] for r in ls.read_notices()["notices"]]
    assert any("ハンド 7 は記録の途中で止まりました" in t for t in texts)
    assert sup.watch_once() is None and sup.crash()["pid"] == dead.pid              # 同じ落ち方は 1 回だけ
    assert len(ls.read_notices()["notices"]) == len(texts)


class _FakeSupervisor:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.started: list = []

    def start(self, raw):
        if self.error is not None:
            raise self.error
        self.started.append(raw)
        return {"pid": 99, "started_at": "2026-11-20T19:00:00"}

    def crash(self):
        return {"pid": 5, "hand_id": 3}


def _client(tmp_path: Path, supervisor=None, writable=True):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from api.server import create_app
    from core.player_repository import PlayerRepository
    from core.session_repository import SessionRepository

    players = PlayerRepository(tmp_path / "players.json")
    app = create_app(players, SessionRepository(tmp_path / "sessions.json", player_repo=players), tmp_path,
                     staff_token="tok", orders_writable=writable, logger_supervisor=supervisor)
    return TestClient(app), {"Authorization": "Bearer tok"}


def test_the_start_api(tmp_path):
    sup = _FakeSupervisor()
    http, head = _client(tmp_path, sup)
    assert http.post("/api/staff/logger/start", json=RING).status_code == 401
    r = http.post("/api/staff/logger/start", json=RING, headers=head)
    assert r.status_code == 201 and r.json()["pid"] == 99 and sup.started == [RING]
    bad = _client(tmp_path, _FakeSupervisor(SetupError([{"field": "seats", "message": "使う席を 2 つ以上"}])))
    r = bad[0].post("/api/staff/logger/start", json=RING, headers=bad[1])
    assert r.status_code == 400 and r.json()["code"] == "invalid_setup" and r.json()["errors"][0]["field"] == "seats"
    busy = _client(tmp_path, _FakeSupervisor(LoggerBusy("端末（RDP）で起動したロガー（pid 1）")))
    r = busy[0].post("/api/staff/logger/start", json=RING, headers=busy[1])
    assert r.status_code == 409 and "端末（RDP）" in r.json()["message"]
    soon = _client(tmp_path, _FakeSupervisor(TooSoon("起動したばかりです")))
    assert soon[0].post("/api/staff/logger/start", json=RING, headers=soon[1]).status_code == 429
    none = _client(tmp_path, None)
    assert none[0].post("/api/staff/logger/start", json=RING, headers=none[1]).json()["code"] == "logger_start_unavailable"
    ro = _client(tmp_path, sup, writable=False)
    assert ro[0].post("/api/staff/logger/start", json=RING, headers=ro[1]).status_code == 503


def test_the_setup_api_gives_the_last_setup_and_structures(tmp_path):
    from tools.session_setup_ui import save_last_setup

    save_last_setup(tmp_path, dict(RING))
    http, head = _client(tmp_path, _FakeSupervisor())
    body = http.get("/api/staff/logger/setup", headers=head).json()
    assert body["last"]["seats"] == [1, 2, 3] and "log_dir" not in body["last"]
    assert isinstance(body["structures"], list) and body["can_start"] is True
    status = http.get("/api/staff/logger", headers=head).json()
    assert status["can_start"] is True and "crash" not in status          # 落ちていなければ出さない
