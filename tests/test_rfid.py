"""tests/test_rfid.py

Phase 6: RFID モジュールのテスト。
- CardMaster: lookup, register, normalize, bytes_to_tag_id, load/save
- PCSCBridge: MockPCSCBridge の基本動作
- RFIDThread: デバウンス、RFIDEvent 投入、ロール/席番号マッピング
"""
from __future__ import annotations

import json
import queue
import threading
import time
from pathlib import Path

import pytest

from core.events import RFIDEvent
from rfid.bridge import MockPCSCBridge
from rfid.card_master import CardMaster, bytes_to_tag_id, normalize_tag_id
from rfid.reader_thread import RFIDThread


# ――― normalize_tag_id ―――

class TestNormalizeTagId:
    def test_lowercase_hex_no_sep(self):
        assert normalize_tag_id("04abcdef1234") == "04:AB:CD:EF:12:34"

    def test_colon_sep_mixed_case(self):
        assert normalize_tag_id("04:ab:CD:ef:12:34") == "04:AB:CD:EF:12:34"

    def test_space_sep(self):
        assert normalize_tag_id("04 AB CD EF 12 34") == "04:AB:CD:EF:12:34"

    def test_single_byte(self):
        assert normalize_tag_id("ff") == "FF"

    def test_odd_length_padded(self):
        # 奇数桁は先頭に 0 を補完
        assert normalize_tag_id("abc") == "0A:BC"


class TestBytesToTagId:
    def test_basic(self):
        assert bytes_to_tag_id(b"\x04\xab\xcd") == "04:AB:CD"

    def test_single_byte(self):
        assert bytes_to_tag_id(b"\xff") == "FF"

    def test_empty(self):
        assert bytes_to_tag_id(b"") == ""


class TestIso15693Uid8Byte:
    """ADR-0034 / rfid-usb-ccid.md §7: ISO 15693 の 8 バイト UID を長さ非依存で扱う。"""

    _UID8 = b"\x04\xAB\xCD\xEF\x12\x34\x56\x78"
    _NORM8 = "04:AB:CD:EF:12:34:56:78"

    def test_bytes_to_tag_id_8byte(self):
        assert bytes_to_tag_id(self._UID8) == self._NORM8

    def test_normalize_roundtrip_8byte(self):
        # colon-hex / 連結 hex / 小文字 いずれも同一正規形に収束する。
        assert normalize_tag_id(self._NORM8) == self._NORM8
        assert normalize_tag_id("04abcdef12345678") == self._NORM8
        assert normalize_tag_id("04:ab:cd:ef:12:34:56:78") == self._NORM8

    def test_card_master_lookup_8byte(self, tmp_path: Path):
        cm = CardMaster(tmp_path / "cards.json")
        cm.register("04abcdef12345678", "Ah")
        assert cm.lookup(self._NORM8) == "Ah"
        assert cm.lookup_bytes(self._UID8) == "Ah"


# ――― CardMaster ―――

class TestCardMasterRegisterLookup:
    def test_lookup_registered_tag(self, tmp_path: Path):
        cm = CardMaster(tmp_path / "cards.json")
        cm.register("04:AB:CD:EF:12:34", "Ah")
        assert cm.lookup("04:AB:CD:EF:12:34") == "Ah"

    def test_lookup_normalizes_input(self, tmp_path: Path):
        cm = CardMaster(tmp_path / "cards.json")
        cm.register("04abcdef1234", "Kd")
        # さまざまな入力形式で同じ結果
        assert cm.lookup("04:AB:CD:EF:12:34") == "Kd"
        assert cm.lookup("04abcdef1234") == "Kd"
        assert cm.lookup("04 AB CD EF 12 34") == "Kd"

    def test_lookup_unknown_returns_empty(self, tmp_path: Path):
        cm = CardMaster(tmp_path / "cards.json")
        assert cm.lookup("FF:FF:FF:FF") == ""

    def test_register_invalid_card_raises(self, tmp_path: Path):
        cm = CardMaster(tmp_path / "cards.json")
        with pytest.raises(ValueError):
            cm.register("04:AB:CD:EF", "XX")

    def test_register_saves_to_file(self, tmp_path: Path):
        path = tmp_path / "cards.json"
        cm = CardMaster(path)
        cm.register("04:AA:BB:CC", "2c")
        data = json.loads(path.read_text())
        assert data["cards"]["04:AA:BB:CC"] == "2c"

    def test_load_existing_file(self, tmp_path: Path):
        path = tmp_path / "cards.json"
        path.write_text(json.dumps({
            "description": "test",
            "cards": {"04:AA:BB:CC": "2c", "04:11:22:33": "Qs"},
        }), encoding="utf-8")
        cm = CardMaster(path)
        assert cm.lookup("04:AA:BB:CC") == "2c"
        assert cm.lookup("04:11:22:33") == "Qs"
        assert len(cm) == 2

    def test_load_skips_invalid_cards(self, tmp_path: Path):
        path = tmp_path / "cards.json"
        path.write_text(json.dumps({
            "cards": {"04:AA:BB:CC": "2c", "04:11:22:33": "INVALID"},
        }), encoding="utf-8")
        cm = CardMaster(path)
        assert len(cm) == 1  # invalid はスキップ

    def test_missing_file_starts_empty(self, tmp_path: Path):
        cm = CardMaster(tmp_path / "nonexistent.json")
        assert len(cm) == 0

    def test_unregister(self, tmp_path: Path):
        cm = CardMaster(tmp_path / "cards.json")
        cm.register("04:AA:BB:CC", "Ah")
        assert cm.unregister("04:AA:BB:CC") is True
        assert cm.lookup("04:AA:BB:CC") == ""

    def test_unregister_unknown_returns_false(self, tmp_path: Path):
        cm = CardMaster(tmp_path / "cards.json")
        assert cm.unregister("00:00:00:00") is False

    def test_lookup_bytes(self, tmp_path: Path):
        cm = CardMaster(tmp_path / "cards.json")
        cm.register("04:AB:CD:EF", "Ts")
        assert cm.lookup_bytes(b"\x04\xab\xcd\xef") == "Ts"


# ――― MockPCSCBridge ―――

class TestMockPCSCBridge:
    def test_connect_returns_true(self):
        bridge = MockPCSCBridge("test_reader")
        assert bridge.connect() is True

    def test_read_uid_advances_then_clamps(self):
        bridge = MockPCSCBridge("r", uid_sequence=["04:AA", None, "04:BB"])
        assert bridge.read_uid() == "04:AA"
        assert bridge.read_uid() is None
        assert bridge.read_uid() == "04:BB"
        # 末尾で固定（循環しない）
        assert bridge.read_uid() == "04:BB"

    def test_empty_sequence_returns_none(self):
        bridge = MockPCSCBridge("r", uid_sequence=[])
        assert bridge.read_uid() is None


# ――― RFIDThread ―――

def _make_rfid_thread(
    tmp_path: Path,
    uid_sequences: dict[str, list],   # reader_name → uid_sequence
    reader_configs: list[dict],
    poll_interval_ms: int = 10,
) -> tuple[RFIDThread, queue.Queue, threading.Event]:
    from core.event_queue import make_rfid_queue
    rfid_q = make_rfid_queue()
    cm = CardMaster(tmp_path / "cards.json")

    def mock_factory(reader_name: str):
        seq = uid_sequences.get(reader_name, [])
        return MockPCSCBridge(reader_name, uid_sequence=seq)

    stop = threading.Event()
    thread = RFIDThread(
        rfid_queue=rfid_q,
        card_master=cm,
        reader_configs=reader_configs,
        poll_interval_ms=poll_interval_ms,
        stop_event=stop,
        bridge_factory=mock_factory,
    )
    return thread, rfid_q, stop


class TestRFIDThread:
    def test_new_card_touch_fires_event(self, tmp_path: Path):
        configs = [{"name": "reader_A", "role": "seat", "seat": 1}]
        # None → "04:AA" と変化するシーケンス
        sequences = {"reader_A": [None, "04:AA", "04:AA", "04:AA"]}
        thread, rfid_q, stop = _make_rfid_thread(tmp_path, sequences, configs)

        thread.start()
        time.sleep(0.15)
        stop.set()
        thread.join(timeout=2)

        assert not rfid_q.empty()
        ev: RFIDEvent = rfid_q.get_nowait()
        assert ev.tag_id == "04:AA"
        assert ev.role == "seat"
        assert ev.seat == 1

    def test_debounce_same_uid_fires_once(self, tmp_path: Path):
        """同一 UID が連続検出されても RFIDEvent は 1 回だけ投入される。"""
        configs = [{"name": "reader_A", "role": "seat", "seat": 2}]
        sequences = {"reader_A": ["04:BB", "04:BB", "04:BB", "04:BB", "04:BB"]}
        thread, rfid_q, stop = _make_rfid_thread(tmp_path, sequences, configs)

        thread.start()
        time.sleep(0.15)
        stop.set()
        thread.join(timeout=2)

        events = []
        while not rfid_q.empty():
            events.append(rfid_q.get_nowait())
        assert len(events) == 1

    def test_card_removed_and_retouched_fires_twice(self, tmp_path: Path):
        """カードが外れて再タッチした場合は 2 回イベントが発火する。"""
        configs = [{"name": "reader_A", "role": "seat", "seat": 1}]
        # 1回目タッチ → 外れる → 2回目タッチ
        sequences = {"reader_A": ["04:AA", None, "04:AA"]}
        thread, rfid_q, stop = _make_rfid_thread(tmp_path, sequences, configs)

        thread.start()
        time.sleep(0.2)
        stop.set()
        thread.join(timeout=2)

        events = []
        while not rfid_q.empty():
            events.append(rfid_q.get_nowait())
        assert len(events) == 2

    def test_board_role_has_no_seat(self, tmp_path: Path):
        configs = [{"name": "reader_B", "role": "board"}]
        sequences = {"reader_B": [None, "04:CC"]}
        thread, rfid_q, stop = _make_rfid_thread(tmp_path, sequences, configs)

        thread.start()
        time.sleep(0.15)
        stop.set()
        thread.join(timeout=2)

        assert not rfid_q.empty()
        ev: RFIDEvent = rfid_q.get_nowait()
        assert ev.role == "board"
        assert ev.seat is None

    def test_card_lookup_fills_card_field(self, tmp_path: Path):
        """CardMaster に登録済みのタグは card フィールドに値が入る。"""
        cm_path = tmp_path / "cards.json"
        # 事前登録
        cm = CardMaster(cm_path)
        cm.register("04:DD:EE:FF", "As")

        configs = [{"name": "reader_A", "role": "seat", "seat": 1}]
        sequences = {"reader_A": [None, "04:DD:EE:FF"]}

        from core.event_queue import make_rfid_queue
        rfid_q = make_rfid_queue()
        stop = threading.Event()

        def mock_factory(name):
            return MockPCSCBridge(name, uid_sequence=sequences.get(name, []))

        thread = RFIDThread(
            rfid_queue=rfid_q,
            card_master=CardMaster(cm_path),
            reader_configs=configs,
            poll_interval_ms=10,
            stop_event=stop,
            bridge_factory=mock_factory,
        )
        thread.start()
        time.sleep(0.15)
        stop.set()
        thread.join(timeout=2)

        ev: RFIDEvent = rfid_q.get_nowait()
        assert ev.card == "As"

    def test_no_readers_thread_exits_gracefully(self, tmp_path: Path):
        """リーダーが 1 台も接続できない場合、スレッドは正常終了する。"""
        from core.event_queue import make_rfid_queue
        rfid_q = make_rfid_queue()
        stop = threading.Event()

        def always_fail_factory(name):
            bridge = MockPCSCBridge(name)
            bridge.connect = lambda: False
            return bridge

        thread = RFIDThread(
            rfid_queue=rfid_q,
            card_master=CardMaster(tmp_path / "cards.json"),
            reader_configs=[{"name": "nonexistent", "role": "seat", "seat": 1}],
            poll_interval_ms=10,
            stop_event=stop,
            bridge_factory=always_fail_factory,
        )
        thread.start()
        thread.join(timeout=2)
        assert not thread.is_alive()

    def test_board_role_maps_to_event(self, tmp_path: Path):
        """ADR-0034 §4 / B3 修正: role=board が RFIDEvent.role=board + board_index を運ぶ。

        board_index は engine の street 自動遷移の分岐条件（engine.py:294）。PC/SC 経路でこれが
        欠落していたため board street が進まなかった（B3 latent bug）。回帰固定する。
        """
        configs = [{"name": "reader_B", "role": "board", "index": 3}]
        sequences = {"reader_B": [None, "04:11:22"]}
        thread, rfid_q, stop = _make_rfid_thread(tmp_path, sequences, configs)
        thread.start()
        time.sleep(0.15)
        stop.set()
        thread.join(timeout=2)
        ev: RFIDEvent = rfid_q.get_nowait()
        assert ev.role == "board"
        assert ev.seat is None
        assert ev.board_index == 3  # B3: PC/SC 経路でも board_index が流れること

    def test_seat_role_has_no_board_index(self, tmp_path: Path):
        """role=seat では board_index は None（B3 修正で seat に誤って付かないこと）。"""
        configs = [{"name": "reader_S", "role": "seat", "seat": 2}]
        sequences = {"reader_S": [None, "04:99"]}
        thread, rfid_q, stop = _make_rfid_thread(tmp_path, sequences, configs)
        thread.start()
        time.sleep(0.15)
        stop.set()
        thread.join(timeout=2)
        ev: RFIDEvent = rfid_q.get_nowait()
        assert ev.seat == 2
        assert ev.board_index is None

    def test_8byte_iso15693_uid_flows_to_event(self, tmp_path: Path):
        """ADR-0034 §7: 8B UID (ISO 15693) が RFIDEvent.tag_id まで長さ非依存で流れる。"""
        configs = [{"name": "reader_C", "role": "seat", "seat": 3}]
        uid8 = "04:AB:CD:EF:12:34:56:78"
        sequences = {"reader_C": [None, uid8, uid8]}
        thread, rfid_q, stop = _make_rfid_thread(tmp_path, sequences, configs)
        thread.start()
        time.sleep(0.15)
        stop.set()
        thread.join(timeout=2)
        ev: RFIDEvent = rfid_q.get_nowait()
        assert ev.tag_id == uid8
        assert ev.seat == 3
