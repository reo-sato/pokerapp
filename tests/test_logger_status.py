"""ロガーを 1 つに限るロック・状態とお知らせのファイル・読む API（ADR-0073 D2・D3, `core/logger_status.py`）。

iPad（スタッフ用画面）でロガーの様子を見るための土台。RDP で起動したロガーも同じファイルを書く。
置き場は試験ごとの一時フォルダ（`tests/conftest.py` が `POKERAPP_LOGGER_STATE_DIR` を差し替える）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import main
from core import logger_status as ls
from tests.test_cli_session_layer import _cfg, _run_cli, _setup, data_dir  # noqa: F401 — fixture

pytest.importorskip("fastapi")


def test_only_one_logger_at_a_time():
    first = ls.LoggerLock()
    assert first.acquire_with_retries("cli", retries=1)
    try:
        second = ls.LoggerLock()
        assert not second.acquire_with_retries("headless", retries=2, wait=0.05)
        holder = second.holder()
        assert holder["mode"] == "cli" and holder["pid"] == os.getpid()
        assert "端末（RDP）で起動したロガー" in ls.describe_logger_holder(holder)
    finally:
        first.release()
    again = ls.LoggerLock()
    assert again.acquire("headless")
    again.release()


def test_state_and_notices_are_written_and_read(tmp_path):
    status = ls.LoggerStatus("cli", directory=tmp_path, version={"commit": "abc"})
    status.attach("mic", lambda: {"state": "running", "device_name": "Wireless Mic Rx", "level": 0.4})
    status.attach("broken", lambda: 1 / 0)                     # 集められない値があっても書ける
    status.set(phase="running", session_id="sid1")
    status.notice("notice", "手札が配られました（ハンド 1）")
    status.notice("action", "[preflop] 席3(C) raise 600  pot=900", hand_id=1)
    status.write()                                              # 心拍（3 秒ごと）の代わり

    got = ls.read_logger(tmp_path)
    assert got["status"] == "running" and got["state"]["session_id"] == "sid1"
    assert got["state"]["mic"]["device_name"] == "Wireless Mic Rx" and "error" in got["state"]["broken"]
    assert got["state"]["notice_seq"] == 2

    n = ls.read_notices(tmp_path)
    assert [r["kind"] for r in n["notices"]] == ["notice", "action"] and n["last_seq"] == 2
    assert n["notices"][1]["hand_id"] == 1 and n["notices"][1]["session_id"] == "sid1"
    assert ls.read_notices(tmp_path, since=1)["notices"][0]["seq"] == 2
    assert ls.read_notices(tmp_path, since=2)["notices"] == []

    status.close()
    assert ls.read_logger(tmp_path)["status"] == "stopped"
    # 起動し直し: 前のお知らせは prev に移り、番号は 1 から。前の番号で読むと初めから返す（reset）
    again = ls.LoggerStatus("cli", directory=tmp_path)
    again.notice("notice", "次のセッション")
    assert (tmp_path / ls.PREV_NOTICES_FILE).is_file()
    r = ls.read_notices(tmp_path, since=2)
    assert r["reset"] and [x["text"] for x in r["notices"]] == ["次のセッション"]


def test_unresponsive_and_crashed(tmp_path):
    status = ls.LoggerStatus("cli", directory=tmp_path, clock=lambda: 1000.0)
    status.set(phase="running")
    assert ls.read_logger(tmp_path, now=1010.0)["status"] == "running"
    assert ls.read_logger(tmp_path, now=1100.0)["status"] == "unresponsive"      # 心拍が古い
    # 終わったプロセスの pid で「動作中」のまま = 落ちた
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    state = json.loads((tmp_path / ls.STATE_FILE).read_text(encoding="utf-8"))
    state["pid"] = dead.pid
    (tmp_path / ls.STATE_FILE).write_text(json.dumps(state), encoding="utf-8")
    assert ls.read_logger(tmp_path, now=1010.0)["status"] == "crashed"
    assert ls.read_logger(tmp_path / "none")["status"] == "stopped"


def test_the_staff_api_reads_the_logger(tmp_path):
    from fastapi.testclient import TestClient

    from api.server import create_app
    from core.player_repository import PlayerRepository
    from core.session_repository import SessionRepository

    players = PlayerRepository(tmp_path / "players.json")
    app = create_app(players, SessionRepository(tmp_path / "sessions.json", player_repo=players), tmp_path,
                     staff_token="tok")
    http = TestClient(app)
    assert http.get("/api/staff/logger").status_code == 401
    head = {"Authorization": "Bearer tok"}
    assert http.get("/api/staff/logger", headers=head).json()["status"] == "stopped"
    status = ls.LoggerStatus("cli")
    status.set(phase="running", session_id="sid1")
    status.notice("notice", "マイクを開きました")
    body = http.get("/api/staff/logger", headers=head).json()
    assert body["status"] == "running" and body["state"]["session_id"] == "sid1"
    n = http.get("/api/staff/logger/notices?since=0", headers=head).json()
    assert [r["text"] for r in n["notices"]] == ["マイクを開きました"] and n["last_seq"] == 1
    status.close()


def test_the_cli_logger_writes_its_state_and_notices(data_dir: Path, monkeypatch):  # noqa: F811
    logs = data_dir / "logs"
    _run_cli(monkeypatch, _cfg(True), _setup(["太郎", "花子"], logs) + ["n", "チェック", "w 1", "q"])
    got = ls.read_logger()
    assert got["status"] == "stopped" and got["state"]["mode"] == "cli"
    assert got["state"]["session_id"] and got["state"]["table"]["seats"] == [1, 2]
    kinds = {r["kind"] for r in ls.read_notices()["notices"]}
    assert "action" in kinds
    assert ls.LoggerLock().acquire("cli")                          # 終わったらロックは外れている


def test_a_second_logger_does_not_start(data_dir: Path, monkeypatch, capsys):  # noqa: F811
    held = ls.LoggerLock()
    assert held.acquire("headless")
    monkeypatch.setattr(ls, "LOCK_RETRIES", 1)
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("起動してしまった"))
    try:
        main.run_cli()
    finally:
        held.release()
    assert "別のロガーが動いています" in capsys.readouterr().out


def test_a_session_change_reopens_the_mic_once():
    """RDP の接続・切断（セッションの種類が変わった）とき、マイクの開き直しを 1 回だけ頼む。変わらなければ何もしない。"""
    console = {"session": 1, "console": True, "remote": False, "state": "active"}
    rdp = {"session": 1, "console": False, "remote": True, "state": "active"}
    gone = {"session": 1, "console": False, "remote": False, "state": "disconnected"}
    now = [console]
    reopened: list[str] = []
    notes: list[str] = []
    check = ls.session_watcher(lambda: now[0], notes.append, reopen=reopened.append)
    check()
    assert reopened == []
    now[0] = rdp
    check()
    check()
    now[0] = gone
    check()
    assert reopened == ["RDP でつながりました", "RDP が閉じられました（切断）"] and notes == []
    # マイクを使わないときは知らせるだけ・Windows 以外（種類が分からない）は何もしない
    only = ls.session_watcher(lambda: now[0], notes.append)
    now[0] = console
    only()
    assert notes == ["コンソールに戻りました"]
    other = ls.session_watcher(lambda: {"platform": "linux"}, notes.append, reopen=reopened.append)
    other()
    assert len(notes) == 1 and len(reopened) == 2


def test_hooks_run_with_the_heartbeat(tmp_path, monkeypatch):
    monkeypatch.setattr(ls, "HEARTBEAT_SEC", 0.01)
    calls: list[int] = []
    status = ls.LoggerStatus("cli", directory=tmp_path)
    status.add_hook(lambda: calls.append(1))
    status.add_hook(lambda: 1 / 0)                       # 失敗しても止まらない
    status.start()
    import time

    deadline = time.time() + 2
    while len(calls) < 3 and time.time() < deadline:
        time.sleep(0.01)
    status.close()
    assert len(calls) >= 3
