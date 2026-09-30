"""tests/test_rfid_relay.py

店舗 2026-09-30: RDP で操作する店舗 PC で、RDP のセッションの中のアプリからリーダー（PC/SC）が見えず
（0x8010001D）、RFID のスレッドはログのファイルにだけ書いて止まり、2 ハンドが記録されなかった。システムの権限では
11 台すべて読めた。

- RFID の中継（`rfid/relay.py`, `tools/rfid_relay.py serve`）: システムの権限でリーダーを読み、127.0.0.1 だけに渡す。
- ロガーは中継が動いていれば中継から受け取る（`AutoRFIDSource`）。札の扱い（`RFIDThread`）は同じ。
- `RFIDThread` はリーダーにつながらなくても止まらず、つながるまで試し直す。CLI はつながり具合を出す。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from core.event_queue import make_rfid_queue
from rfid.card_master import CardMaster
from rfid.reader_thread import RFIDThread
from rfid.relay import AutoRFIDSource, RelayBridge, RelayClient, RelayPoller, make_relay_server, reader_key

NAME = "PokerRFID PN5180-CCID 0"
READERS = [
    {"name": NAME, "reader": 0, "role": "seat", "seat": 1},
    {"name": NAME, "reader": 1, "role": "seat", "seat": 2},
    {"name": NAME, "reader": 2, "role": "board"},
]


class _Table:
    """物理リーダーごとの UID（テストが書き換える）と、つながるかどうか。"""

    def __init__(self, present: bool = True):
        self.present = present
        self.uids: dict[int, list[str]] = {}

    def factory(self, name: str, index: int = 0):
        table = self

        class Bridge:
            def connect(self) -> bool:
                return table.present

            def read_uids(self) -> list[str]:
                return list(table.uids.get(index, []))

            def close(self) -> None:
                return None

        return Bridge()


@pytest.fixture
def relay():
    table = _Table()
    poller = RelayPoller(READERS, poll_interval_ms=5, bridge_factory=table.factory,
                         reader_present=lambda _name: table.present, reconnect_sec=0.1)
    server = make_relay_server(poller, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    poller.start()
    yield table, poller, RelayClient(port=server.server_address[1])
    poller.stop()
    server.shutdown()
    server.server_close()


def _wait(cond, timeout: float = 3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return False


class TestRelay:
    def test_the_relay_serves_each_readers_uids_on_localhost(self, relay):
        table, _, client = relay
        table.uids = {0: ["E0:04:01"], 2: ["E0:04:02", "E0:04:03"]}
        assert _wait(lambda: (client.fetch() or {}).get("readers", {}).get(reader_key(NAME, 2), {}).get("uids")
                     == ["E0:04:02", "E0:04:03"])
        snap = client.fetch()
        assert (snap["state"], snap["connected"], snap["configured"]) == ("running", 3, 3)
        assert client.url.startswith("http://127.0.0.1:")
        assert RelayBridge(NAME, 0, client).read_uids() == ["E0:04:01"]
        assert RelayBridge(NAME, 1, client).read_uids() == []

    def test_a_stopped_reading_is_not_used(self, relay):
        table, poller, client = relay
        table.uids = {0: ["E0:04:01"]}
        assert _wait(lambda: RelayBridge(NAME, 0, client).read_uids() == ["E0:04:01"])
        poller.stop()
        stale = RelayClient(port=int(client.url.rsplit(":", 1)[1].strip("/")), clock=lambda: time.time() + 10)
        assert RelayBridge(NAME, 0, stale).read_uids() == []

    def test_readers_that_are_not_there_yet_are_retried_quietly(self):
        table = _Table(present=False)
        poller = RelayPoller(READERS, bridge_factory=table.factory, reader_present=lambda _n: table.present,
                             reconnect_sec=0.0, clock=time.time)
        poller.poll_once()
        assert poller.snapshot()["state"] == "no_readers"
        table.present = True
        poller.poll_once()
        assert poller.snapshot()["connected"] == 3

    def test_no_relay(self):
        client = RelayClient(port=1)            # 何も待ち受けていない
        assert client.fetch() is None and not client.available()
        assert RelayBridge(NAME, 0, client).read_uids() == []

    def test_the_relay_is_reached_without_a_proxy(self, relay, monkeypatch):
        """Windows のプロキシ設定があっても 127.0.0.1 はプロキシに送らない。"""
        _, _, client = relay
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
        monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)
        assert RelayClient(port=int(client.url.rsplit(":", 1)[1].strip("/"))).fetch() is not None

    def test_a_config_change_swaps_the_readers(self):
        table = _Table()
        table.uids = {0: ["E0:04:01"], 5: ["E0:04:05"]}
        poller = RelayPoller(READERS, bridge_factory=table.factory, reader_present=lambda _n: True)
        poller.poll_once()
        assert poller.snapshot()["connected"] == 3
        poller.set_readers([READERS[0], {"name": NAME, "reader": 5, "role": "seat", "seat": 6}])
        poller.poll_once()                                     # 増えたリーダーはすぐ試す（5 秒待たない）
        snap = poller.snapshot()
        assert (snap["connected"], snap["configured"]) == (2, 2)
        assert set(snap["readers"]) == {reader_key(NAME, 0), reader_key(NAME, 5)}
        assert snap["readers"][reader_key(NAME, 5)]["uids"] == ["E0:04:05"]

    def test_the_config_watch_reloads_only_when_the_file_changes(self):
        from tools.rfid_relay import watch_config

        stamps = iter([(1, 1), (1, 1), (2, 1)])
        got = []
        stop = threading.Event()

        class _Poller:
            def set_readers(self, readers):
                got.append(readers)
                stop.set()

        def stamp():
            return next(stamps, (2, 1))

        watch_config(_Poller(), stop, interval=0.001, stamp=stamp,
                     load=lambda: {"pcsc_readers": [READERS[0]]})
        assert got == [[READERS[0]]]

    def test_an_error_does_not_stop_the_relay(self, monkeypatch):
        table = _Table()
        table.uids = {0: ["E0:04:01"]}
        poller = RelayPoller(READERS[:1], poll_interval_ms=1, bridge_factory=table.factory,
                             reader_present=lambda _n: True)
        calls = {"n": 0}
        real = poller.poll_once

        def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("SCARD_E_NO_SERVICE")
            real()

        monkeypatch.setattr(poller, "poll_once", flaky)
        poller.start()
        try:
            assert _wait(lambda: poller.snapshot()["readers"].get(reader_key(NAME, 0), {}).get("uids")
                         == ["E0:04:01"])
            assert poller.is_alive()
        finally:
            poller.stop()
            poller.join(timeout=3)


class TestLoggerReadsThroughTheRelay:
    def test_cards_come_through_the_relay_as_rfid_events(self, relay, tmp_path):
        table, _, client = relay
        source = AutoRFIDSource(client, direct_factory=lambda *a: pytest.fail("直接読まない"))
        q = make_rfid_queue()
        stop = threading.Event()
        thread = RFIDThread(rfid_queue=q, card_master=CardMaster(tmp_path / "cards.json"), reader_configs=READERS,
                            poll_interval_ms=10, stop_event=stop, bridge_factory=source.bridge,
                            reader_present=source.present)
        thread.start()
        try:
            assert _wait(lambda: thread.health["state"] == "running")
            assert source.mode == "relay" and thread.health["connected"] == 3
            table.uids = {1: ["E0:04:AA"]}
            ev = q.get(timeout=3)
            assert (ev.role, ev.seat, ev.tag_id) == ("seat", 2, "E0:04:AA")
        finally:
            stop.set()
            thread.join(timeout=2)

    def test_without_a_relay_the_readers_are_read_directly(self):
        made = []
        source = AutoRFIDSource(RelayClient(port=1), direct_factory=lambda name, index=0: made.append((name, index)),
                                direct_present=lambda _n: False)
        source.bridge(NAME, 3)
        assert made == [(NAME, 3)] and source.mode == "direct"
        assert source.present(NAME) is False


class TestReaderThreadRetries:
    def test_it_waits_for_the_readers_instead_of_stopping(self, tmp_path):
        table = _Table(present=False)
        stop = threading.Event()
        thread = RFIDThread(rfid_queue=make_rfid_queue(), card_master=CardMaster(tmp_path / "cards.json"),
                            reader_configs=READERS, poll_interval_ms=10, stop_event=stop,
                            bridge_factory=table.factory, reader_present=lambda _n: table.present,
                            reconnect_sec=0.1)
        thread.start()
        try:
            assert _wait(lambda: thread.health["state"] == "no_readers")
            assert thread.is_alive()                          # 前はここで終わっていた
            table.present = True                              # USB を挿し直した / 中継が動いた
            assert _wait(lambda: thread.health["state"] == "running")
        finally:
            stop.set()
            thread.join(timeout=2)
        assert not thread.is_alive()


class _Thread:
    def __init__(self, state: str, connected: int = 0, configured: int = 11):
        self.health = {"state": state, "connected": connected, "configured": configured}


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
    def test_connected_through_the_relay(self, capsys):
        import main

        stop = threading.Event()
        source = _Source("relay", {"state": "running", "connected": 11, "configured": 11})
        main._report_rfid_status(_Thread("running", 11), source, stop, wait_sec=0)
        stop.set()
        assert "RFID: リーダー 11/11 台を中継（RDP の外の読み取り）から読んでいます。" in capsys.readouterr().out

    def test_the_count_is_the_relays_own(self):
        """ロガーの側は中継につながれば全台「つながった」になるので、台数は中継が実際につないでいる台数。"""
        import main

        running = {"state": "running", "connected": 11, "configured": 11}
        msg = main._rfid_status_message(running, _Source("relay", {"state": "no_readers", "connected": 0,
                                                                   "configured": 11}))
        assert "中継は動いていますが、リーダーにつながっていません" in msg
        msg = main._rfid_status_message(running, _Source("relay", {"connected": 9, "configured": 11}))
        assert "リーダー 9/11 台を中継" in msg
        msg = main._rfid_status_message(running, _Source("relay", None))
        assert "中継（RDP の外の読み取り）が止まっています" in msg and "rfid_relay_task.ps1" in msg
        assert main._rfid_status_message({"state": "starting"}, _Source("direct")) is None
        assert "直接読んでいます" in main._rfid_status_message(running, _Source("direct"))

    def test_not_connected_says_what_to_do_and_reports_the_recovery(self, capsys):
        import main

        stop = threading.Event()
        thread = _Thread("no_readers")
        source = _Source("direct")
        main._report_rfid_status(thread, source, stop, wait_sec=0)
        out = capsys.readouterr().out
        assert "RFID: リーダーにつながりません（設定 11 台）" in out and "rfid_relay_task.ps1" in out
        source.mode = "relay"                                  # 中継が動いた → 次につなぐときに中継へ
        source.client.snap = {"state": "running", "connected": 11, "configured": 11}
        thread.health = {"state": "running", "connected": 11, "configured": 11}
        assert _wait(lambda: "11/11 台を中継" in capsys.readouterr().out, timeout=3.0)
        stop.set()

    def test_quitting_is_not_reported_as_a_stop(self, capsys):
        import main

        stop = threading.Event()
        stop.set()
        main._report_rfid_status(_Thread("stopped"), _Source("relay"), stop, wait_sec=0)
        assert "止まりました" not in capsys.readouterr().out


class TestCheckTool:
    def test_the_check_goes_through_a_running_relay(self, relay, capsys, monkeypatch):
        import tools.probe_pcsc as probe

        _, _, client = relay
        monkeypatch.setattr(probe, "_running_relay", lambda args: client)
        monkeypatch.setattr(probe, "load_rfid_config", lambda path=None: {"transport": "pcsc", "pcsc_readers": READERS})
        assert _wait(lambda: (client.fetch() or {}).get("connected") == 3)
        assert probe.main(["check"]) == 0
        out = capsys.readouterr().out
        assert "中継（RDP の外の読み取り）が動いています" in out and "PASS" in out

    def test_card_registration_goes_through_a_running_relay(self, relay, monkeypatch):
        import rfid.relay as relay_mod
        import tools.register_cards as reg

        _, _, client = relay
        monkeypatch.setattr(relay_mod, "running_relay", lambda cfg: client)
        seen = {}

        def fake_run(args, *, bridge_factory):
            seen["bridge"] = bridge_factory(NAME, 2)
            return 0

        monkeypatch.setattr(reg, "_cmd_run", fake_run)          # build_parser が読む名前も差し替わる
        monkeypatch.setattr(reg, "load_rfid_config", lambda path=None: {"pcsc_readers": READERS})
        assert reg.main(["run", "--deck", "1"]) == 0
        assert isinstance(seen["bridge"], RelayBridge) and seen["bridge"].reader_index == 2

    def test_the_task_script(self):
        raw = (Path(__file__).resolve().parent.parent / "installer" / "rfid_relay_task.ps1").read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf")                     # PowerShell 5.1 の -File（日本語）
        text = raw[3:].decode("utf-8")
        assert "\r\n" in text and r"tools\rfid_relay.py serve" in text and r"NT AUTHORITY\SYSTEM" in text
        assert "-AtStartup" in text and "PokerRFIDRelay" in text
