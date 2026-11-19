"""画面なしのロガー（`main.py --headless --setup`, ADR-0073 段階 B / D6）。

店舗サーバーが iPad の操作で起動する形。標準入力を使わず、卓の設定は設定画面と同じ形の JSON で受け取り、iPad の
制御で操作し、「終了」（stop の制御）で端末の q と同じ手順で終わる。
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

import core.player_repository as player_repository
import core.session_repository as session_repository
import main
from core import logger_status as ls
from core.control_queue import ControlCommandLog

pytest.importorskip("pokerkit")


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(player_repository, "_DEFAULT_PLAYER_DB", tmp_path / "players.json")
    monkeypatch.setattr(session_repository, "_DEFAULT_SESSION_DB", tmp_path / "sessions.json")
    monkeypatch.chdir(tmp_path)                       # 卓の設定の log_dir 既定 ./logs をここに
    monkeypatch.setattr("core.config.load_config", lambda path=None: {
        "audio": {"enabled": False}, "rfid": {"enabled": False}, "table_state": {"enabled": False},
        "session_layer": {"enabled": True}})
    monkeypatch.setattr(sys, "stdin", None)          # pythonw の子と同じ（標準入力が無い）
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("画面なしなのに入力を待った"))
    return tmp_path


def _setup(path: Path, **over) -> Path:
    raw = {"mode": "ring", "seats": [1, 2, 3], "names": {"1": "太郎", "2": "花子"}, "stacks": {},
           "default_stack": 1000, "sb": 5, "bb": 10, "button": 3, "log_dir": "/etc", **over}
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return path


def _wait(cond, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while not cond() and time.time() < deadline:
        time.sleep(0.02)
    assert cond()


def test_headless_runs_from_the_setup_and_stops_from_the_ipad(data_dir: Path):
    setup = _setup(data_dir / "setup.json")
    runner = threading.Thread(target=main.run_cli, kwargs={"setup": setup}, daemon=True)
    runner.start()
    _wait(lambda: (ls.read_logger().get("state") or {}).get("phase") == "running")
    state = ls.read_logger()["state"]
    assert state["mode"] == "headless" and state["table"]["seats"] == [1, 2, 3]
    assert Path(state["log_dir"]) == (data_dir / "logs").resolve()          # 設定の log_dir（/etc）は使わない
    control = ControlCommandLog(Path(state["log_dir"]) / f"{state['session_id']}.control.jsonl")
    control.append("new_hand", {})
    control.append("text", {"text": "コール"})
    control.append("stop", {})
    runner.join(timeout=20)
    assert not runner.is_alive()
    got = ls.read_logger()
    assert got["status"] == "stopped"
    kinds = [r["kind"] for r in ls.read_notices()["notices"]]
    assert "control" in kinds and any("終了します" in r["text"] for r in ls.read_notices()["notices"])
    sessions = session_repository.SessionRepository().list_sessions()
    assert [s.status for s in sessions] == ["closed"]                       # q と同じくセッションを閉じた
    last = json.loads((data_dir / "logs" / "last_session_setup.json").read_text(encoding="utf-8"))
    assert last["seats"] == [1, 2, 3]                                       # 前回の設定として覚える
    assert ls.LoggerLock().acquire("cli")                                   # ロックは外れている


def test_a_bad_setup_does_not_start(data_dir: Path, capsys):
    main.run_cli(setup=_setup(data_dir / "bad.json", seats=[1]))
    assert "卓の設定が使えません" in capsys.readouterr().out
    assert any("使う席を 2 つ以上" in r["text"] for r in ls.read_notices()["notices"])
    assert ls.read_logger()["status"] == "stopped"


def test_the_terminal_logger_is_not_stopped_from_the_ipad(tmp_path):
    """端末のロガーは入力を待っているので、iPad の「終了」は未適用として知らせる（端末の q で終える）。"""
    import queue

    from integration.control_consumer import ControlConsumerThread

    log = ControlCommandLog(tmp_path / "s.control.jsonl")
    stopped: list = []
    consumer = ControlConsumerThread(log, queue.Queue(), threading.Event(), start_offset=0,
                                     pending_before=lambda since: pytest.fail("stop は聞き取り待ちを通さない"),
                                     on_stop=stopped.append)
    log.append("stop", {})
    consumer.poll_once()
    assert [c.type for c in stopped] == ["stop"]
