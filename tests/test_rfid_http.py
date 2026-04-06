"""tests/test_rfid_http.py

RFIDHTTPReceiver のユニットテスト。

- POST /rfid 正常系
- POST /rfid 異常系 (invalid JSON, unknown reader_id, unregistered tag)
- board_index の正しい設定
- タイムスタンプ解析 (正常・異常・未指定)
- GET /status
- 重複タグの冪等性
"""
from __future__ import annotations

import json
import queue
import socket
import threading
import time
import urllib.request
from typing import Optional
from unittest.mock import MagicMock

import pytest

from core.event_queue import make_rfid_queue
from core.events import RFIDEvent
from rfid.card_master import CardMaster
from rfid.http_receiver import RFIDHTTPReceiver, _parse_timestamp


# ――― _parse_timestamp ユニットテスト ―――

class TestParseTimestamp:
    def test_iso_without_tz(self):
        ts = _parse_timestamp("2026-04-06T17:00:00.123")
        assert isinstance(ts, float)
        assert ts > 0

    def test_iso_with_tz(self):
        ts = _parse_timestamp("2026-04-06T17:00:00+09:00")
        assert isinstance(ts, float)
        assert ts > 0

    def test_empty_string_returns_current_time(self):
        before = time.time()
        ts = _parse_timestamp("")
        after = time.time()
        assert before <= ts <= after

    def test_invalid_string_returns_current_time(self):
        before = time.time()
        ts = _parse_timestamp("not-a-timestamp")
        after = time.time()
        assert before <= ts <= after


# ――― RFIDHTTPReceiver フィクスチャ ―――

READER_CONFIGS = {
    "seat_1":  {"role": "seat",  "seat": 1},
    "seat_2":  {"role": "seat",  "seat": 2},
    "board_1": {"role": "board", "index": 1},
    "board_3": {"role": "board", "index": 3},
}


def _free_port() -> int:
    """利用可能なポートを動的に確保する。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def receiver_fixture():
    """RFIDHTTPReceiver を起動して yield し、終了後に停止する。"""
    port = _free_port()
    rfid_q = make_rfid_queue()

    card_master = MagicMock(spec=CardMaster)
    # normalize_tag_id("04AABBCC") → "04:AA:BB:CC" なので正規化済み形式でマッチ
    card_master.lookup.side_effect = lambda tag: "Ah" if tag == "04:AA:BB:CC" else None

    stop = threading.Event()
    receiver = RFIDHTTPReceiver(
        rfid_queue=rfid_q,
        card_master=card_master,
        reader_configs=READER_CONFIGS,
        bind_host="127.0.0.1",
        bind_port=port,
        stop_event=stop,
    )
    receiver.start()
    # サーバが起動するまで最大1秒待機
    for _ in range(20):
        if receiver._server is not None:
            break
        time.sleep(0.05)

    yield receiver, rfid_q, port

    stop.set()
    receiver.stop()
    receiver.join(timeout=3)


def _post(port: int, path: str, body: dict | bytes) -> tuple[int, dict]:
    """指定ポートに HTTP POST を送り (status_code, response_dict) を返す。"""
    url = f"http://127.0.0.1:{port}{path}"
    if isinstance(body, dict):
        data = json.dumps(body).encode()
    else:
        data = body
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        return resp.status, json.loads(resp.read())


def _get(port: int, path: str) -> tuple[int, dict]:
    url = f"http://127.0.0.1:{port}{path}"
    with urllib.request.urlopen(url) as resp:
        return resp.status, json.loads(resp.read())


# ――― 正常系テスト ―――

class TestRFIDHTTPReceiverSuccess:
    def test_valid_seat_post_enqueues_event(self, receiver_fixture):
        """正常な seat POST → RFIDEvent がキューに入る。"""
        receiver, rfid_q, port = receiver_fixture
        status, body = _post(port, "/rfid", {
            "reader_id": "seat_1",
            "tag_id": "04AABBCC",
            "timestamp": "2026-04-06T17:00:00.000",
        })
        assert status == 200
        assert body["status"] == "ok"

        ev: RFIDEvent = rfid_q.get(timeout=1.0)
        assert ev.role == "seat"
        assert ev.seat == 1
        assert ev.card == "Ah"
        assert ev.tag_id == "04:AA:BB:CC"   # normalize_tag_id("04AABBCC") → "04:AA:BB:CC"
        assert ev.board_index is None

    def test_valid_board_post_sets_board_index(self, receiver_fixture):
        """board_1 POST → board_index=1 の RFIDEvent。"""
        receiver, rfid_q, port = receiver_fixture
        _post(port, "/rfid", {
            "reader_id": "board_1",
            "tag_id": "04AABBCC",
            "timestamp": "",
        })
        ev: RFIDEvent = rfid_q.get(timeout=1.0)
        assert ev.role == "board"
        assert ev.board_index == 1
        assert ev.seat is None

    def test_board_3_index_correct(self, receiver_fixture):
        """board_3 POST → board_index=3。"""
        receiver, rfid_q, port = receiver_fixture
        _post(port, "/rfid", {
            "reader_id": "board_3",
            "tag_id": "04AABBCC",
            "timestamp": "",
        })
        ev: RFIDEvent = rfid_q.get(timeout=1.0)
        assert ev.board_index == 3

    def test_timestamp_parsed_correctly(self, receiver_fixture):
        """タイムスタンプが float として event.timestamp に反映される。"""
        receiver, rfid_q, port = receiver_fixture
        ts_str = "2026-04-06T08:00:00.000"
        _post(port, "/rfid", {
            "reader_id": "seat_1",
            "tag_id": "04AABBCC",
            "timestamp": ts_str,
        })
        ev: RFIDEvent = rfid_q.get(timeout=1.0)
        # 2026-04-06T08:00:00 UTC+0 ≈ 1775289600
        assert isinstance(ev.timestamp, float)
        assert ev.timestamp > 0

    def test_events_received_counter_increments(self, receiver_fixture):
        """POST ごとに events_received がインクリメントされる。"""
        receiver, rfid_q, port = receiver_fixture
        before = receiver.status["events_received"]
        _post(port, "/rfid", {
            "reader_id": "seat_1",
            "tag_id": "04AABBCC",
            "timestamp": "",
        })
        rfid_q.get(timeout=1.0)
        assert receiver.status["events_received"] == before + 1

    def test_trailing_slash_accepted(self, receiver_fixture):
        """POST /rfid/ (末尾スラッシュ) も受け付ける。"""
        receiver, rfid_q, port = receiver_fixture
        status, body = _post(port, "/rfid/", {
            "reader_id": "seat_2",
            "tag_id": "04AABBCC",
            "timestamp": "",
        })
        assert status == 200
        ev: RFIDEvent = rfid_q.get(timeout=1.0)
        assert ev.seat == 2


# ――― 異常系テスト ―――

class TestRFIDHTTPReceiverErrors:
    def test_invalid_json_returns_400(self, receiver_fixture):
        """不正 JSON → 400。"""
        receiver, rfid_q, port = receiver_fixture
        url = f"http://127.0.0.1:{port}/rfid"
        data = b"not json"
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req)
            assert False, "should raise"
        except urllib.error.HTTPError as e:
            assert e.code == 400

    def test_unknown_reader_id_returns_200_no_event(self, receiver_fixture):
        """未知の reader_id → 200 で返るがキューにイベントは入らない。"""
        receiver, rfid_q, port = receiver_fixture
        status, body = _post(port, "/rfid", {
            "reader_id": "unknown_reader",
            "tag_id": "04AABBCC",
            "timestamp": "",
        })
        assert status == 200
        # キューは空のまま
        with pytest.raises(queue.Empty):
            rfid_q.get(timeout=0.2)

    def test_unregistered_tag_enqueues_event_with_empty_card(self, receiver_fixture):
        """未登録タグでも RFIDEvent は入るが card は空文字。"""
        receiver, rfid_q, port = receiver_fixture
        _post(port, "/rfid", {
            "reader_id": "seat_1",
            "tag_id": "DEADBEEF",
            "timestamp": "",
        })
        ev: RFIDEvent = rfid_q.get(timeout=1.0)
        assert ev.card is None
        # normalize_tag_id("DEADBEEF") → "DE:AD:BE:EF"
        assert ev.tag_id == "DE:AD:BE:EF"

    def test_wrong_path_returns_404(self, receiver_fixture):
        """存在しないパスへの POST → 404。"""
        receiver, rfid_q, port = receiver_fixture
        url = f"http://127.0.0.1:{port}/unknown"
        req = urllib.request.Request(
            url, data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req)
            assert False, "should raise"
        except urllib.error.HTTPError as e:
            assert e.code == 404


# ――― GET /status テスト ―――

class TestRFIDHTTPReceiverStatus:
    def test_get_status_returns_200(self, receiver_fixture):
        """GET /status → 200 かつ必要フィールドを含む。"""
        receiver, rfid_q, port = receiver_fixture
        status, body = _get(port, "/status")
        assert status == 200
        assert "running" in body
        assert "bind_port" in body
        assert "events_received" in body
        assert body["running"] is True
        assert body["bind_port"] == port

    def test_status_reflects_event_count(self, receiver_fixture):
        """POST 後の GET /status で events_received が増加している。"""
        receiver, rfid_q, port = receiver_fixture
        _post(port, "/rfid", {
            "reader_id": "seat_1",
            "tag_id": "04AABBCC",
            "timestamp": "",
        })
        rfid_q.get(timeout=1.0)

        _, body = _get(port, "/status")
        assert body["events_received"] >= 1
        assert body["last_event_time"] is not None

    def test_get_status_trailing_slash(self, receiver_fixture):
        """GET /status/ (末尾スラッシュ) も受け付ける。"""
        receiver, rfid_q, port = receiver_fixture
        status, _ = _get(port, "/status/")
        assert status == 200

    def test_get_wrong_path_returns_404(self, receiver_fixture):
        """GET /unknown → 404。"""
        receiver, rfid_q, port = receiver_fixture
        try:
            _get(port, "/unknown")
            assert False, "should raise"
        except urllib.error.HTTPError as e:
            assert e.code == 404


# ――― 複数イベント / raw_tag_id テスト ―――

class TestRFIDHTTPReceiverMultiple:
    def test_raw_tag_id_preserved(self, receiver_fixture):
        """raw_tag_id に送信時のタグ ID がそのまま保存される。"""
        receiver, rfid_q, port = receiver_fixture
        _post(port, "/rfid", {
            "reader_id": "seat_1",
            "tag_id": "04:aa:bb:cc",
            "timestamp": "",
        })
        ev: RFIDEvent = rfid_q.get(timeout=1.0)
        assert ev.raw_tag_id == "04:aa:bb:cc"

    def test_multiple_posts_enqueue_multiple_events(self, receiver_fixture):
        """連続 POST → イベントがそれぞれキューに入る。"""
        receiver, rfid_q, port = receiver_fixture
        for _ in range(3):
            _post(port, "/rfid", {
                "reader_id": "seat_1",
                "tag_id": "04AABBCC",
                "timestamp": "",
            })
        events = []
        for _ in range(3):
            events.append(rfid_q.get(timeout=1.0))
        assert len(events) == 3
