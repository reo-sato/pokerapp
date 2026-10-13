"""tests/test_rfid_restart.py

店舗 2026-10-06: 真ん中のボードのリーダー（reader 9）が 14:01〜14:58 に一度も札を読まず（エラーは出ない。ロガーを
起動し直しても同じ）、読み取り装置（ESP32）の再起動で直った。firmware は起動のときに初期化できなかったリーダーに
「札なし」と答え続け、host からは区別できない。

- firmware（契約 v1.11 §6）: 使えるリーダーの一覧（Get UID の P2=0xFE）と再起動の命令（P2=0xFD）。
- 中継（`rfid/relay.py`）: 一覧を読み取りと一緒に渡し、`POST /restart` で命令を送ってつなぎ直す。
- ロガー（`RFIDThread`）: 一覧に無いリーダーを知らせ、`rr` / 自動で、卓に札が無いときだけ再起動を頼む。ボードの
  リーダーが 2 ハンド続けて 1 枚も読まなければ知らせる（一覧に出ない不調。店舗 10/06 の健全な 59 ハンドでは
  どのボードのリーダーも毎ハンド読んでいた）。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from core.event_queue import make_rfid_queue
from rfid.card_master import CardMaster
from rfid.reader_thread import (
    AUTO_RESTART_EMPTY_SEC,
    AUTO_RESTART_GAP_SEC,
    AUTO_RESTART_MAX,
    READY_CHECK_SEC,
    RESTART_EMPTY_SEC,
    RESTART_SETTLE_SEC,
    RFIDThread,
)
from rfid.relay import AutoRFIDSource, RelayClient, RelayPoller, make_relay_server

NAME = "PokerRFID PN5180-CCID 0"
# 店舗の並び（席は reader 0..2、ボードは左 8・真ん中 9・右 10）
CONFIGS = [
    {"name": NAME, "reader": 0, "role": "seat", "seat": 1},
    {"name": NAME, "reader": 1, "role": "seat", "seat": 2},
    {"name": NAME, "reader": 2, "role": "seat", "seat": 3},
    {"name": NAME, "reader": 8, "role": "board"},
    {"name": NAME, "reader": 9, "role": "board"},
    {"name": NAME, "reader": 10, "role": "board"},
]
ALL = {0, 1, 2, 8, 9, 10}
SEAT1, SEAT2, LEFT, MIDDLE, RIGHT = 0, 1, 8, 9, 10
CARDS = {
    "04:A1": "As", "04:A2": "Kd", "04:B1": "Qh", "04:B2": "Jc",
    "04:F1": "5d", "04:F2": "Tc", "04:F3": "2h", "04:T1": "7c", "04:R1": "6h",
}


class _Bridge:
    def __init__(self, index: int, table: "Table") -> None:
        self.index = index
        self.table = table
        self.closed = False

    def connect(self) -> bool:
        self.table.connects += 1
        return True

    def read_uids(self) -> list[str]:
        return list(self.table.uids.get(self.index, []))

    def close(self) -> None:
        self.closed = True


class Device:
    """`AutoRFIDSource` の代わり（一覧・再起動・つなぎ直しの持ち主）。"""

    def __init__(self, ready=ALL, status: str = "accepted", manages: bool = True) -> None:
        self.ready = ready
        self.status = status
        self.manages = manages
        self.restarts: list[list[str]] = []
        self.restarting_flag = False

    def ready_readers(self, names):
        return {n: (None if self.ready is None else set(self.ready)) for n in names}

    def restart(self, names):
        self.restarts.append(list(names))
        return {n: self.status for n in names}

    def manages_connection(self) -> bool:
        return self.manages

    def restarting(self) -> bool:
        return self.restarting_flag


class Table:
    """時計を手で進めながら、本番の run() と同じ順（step_device → 全リーダーの poll → 卓の空き）で回す。"""

    def __init__(self, tmp_path: Path, device=None, auto: bool = False) -> None:
        cm = CardMaster(tmp_path / "cards.json")
        for uid, card in CARDS.items():
            cm.register(uid, card)
        self.uids: dict[int, list[str]] = {}
        self.connects = 0
        self.now = 1000.0
        self.notices: list[str] = []
        self.thread = RFIDThread(
            rfid_queue=make_rfid_queue(), card_master=cm, reader_configs=CONFIGS,
            stop_event=threading.Event(),
            bridge_factory=lambda name, reader=0: _Bridge(reader, self),
            reader_present=lambda _name: True,
            clock=lambda: self.now, device=device, on_notice=self.notices.append, auto_restart=auto,
        )
        self.thread._bridges = self.thread._connect_readers()  # noqa: SLF001 — run() と同じ

    def put(self, index: int, *uids: str) -> None:
        self.uids[index] = list(uids)

    def clear(self) -> None:
        self.uids = {}

    def run(self, seconds: float, step: float = 0.5) -> None:
        end = self.now + seconds
        while self.now < end - 1e-9:
            self.now = round(self.now + step, 6)
            th = self.thread
            th.step_device(self.now)
            if th._restart_phase is None:  # noqa: SLF001
                for reader_id, (bridge, cfg) in list(th._bridges.items()):  # noqa: SLF001
                    th._poll_reader(bridge, cfg, reader_id)  # noqa: SLF001
                th._note_table(self.now)  # noqa: SLF001
            while not th._queue.empty():  # noqa: SLF001
                th._queue.get_nowait()  # noqa: SLF001

    def deal_hand(self, board: dict[int, list[str]], seconds: float = 6.0) -> None:
        """手札を配り、ボードを置き、片付ける（片付けたあと 4 秒待つ = そのハンドのボードのリーダーを数える）。"""
        self.put(SEAT1, "04:A1", "04:A2")
        self.put(SEAT2, "04:B1", "04:B2")
        self.run(1.0)
        for index, uids in board.items():
            self.put(index, *uids)
        self.run(seconds)
        self.clear()
        self.run(4.0)


def _has(notices: list[str], text: str) -> bool:
    return any(text in n for n in notices)


class TestReadyList:
    def test_a_reader_missing_from_the_ready_list_is_announced(self, tmp_path):
        device = Device(ready=ALL - {MIDDLE})
        t = Table(tmp_path, device=device)
        t.run(1.0)
        assert _has(t.notices, "使えないリーダーがあります: ボードの左から 2 台目のリーダー（reader 9）")
        assert t.thread.health["unusable"] == ["ボードの左から 2 台目のリーダー（reader 9）"]
        before = len(t.notices)
        t.run(READY_CHECK_SEC * 3)
        assert len(t.notices) == before                  # 同じ見立ては繰り返し知らせない

    def test_an_unknown_ready_list_changes_nothing(self, tmp_path):
        # 旧 firmware・旧中継・起動の途中は一覧が返らない = いまの見立てのまま（知らせない）
        t = Table(tmp_path, device=Device(ready=None))
        t.run(READY_CHECK_SEC * 2)
        assert t.notices == []
        assert t.thread.health.get("unusable") is None

    def test_a_seat_reader_is_named_by_its_seat(self, tmp_path):
        t = Table(tmp_path, device=Device(ready=ALL - {1}))
        t.run(1.0)
        assert _has(t.notices, "席2 のリーダー（reader 1）")


class TestRestartOnlyWithAnEmptyTable:
    def test_rr_waits_until_the_cards_are_cleared(self, tmp_path):
        device = Device()
        t = Table(tmp_path, device=device)
        t.put(SEAT1, "04:A1", "04:A2")
        t.run(1.0)
        t.thread.request_restart()
        t.run(30.0)
        assert device.restarts == []                     # 札が載っている間は送らない
        assert _has(t.notices, "札を片付けたら読み取り装置を再起動します")
        t.clear()
        t.run(RESTART_EMPTY_SEC + 1.0)
        assert len(device.restarts) == 1
        assert t.thread.health["state"] == "restarting"   # CLI のつながり具合の行が「再起動しています」と出す

    def test_rr_on_an_empty_table_restarts_and_reports_all_readers(self, tmp_path):
        device = Device()
        t = Table(tmp_path, device=device)
        t.run(3.0)
        t.thread.request_restart()
        t.run(1.0)
        assert len(device.restarts) == 1
        assert t.thread.health["state"] == "restarting"
        t.run(RESTART_SETTLE_SEC + 3.0)
        assert t.thread.health["state"] == "running"
        assert _has(t.notices, "読み取り装置を再起動しました — 6 台とも使えます")

    def test_no_reads_during_the_restart(self, tmp_path):
        device = Device()
        t = Table(tmp_path, device=device)
        t.run(3.0)
        t.thread.request_restart()
        t.run(0.5)
        polled = []
        orig = t.thread._poll_reader  # noqa: SLF001
        t.thread._poll_reader = lambda *a: polled.append(a)  # noqa: SLF001
        t.run(RESTART_SETTLE_SEC - 0.5)
        assert polled == []
        t.thread._poll_reader = orig  # noqa: SLF001

    def test_direct_reading_closes_and_reconnects_the_readers(self, tmp_path):
        device = Device(manages=False)                    # 中継ではなくロガーがリーダーを直接読んでいる
        t = Table(tmp_path, device=device)
        old = [b for b, _cfg in t.thread._bridges.values()]  # noqa: SLF001
        connects = t.connects
        t.run(3.0)
        t.thread.request_restart()
        t.run(RESTART_SETTLE_SEC + 3.0)
        assert all(b.closed for b in old)
        assert t.connects == connects + len(CONFIGS)
        assert t.thread.health["state"] == "running"
        assert _has(t.notices, "6 台とも使えます")

    def test_a_relay_restart_waits_for_the_relay_to_reconnect(self, tmp_path):
        device = Device()
        t = Table(tmp_path, device=device)
        t.run(3.0)
        device.restarting_flag = True
        t.thread.request_restart()
        t.run(RESTART_SETTLE_SEC + 5.0)
        assert t.thread.health["state"] == "restarting"  # 中継がつなぎ直すまで待つ
        device.restarting_flag = False
        t.run(2.0)
        assert t.thread.health["state"] == "running"
        assert _has(t.notices, "6 台とも使えます")

    def test_still_unusable_after_the_restart_is_reported(self, tmp_path):
        device = Device(ready=ALL - {MIDDLE})
        t = Table(tmp_path, device=device)
        t.run(3.0)
        t.thread.request_restart()
        t.run(RESTART_SETTLE_SEC + 3.0)
        assert _has(t.notices, "再起動しましたが、まだ使えないリーダーがあります: ボードの左から 2 台目")
        assert _has(t.notices, "配線・電源を確かめてください")


class TestAutoRestart:
    def test_auto_restart_waits_for_an_empty_table(self, tmp_path):
        device = Device(ready=ALL - {MIDDLE})
        t = Table(tmp_path, device=device, auto=True)
        t.put(SEAT1, "04:A1", "04:A2")
        t.run(60.0)
        assert device.restarts == []
        assert _has(t.notices, "卓に札が無くなったら読み取り装置を自動で再起動します")
        t.clear()
        t.run(AUTO_RESTART_EMPTY_SEC - 1.0)
        assert device.restarts == []
        t.run(2.0)
        assert len(device.restarts) == 1
        assert t.thread.health["state"] == "restarting"

    def test_auto_restart_fixes_the_reader(self, tmp_path):
        device = Device(ready=ALL - {MIDDLE})
        t = Table(tmp_path, device=device, auto=True)
        t.run(AUTO_RESTART_EMPTY_SEC + 1.0)
        assert len(device.restarts) == 1
        device.ready = ALL
        t.run(RESTART_SETTLE_SEC + 3.0)
        assert _has(t.notices, "6 台とも使えます")
        assert t.thread.health["unusable"] == []
        t.run(AUTO_RESTART_GAP_SEC * 3)
        assert len(device.restarts) == 1                 # 直ったので繰り返さない

    def test_auto_restart_gives_up_after_the_limit(self, tmp_path):
        device = Device(ready=ALL - {MIDDLE})
        t = Table(tmp_path, device=device, auto=True)
        t.run((AUTO_RESTART_GAP_SEC + AUTO_RESTART_EMPTY_SEC) * (AUTO_RESTART_MAX + 2), step=1.0)
        assert len(device.restarts) == AUTO_RESTART_MAX
        assert _has(t.notices, f"{AUTO_RESTART_MAX} 回自動で再起動しても直りません")
        assert _has(t.notices, "卓に札が無くなったらもう一度試します")   # 1 回目のあと

    def test_rr_still_works_after_giving_up(self, tmp_path):
        device = Device(ready=ALL - {MIDDLE})
        t = Table(tmp_path, device=device, auto=True)
        t.run((AUTO_RESTART_GAP_SEC + AUTO_RESTART_EMPTY_SEC) * (AUTO_RESTART_MAX + 2), step=1.0)
        t.thread.request_restart()
        t.run(RESTART_EMPTY_SEC + 1.0)
        assert len(device.restarts) == AUTO_RESTART_MAX + 1

    def test_old_firmware_is_told_to_replug_once(self, tmp_path):
        device = Device(ready=ALL - {MIDDLE}, status="old_firmware")
        t = Table(tmp_path, device=device, auto=True)
        t.run(AUTO_RESTART_GAP_SEC * 4, step=1.0)
        assert len(device.restarts) == 1                 # 命令が無いなら自動では繰り返さない
        assert _has(t.notices, "ファームウェアに再起動の命令がありません")
        assert _has(t.notices, "USB を抜いて挿し直して")

    def test_an_outdated_relay_is_told_to_reregister(self, tmp_path):
        device = Device(ready=ALL - {MIDDLE}, status="relay_outdated")
        t = Table(tmp_path, device=device, auto=True)
        t.run(AUTO_RESTART_EMPTY_SEC + 2.0)
        assert _has(t.notices, "rfid_relay_task.ps1")

    def test_without_auto_restart_it_only_tells(self, tmp_path):
        device = Device(ready=ALL - {MIDDLE})
        t = Table(tmp_path, device=device, auto=False)
        t.run(AUTO_RESTART_GAP_SEC * 2)
        assert device.restarts == []
        assert _has(t.notices, "卓に札が無いときに rr で読み取り装置を再起動できます")


class TestNoDevice:
    def test_rr_without_a_device_says_how_to_restart_by_hand(self, tmp_path):
        t = Table(tmp_path, device=None)
        t.thread.request_restart()
        t.run(1.0)
        assert _has(t.notices, "再起動できません")
        assert _has(t.notices, "USB を抜いて挿し直して")


class TestSilentBoardReader:
    # 不調の 14:04〜14:21 の 4 ハンド: フロップは左、ターン・リバーは右が読み、真ん中は 1 枚も読まなかった
    BROKEN = {LEFT: ["04:F1", "04:F2", "04:F3"], RIGHT: ["04:T1", "04:R1"]}
    HEALTHY = {LEFT: ["04:F1", "04:F2"], MIDDLE: ["04:F3"], RIGHT: ["04:T1", "04:R1"]}

    def test_two_silent_hands_are_announced(self, tmp_path):
        t = Table(tmp_path, device=None)
        t.deal_hand(self.BROKEN)
        assert not _has(t.notices, "読んでいません")
        t.deal_hand(self.BROKEN)
        assert _has(t.notices, "ボードの左から 2 台目のリーダー（reader 9）が 2 ハンド続けて 1 枚も札を読んでいません")
        assert _has(t.notices, "USB を抜いて挿し直して")   # 再起動の命令が無い卓

    def test_a_reading_hand_resets_the_count(self, tmp_path):
        t = Table(tmp_path, device=None)
        t.deal_hand(self.BROKEN)
        t.deal_hand(self.HEALTHY)
        t.deal_hand(self.BROKEN)
        assert not _has(t.notices, "読んでいません")

    def test_the_rightmost_reader_counts_only_hands_with_a_turn(self, tmp_path):
        t = Table(tmp_path, device=None)
        flop_only = {LEFT: ["04:F1", "04:F2"], MIDDLE: ["04:F3"]}
        for _ in range(3):
            t.deal_hand(flop_only)
        assert not _has(t.notices, "読んでいません")

    def test_preflop_hands_are_not_counted(self, tmp_path):
        t = Table(tmp_path, device=None)
        for _ in range(3):
            t.deal_hand({})
        assert not _has(t.notices, "読んでいません")

    def test_a_silent_reader_is_restarted_automatically(self, tmp_path):
        device = Device()                                 # 一覧では使える = 一覧に出ない不調
        t = Table(tmp_path, device=device, auto=True)
        t.deal_hand(self.BROKEN)
        t.deal_hand(self.BROKEN)
        assert _has(t.notices, "卓に札が無くなったら読み取り装置を自動で再起動します")
        t.run(AUTO_RESTART_EMPTY_SEC)
        assert len(device.restarts) == 1
        t.run(RESTART_SETTLE_SEC + 3.0)
        # 再起動のあとは数え直し（次の 2 ハンドで読めば知らせない）
        t.deal_hand(self.HEALTHY)
        t.deal_hand(self.HEALTHY)
        assert len(device.restarts) == 1

    def test_restarts_for_a_silent_reader_are_bounded(self, tmp_path):
        # 店の置き方で真ん中に札が載らないだけ（リーダーは正常）でも、2 ハンドごとに再起動を繰り返さない
        device = Device()
        t = Table(tmp_path, device=device, auto=True)
        for _ in range(12):
            t.deal_hand(self.BROKEN)
            t.run(AUTO_RESTART_GAP_SEC, step=1.0)
        assert len(device.restarts) == AUTO_RESTART_MAX
        assert _has(t.notices, f"{AUTO_RESTART_MAX} 回自動で再起動しても直りません")

    def test_the_reader_reading_again_is_announced(self, tmp_path):
        t = Table(tmp_path, device=None)
        t.deal_hand(self.BROKEN)
        t.deal_hand(self.BROKEN)
        t.deal_hand(self.HEALTHY)
        assert _has(t.notices, "ボードの左から 2 台目のリーダー（reader 9）がまた札を読みました")


# ――― 中継（RDP のセッションの外でリーダーを読む）を通した一覧と再起動 ―――

class _RelayTable:
    def __init__(self) -> None:
        self.present = True
        self.uids: dict[int, list[str]] = {}
        self.ready: set[int] | None = set(ALL)
        self.restarts: list[str] = []
        self.restart_answer: bool | None = True
        self.connects = 0
        self.closed = 0

    def factory(self, name: str, index: int = 0):
        table = self

        class Bridge:
            def connect(self) -> bool:
                table.connects += 1
                return table.present

            def read_uids(self) -> list[str]:
                return list(table.uids.get(index, []))

            def close(self) -> None:
                table.closed += 1

        return Bridge()

    def ready_query(self, name: str):
        return None if self.ready is None else set(self.ready)

    def restart(self, name: str):
        self.restarts.append(name)
        return self.restart_answer


@pytest.fixture
def start_relay():
    """中継を動かす（`ready` = firmware が返す使えるリーダーの一覧）。"""
    started = []

    def start(ready=ALL):
        table = _RelayTable()
        table.ready = None if ready is None else set(ready)
        poller = RelayPoller(CONFIGS, poll_interval_ms=5, bridge_factory=table.factory,
                             reader_present=lambda _name: table.present, reconnect_sec=0.1,
                             ready_query=table.ready_query, restart_device=table.restart)
        server = make_relay_server(poller, port=0)
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        poller.start()
        started.append((poller, server))
        return table, poller, RelayClient(port=server.server_address[1])

    yield start
    for poller, server in started:
        poller.stop()
        server.shutdown()
        server.server_close()


def _wait(cond, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return False


class TestRelay:
    def test_the_relay_passes_the_ready_list(self, start_relay):
        _table, _poller, client = start_relay(ready=ALL - {MIDDLE})
        assert _wait(lambda: (client.fetch() or {}).get("ready", {}).get(NAME) is not None)
        assert set(client.fetch()["ready"][NAME]) == ALL - {MIDDLE}

    def test_restart_through_the_relay_reconnects(self, start_relay, monkeypatch):
        import rfid.relay as relay_mod

        monkeypatch.setattr(relay_mod, "RESTART_SETTLE_SEC", 0.2)
        table, _poller, client = start_relay()
        assert _wait(lambda: (client.fetch() or {}).get("connected") == len(CONFIGS))
        connects = table.connects
        result = client.restart()
        assert result["ok"] is True and result["results"] == {NAME: True}
        assert table.restarts == [NAME]
        assert table.closed == len(CONFIGS)               # 古い接続を捨てた
        assert _wait(lambda: not (client.fetch() or {}).get("restarting", True))
        assert table.connects == connects + len(CONFIGS)  # つなぎ直した
        assert set(client.fetch()["ready"][NAME]) == ALL

    def test_old_firmware_restart_is_refused(self, start_relay):
        table, _poller, client = start_relay()
        table.restart_answer = False
        result = client.restart()
        assert result["ok"] is False and result["results"] == {NAME: False}
        assert not (client.fetch() or {}).get("restarting")

    def test_an_old_relay_without_restart_is_detected(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class OldHandler(BaseHTTPRequestHandler):        # 前の版の中継（GET だけ）
            def do_GET(self):  # noqa: N802
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), OldHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            client = RelayClient(port=server.server_address[1])
            assert client.restart() == {"ok": False, "unsupported": True}
            source = AutoRFIDSource(client)
            source.mode = "relay"
            assert source.restart([NAME]) == {NAME: "relay_outdated"}
            assert source.ready_readers([NAME]) == {NAME: None}
        finally:
            server.shutdown()
            server.server_close()

    def test_the_source_reads_the_ready_list_from_the_relay(self, start_relay):
        _table, _poller, client = start_relay(ready=ALL - {MIDDLE})
        source = AutoRFIDSource(client)
        source.bridge(NAME, 0)                            # 中継が動いている = 中継から
        assert source.mode == "relay" and source.manages_connection()
        assert _wait(lambda: source.ready_readers([NAME])[NAME] == ALL - {MIDDLE})

    def test_direct_mode_sends_the_apdus_itself(self, monkeypatch):
        import rfid.relay as relay_mod

        monkeypatch.setattr(relay_mod, "query_ready_readers", lambda name: {0, 1})
        monkeypatch.setattr(relay_mod, "request_device_restart", lambda name: False)
        source = AutoRFIDSource(RelayClient(port=1), direct_factory=lambda *a: object(),
                                direct_present=lambda _n: True)
        source.bridge(NAME, 0)                            # 中継が動いていない = 直接
        assert source.mode == "direct" and not source.manages_connection()
        assert source.ready_readers([NAME]) == {NAME: {0, 1}}
        assert source.restart([NAME]) == {NAME: "old_firmware"}


class TestRelayStatusTool:
    def test_status_marks_unusable_readers(self):
        from tools.rfid_relay import describe

        now = 100.0
        snap = {
            "state": "running", "connected": len(CONFIGS), "configured": len(CONFIGS),
            "readers": {f"{NAME}|{c['reader']}": {"uids": [], "at": now} for c in CONFIGS},
            "ready": {NAME: sorted(ALL - {MIDDLE})},
        }
        ok, lines = describe(snap, CONFIGS, now)
        assert not ok
        assert sum("使えません" in line for line in lines) == 1
        assert any("python tools/rfid_relay.py restart" in line for line in lines)

    def test_status_while_restarting(self):
        from tools.rfid_relay import describe

        ok, lines = describe({"restarting": True}, CONFIGS, 0.0)
        assert not ok and "再起動しています" in lines[0]


# ――― CLI の表示と確認の道具 ―――

class _Client:
    def __init__(self, snap):
        self.snap = snap

    def snapshot(self, max_age: float = 0.0):
        return self.snap


class _Source:
    def __init__(self, mode: str, snap=None):
        self.mode = mode
        self.client = _Client(snap)


class TestCliStatus:
    def test_restarting_is_shown(self):
        import main

        assert "再起動しています" in main._rfid_status_message({"state": "restarting"}, _Source("direct"))
        running = {"state": "running", "connected": 11, "configured": 11}
        snap = {"state": "restarting", "connected": 0, "configured": 11, "restarting": True}
        assert "再起動しています" in main._rfid_status_message(running, _Source("relay", snap))

    def test_device_kwargs_default_to_auto_restart(self):
        import main

        source = object()
        kwargs = main._rfid_device_kwargs({}, source)
        assert kwargs["device"] is source and kwargs["auto_restart"] is True
        assert main._rfid_device_kwargs({"auto_restart": False}, source)["auto_restart"] is False


class TestProbeTool:
    def _config(self, tmp_path):
        import json

        path = tmp_path / "config.json"
        path.write_text(json.dumps({"rfid": {"transport": "pcsc", "pcsc_readers": CONFIGS}}), encoding="utf-8")
        return str(path)

    def test_list_marks_unusable_readers(self, tmp_path, monkeypatch, capsys):
        import argparse

        import tools.probe_pcsc as probe

        monkeypatch.setattr(probe, "pyscard_available", lambda: True)
        args = argparse.Namespace(config=self._config(tmp_path))
        assert probe._cmd_list(args, lister=lambda: [NAME], counter=lambda _n: 11,
                               ready_query=lambda _n: ALL - {MIDDLE}) == 0
        out = capsys.readouterr().out
        assert "使えるリーダー: 0, 1, 2, 8, 10" in out
        assert out.count("⚠ 使えません") == 1
        assert "python tools/probe_pcsc.py restart" in out

    def test_list_with_an_old_firmware(self, tmp_path, monkeypatch, capsys):
        import argparse

        import tools.probe_pcsc as probe

        monkeypatch.setattr(probe, "pyscard_available", lambda: True)
        args = argparse.Namespace(config=self._config(tmp_path))
        probe._cmd_list(args, lister=lambda: [NAME], counter=lambda _n: 11, ready_query=lambda _n: None)
        out = capsys.readouterr().out
        assert "非対応（v1.11 より前の firmware）" in out and "⚠" not in out

    def test_restart_waits_for_the_ready_list(self, monkeypatch, capsys):
        import argparse

        import tools.probe_pcsc as probe

        monkeypatch.setattr(probe, "pyscard_available", lambda: True)
        now = [0.0]
        answers = iter([None, None, ALL])                # 起動の途中は一覧が返らない
        rc = probe._cmd_restart(
            argparse.Namespace(wait=40.0), lister=lambda: [NAME], restarter=lambda _n: True,
            ready_query=lambda _n: next(answers), sleep=lambda sec: now.__setitem__(0, now[0] + sec),
            clock=lambda: now[0],
        )
        out = capsys.readouterr().out
        assert rc == 0
        assert "再起動を受け付けました" in out and "使えるリーダー: 0, 1, 2, 8, 9, 10" in out

    def test_restart_with_an_old_firmware(self, monkeypatch, capsys):
        import argparse

        import tools.probe_pcsc as probe

        monkeypatch.setattr(probe, "pyscard_available", lambda: True)
        rc = probe._cmd_restart(argparse.Namespace(wait=5.0), lister=lambda: [NAME],
                                restarter=lambda _n: False, sleep=lambda _s: None)
        out = capsys.readouterr().out
        assert rc == 1 and "USB を抜いて挿し直す" in out
