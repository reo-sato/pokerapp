"""tests/test_rfid_serial.py

RFIDSerialReceiver（ESP32-S3 USB-CDC 直結）のユニットテスト。

- 改行区切り JSON 行 → RFIDEvent 投入（正常系 / board / card lookup）
- 非 JSON 行・空行のスキップ（クラッシュしない）
- 未知 reader_id は enqueue されない（HTTP transport と同挙動）
- 未登録 tag は card="" で enqueue される（HTTP transport と同挙動）
- ISO15693 8 バイト UID の正規化（ADR-0006/0007 dual-support）
- オープン失敗時の自動再接続
- build_rfid_event 共通ヘルパの契約

serial ハードウェア / pyserial 不要: serial_factory に MockSerial を注入する。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from core.event_queue import make_rfid_queue
from core.events import RFIDEvent
from rfid.card_master import CardMaster
from rfid.event_builder import build_rfid_event
from rfid.serial_receiver import RFIDSerialReceiver


READER_CONFIGS = {
    "seat_1":  {"role": "seat",  "seat": 1},
    "seat_3":  {"role": "seat",  "seat": 3},
    "board_1": {"role": "board", "index": 1},
}


# ――― Mock serial ―――

class MockSerial:
    """改行区切りバイト行を順に返す偽 serial。

    lines を返し切ったら、以後 readline() は短い sleep 後 b"" を返し続ける
    （実 pyserial の readline timeout を模す）。fail_on_open=True なら最初の
    オープンで例外を投げ、以後は成功する（再接続テスト用）。
    """

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)
        self._i = 0
        self.closed = False

    def readline(self) -> bytes:
        if self._i < len(self._lines):
            ln = self._lines[self._i]
            self._i += 1
            return ln if isinstance(ln, bytes) else ln.encode()
        time.sleep(0.005)
        return b""

    def close(self) -> None:
        self.closed = True


def _line(reader_id: str, tag_id: str, timestamp: str = "") -> bytes:
    import json
    return (json.dumps({
        "reader_id": reader_id, "tag_id": tag_id, "timestamp": timestamp,
    }) + "\n").encode()


def _run_receiver(
    tmp_path: Path,
    lines: list[bytes],
    *,
    register: dict[str, str] | None = None,
    serial_factory=None,
    settle: float = 0.15,
) -> list[RFIDEvent]:
    """受信スレッドを起動して lines を流し、投入された RFIDEvent を返す。"""
    rfid_q = make_rfid_queue()
    cm = CardMaster(tmp_path / "cards.json")
    for tag, card in (register or {}).items():
        cm.register(tag, card)

    stop = threading.Event()
    if serial_factory is None:
        def serial_factory(port, baud):  # noqa: ANN001
            return MockSerial(lines)

    receiver = RFIDSerialReceiver(
        rfid_queue=rfid_q,
        card_master=cm,
        reader_configs=READER_CONFIGS,
        serial_port="/dev/ttyMOCK",
        baudrate=115200,
        reconnect_interval_ms=20,
        stop_event=stop,
        serial_factory=serial_factory,
    )
    receiver.start()
    time.sleep(settle)
    stop.set()
    receiver.stop()
    receiver.join(timeout=3)

    events: list[RFIDEvent] = []
    while not rfid_q.empty():
        events.append(rfid_q.get_nowait())
    return events


# ――― 正常系 ―――

class TestSerialReceiverSuccess:
    def test_valid_line_emits_event(self, tmp_path: Path):
        events = _run_receiver(tmp_path, [_line("seat_3", "04AABBCC")])
        assert len(events) == 1
        ev = events[0]
        assert ev.reader_id == "seat_3"
        assert ev.role == "seat"
        assert ev.seat == 3
        assert ev.tag_id == "04:AA:BB:CC"

    def test_board_role_has_no_seat(self, tmp_path: Path):
        events = _run_receiver(tmp_path, [_line("board_1", "04DDEEFF")])
        assert len(events) == 1
        assert events[0].role == "board"
        assert events[0].seat is None
        assert events[0].board_index == 1

    def test_card_lookup_fills_card_field(self, tmp_path: Path):
        events = _run_receiver(
            tmp_path, [_line("seat_1", "04:11:22:33")],
            register={"04:11:22:33": "As"},
        )
        assert len(events) == 1
        assert events[0].card == "As"

    def test_multiple_lines_emit_multiple_events(self, tmp_path: Path):
        events = _run_receiver(tmp_path, [
            _line("seat_1", "04AA"),
            _line("seat_3", "04BB"),
            _line("board_1", "04CC"),
        ])
        assert len(events) == 3


# ――― 異常系・堅牢性 ―――

class TestSerialReceiverRobustness:
    def test_non_json_line_skipped(self, tmp_path: Path):
        # ESP32 ブートログのような非 JSON 行を挟んでもクラッシュせず後続を処理する
        events = _run_receiver(tmp_path, [
            b"rst:0x1 (POWERON),boot:0x8\n",
            b"\n",
            _line("seat_1", "04AA"),
        ])
        assert len(events) == 1
        assert events[0].tag_id == "04:AA"

    def test_unknown_reader_not_enqueued(self, tmp_path: Path):
        events = _run_receiver(tmp_path, [_line("seat_99", "04AA")])
        assert events == []

    def test_unregistered_tag_enqueued_with_empty_card(self, tmp_path: Path):
        events = _run_receiver(tmp_path, [_line("seat_1", "04AA")])
        assert len(events) == 1
        assert events[0].card == ""

    def test_iso15693_8byte_uid_normalized(self, tmp_path: Path):
        # PN5180 が読む ISO15693 vicinity タグ（8 バイト UID）の dual-support
        events = _run_receiver(tmp_path, [_line("seat_1", "E0040150ABCDEF12")])
        assert len(events) == 1
        assert events[0].tag_id == "E0:04:01:50:AB:CD:EF:12"

    def test_reconnect_after_open_failure(self, tmp_path: Path):
        attempts = {"n": 0}

        def flaky_factory(port, baud):  # noqa: ANN001
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise OSError("port busy")
            return MockSerial([_line("seat_1", "04AA")])

        events = _run_receiver(
            tmp_path, [], serial_factory=flaky_factory, settle=0.2,
        )
        assert attempts["n"] >= 2          # 1 回失敗 → 再接続している
        assert len(events) == 1            # 再接続後にイベントを受信


# ――― 共通ヘルパ build_rfid_event ―――

class TestBuildRfidEvent:
    def test_unknown_reader_returns_none(self, tmp_path: Path):
        cm = CardMaster(tmp_path / "cards.json")
        ev, status = build_rfid_event(
            {"reader_id": "nope", "tag_id": "04AA"}, READER_CONFIGS, cm,
        )
        assert ev is None
        assert status == "unknown reader_id"

    def test_success_returns_event_ok(self, tmp_path: Path):
        cm = CardMaster(tmp_path / "cards.json")
        ev, status = build_rfid_event(
            {"reader_id": "seat_3", "tag_id": "04AA"}, READER_CONFIGS, cm,
        )
        assert status == "ok"
        assert ev is not None
        assert ev.seat == 3
        assert ev.raw_tag_id == "04AA"
