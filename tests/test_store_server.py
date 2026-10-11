"""tests/test_store_server.py

ADR-0065: 画面を持たない店舗サーバー（`python main.py --serve`）と、会計の書き手のロック。

- 会計を書くのは 1 プロセスだけ（店舗サーバーと会計画面が同時に書くと、あとから書いた方が相手の変更を消す）。
- 店舗サーバーは会計画面に組み込まれていた API と同じもの（注文・スタッフの会計 write が有効、
  お客さん向け画面 `/`・スタッフ用画面 `/staff/`）を出す。
- 卓モニタなどは同じプロセスの中で、落ちたら起動し直す（失敗が続くほど間を空ける）。
- API が待ち受けられなければ終了コード 1（タスクスケジューラが起動し直す）。ロックは手放す。
"""
from __future__ import annotations

import socket
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from core.store_lock import StoreWriterLock, describe_holder

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestStoreWriterLock:
    def test_only_one_writer_at_a_time(self, tmp_path: Path):
        path = tmp_path / "store_writer.lock"
        first, second = StoreWriterLock(path), StoreWriterLock(path)
        assert first.acquire("serve") is True
        assert second.acquire("ledger") is False
        assert second.holder()["mode"] == "serve"   # 持ち主はロックを持たずに読める
        first.release()
        assert second.acquire("ledger") is True
        assert second.holder()["mode"] == "ledger"
        second.release()

    def test_another_process_cannot_take_it(self, tmp_path: Path):
        path = tmp_path / "store_writer.lock"
        lock = StoreWriterLock(path)
        assert lock.acquire("ledger")
        code = textwrap.dedent(f"""
            import sys; sys.path.insert(0, {str(REPO_ROOT)!r})
            from core.store_lock import StoreWriterLock
            print(StoreWriterLock({str(path)!r}).acquire("serve"))
        """)
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
        assert out.stdout.strip() == "False"
        lock.release()
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
        assert out.stdout.strip() == "True"

    def test_a_crashed_holder_does_not_block_the_next_start(self, tmp_path: Path):
        # プロセスが落ちれば OS がロックを外す（ファイルが残っていても取れる）
        path = tmp_path / "store_writer.lock"
        code = textwrap.dedent(f"""
            import os, sys; sys.path.insert(0, {str(REPO_ROOT)!r})
            from core.store_lock import StoreWriterLock
            StoreWriterLock({str(path)!r}).acquire("serve"); os._exit(3)
        """)
        subprocess.run([sys.executable, "-c", code], check=False)
        assert path.exists()
        lock = StoreWriterLock(path)
        assert lock.acquire("ledger")
        lock.release()

    def test_describe_holder(self):
        assert "店舗サーバー" in describe_holder({"mode": "serve", "pid": 12, "since": "2026-10-11T10:00:00"})
        assert "会計画面" in describe_holder({"mode": "ledger", "pid": 12})
        assert describe_holder(None) == "別のプロセス"


pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

import api.store_server as store_server  # noqa: E402
from core.ledger_repository import LedgerRepository  # noqa: E402
from core.menu import MenuMaster  # noqa: E402
from core.order_request_repository import OrderRequestRepository  # noqa: E402
from core.player_repository import PlayerRepository  # noqa: E402
from core.session_repository import SessionRepository  # noqa: E402


def _store_client(tmp_path: Path, cfg: dict) -> TestClient:
    players = PlayerRepository(tmp_path / "players.json")
    sessions = SessionRepository(tmp_path / "sessions.json", player_repo=players)
    return TestClient(store_server.create_store_app(
        cfg, player_repo=players, session_repo=sessions,
        ledger_repo=LedgerRepository(tmp_path / "ledger.json", session_repo=sessions),
        order_repo=OrderRequestRepository(tmp_path / "order_requests.json", session_repo=sessions),
        menu=MenuMaster(tmp_path / "menu.json"),
    ))


class TestStoreApp:
    def test_it_is_the_writer_api_with_both_screens(self, tmp_path: Path):
        cfg = {"session": {"log_dir": str(tmp_path / "logs")},
               "viewer_api": {"staff_token": "tok"}, "ledger": {"buyin_presets": [5000]}}
        client = _store_client(tmp_path, cfg)
        auth = {"Authorization": "Bearer tok"}
        created = client.post("/api/staff/sessions", json={"label": "卓 1"}, headers=auth)
        assert created.status_code in (200, 201), created.text   # write が有効（503 ではない）
        assert client.get("/api/staff/buyin-presets", headers=auth).json()  # 会計の設定も同じ
        assert client.get("/").status_code == 200          # お客さん向け画面
        assert client.get("/staff/").status_code == 200    # スタッフ用画面
        assert client.get("/api/staff/sessions").status_code == 401   # トークンが要る

    def test_no_staff_token_means_no_staff_writes(self, tmp_path: Path):
        client = _store_client(tmp_path, {"session": {"log_dir": str(tmp_path / "logs")}})
        res = client.post("/api/staff/sessions", json={"label": "x"},
                          headers={"Authorization": "Bearer anything"})
        assert res.status_code == 403   # staff_writes_disabled（ADR-0021 のまま）


class TestServerSettings:
    def test_defaults_listen_on_the_lan_with_the_monitor(self):
        s = store_server.server_settings({})
        assert (s["host"], s["port"]) == ("0.0.0.0", 8788)
        assert s["table_monitor"] is True and s["table_monitor_port"] == 8790
        assert s["ground_truth_ui"] is False

    def test_precedence(self):
        cfg = {"viewer_api": {"bind_port": 9000},
               "store_server": {"bind_host": "192.168.1.5", "table_monitor": False}}
        s = store_server.server_settings(cfg)
        assert (s["host"], s["port"], s["table_monitor"]) == ("192.168.1.5", 9000, False)
        cfg["store_server"]["bind_port"] = 9100
        assert store_server.server_settings(cfg)["port"] == 9100
        assert store_server.server_settings(cfg, host="127.0.0.1", port=9200)["port"] == 9200


class _RecordingStop(threading.Event):
    """待ち時間を記録し、待たずに戻る stop（`limit` 回で止める）。"""

    def __init__(self, limit: int) -> None:
        super().__init__()
        self.waits: list[float] = []
        self.limit = limit

    def wait(self, timeout=None):  # noqa: D401
        self.waits.append(timeout)
        if len(self.waits) >= self.limit:
            self.set()
        return self.is_set()


class TestKeepRunning:
    def test_restarts_with_growing_waits_up_to_the_cap(self):
        calls = []

        def boom():
            calls.append(1)
            raise OSError("port in use")

        stop = _RecordingStop(limit=6)
        store_server.keep_running("卓モニタ", boom, stop, restart_sec=5, max_restart_sec=30,
                                  stable_sec=60, clock=lambda: 0.0).join(timeout=5)
        assert len(calls) == 6
        assert stop.waits == [5, 10, 20, 30, 30, 30]

    def test_a_long_run_resets_the_wait(self):
        # 1 回目・2 回目はすぐ落ちる（5 → 10）。3 回目は 198 秒動いてから止まった = 最初の 5 秒に戻す
        times = iter([0.0, 1.0, 1.0, 2.0, 2.0, 200.0])
        stop = _RecordingStop(limit=3)
        store_server.keep_running("x", lambda: None, stop, restart_sec=5, max_restart_sec=60,
                                  stable_sec=60, clock=lambda: next(times)).join(timeout=5)
        assert stop.waits == [5, 10, 5]


class TestRunStoreServer:
    def test_refuses_when_another_writer_holds_the_lock(self, tmp_path: Path, monkeypatch):
        path = tmp_path / "store_writer.lock"
        ledger = StoreWriterLock(path)
        assert ledger.acquire("ledger")
        import uvicorn
        monkeypatch.setattr(uvicorn, "Server", lambda *a, **k: pytest.fail("API を起動してはいけない"))
        code = store_server.run_store_server({}, lock=StoreWriterLock(path), log_to_file=False)
        assert code == 1
        ledger.release()

    def test_a_busy_port_ends_with_1_and_releases_the_lock(self, tmp_path: Path):
        busy = socket.socket()
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        port = busy.getsockname()[1]
        path = tmp_path / "store_writer.lock"
        cfg = {"session": {"log_dir": str(tmp_path / "logs")},
               "store_server": {"table_monitor": False}}
        try:
            code = store_server.run_store_server(cfg, host="127.0.0.1", port=port,
                                                 lock=StoreWriterLock(path), log_to_file=False)
        finally:
            busy.close()
        assert code == 1
        again = StoreWriterLock(path)
        assert again.acquire("serve")   # 終わったらロックを手放している
        again.release()


def test_main_has_the_serve_option():
    out = subprocess.run([sys.executable, str(REPO_ROOT / "main.py"), "--help"],
                         capture_output=True, text=True, cwd=REPO_ROOT, check=True)
    assert "--serve" in out.stdout


class TestAutostartScripts:
    """Windows の登録（`installer/store_server_task.ps1` + `server_autostart.cmd`）。CI には Windows が無いので中身を固定する。"""

    def test_the_task_script(self):
        raw = (REPO_ROOT / "installer" / "store_server_task.ps1").read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf")              # PowerShell 5.1 の -File（日本語）
        text = raw[3:].decode("utf-8")
        assert "\r\n" in text and "\n" not in text.replace("\r\n", "")
        assert '"main.py --serve"' in text and "PokerStoreServer" in text
        # 店舗 PC は通電で起動して自動サインインする → サインイン時 + 5 分ごと（オーナー決定 2026-10-11）
        assert "-AtLogOn -User $user" in text and "-RepetitionInterval (New-TimeSpan -Minutes 5)" in text
        assert "-MultipleInstances IgnoreNew" in text           # 動いていれば 5 分ごとの起動は何もしない
        assert "-LogonType Interactive" in text and "SYSTEM" not in text   # サインインしたユーザーの権限
        assert "RunAs" not in text and "-Remove" in text         # 管理者の確認は出さない
        assert r"venv\Scripts\pythonw.exe" in text              # 窓を出さない（閉じて止める事故を防ぐ）

    def test_the_launcher_calls_the_task_script(self):
        text = (REPO_ROOT / "server_autostart.cmd").read_text(encoding="ascii")
        assert r'installer\store_server_task.ps1" -Remove' in text
        assert r'installer\store_server_task.ps1"' in text

    def test_the_installer_stops_and_restarts_the_server_around_updates(self):
        text = (REPO_ROOT / "installer" / "install.ps1").read_bytes().decode("utf-8-sig")   # CRLF のまま
        assert '"PokerStoreServer"' in text
        venv = text.index("    Stop-StoreServer\r\n    Initialize-Venv")
        checked = text.index("    Test-Installation\r\n")
        restart = text.index("    Start-StoreServer\r\n")
        assert venv < checked < restart   # 依存を入れ替える前に止め、動作確認のあとで起動し直す
        assert "Remove-StoreServerTask" in text and "server_autostart.cmd" in text
