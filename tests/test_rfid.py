"""tests/test_rfid.py

Phase 6: RFID モジュールのテスト。
- CardMaster: lookup, register, normalize, bytes_to_tag_id, load/save
- PCSCBridge: MockPCSCBridge の基本動作 / 複数 UID 応答の分割（契約 v1.1 §6）
- RFIDThread: デバウンス（UID 集合）、RFIDEvent 投入、ロール/席番号マッピング、
  board の重ね置き位置割り当て（契約 v1.1 §4）
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from pathlib import Path

import pytest

from core.events import RFIDEvent
from rfid.bridge import MockPCSCBridge, PCSCBridge, bridge_read_uids, split_uid_response
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

    def test_read_uids_normalizes_str_none_and_list(self):
        """uid_sequence の要素は str / None / list[str] のいずれでもよい（v1.1 重ね置き）。"""
        bridge = MockPCSCBridge("r", uid_sequence=["04:AA", None, ["04:BB", "04:CC"]])
        assert bridge.read_uids() == ["04:AA"]
        assert bridge.read_uids() == []
        assert bridge.read_uids() == ["04:BB", "04:CC"]
        # 末尾クランプ後も list を返し続ける
        assert bridge.read_uids() == ["04:BB", "04:CC"]

    def test_read_uid_returns_first_of_stack(self):
        bridge = MockPCSCBridge("r", uid_sequence=[["04:BB", "04:CC"]])
        assert bridge.read_uid() == "04:BB"

    def test_read_uids_empty_sequence(self):
        assert MockPCSCBridge("r").read_uids() == []


# ――― 複数 UID 応答の分割（契約 v1.1 §6） ―――

class TestSplitUidResponse:
    """1 slot に複数枚（席 2 枚 / flop 3 枚）: 8B UID を枚数ぶん連結した応答を分割する。"""

    _A = b"\xE0\x04\x00\x00\x00\x00\x00\x01"
    _B = b"\xE0\x04\x00\x00\x00\x00\x00\x02"
    _C = b"\xE0\x04\x00\x00\x00\x00\x00\x03"

    def test_single_8byte(self):
        assert split_uid_response(self._A) == ["E0:04:00:00:00:00:00:01"]

    def test_two_cards_16byte(self):
        assert split_uid_response(self._A + self._B) == [
            "E0:04:00:00:00:00:00:01", "E0:04:00:00:00:00:00:02",
        ]

    def test_three_cards_24byte(self):
        uids = split_uid_response(self._A + self._B + self._C)
        assert len(uids) == 3 and uids[2] == "E0:04:00:00:00:00:00:03"

    def test_four_cards_32byte(self):
        assert len(split_uid_response(self._A + self._B + self._C + self._A)) == 4

    def test_4_and_7_byte_stay_single(self):
        # ISO 14443A は連結しない（従来どおり単一 UID）。
        assert split_uid_response(b"\x04\xAB\xCD\xEF") == ["04:AB:CD:EF"]
        assert split_uid_response(b"\x04\x11\x22\x33\x44\x55\x66") == ["04:11:22:33:44:55:66"]

    def test_other_lengths_stay_single(self):
        # 6B や 12B は「複数枚」の長さではないので単一 UID 扱い（分割しない）。
        assert split_uid_response(b"\x01\x02\x03\x04\x05\x06") == ["01:02:03:04:05:06"]
        assert len(split_uid_response(bytes(12))) == 1

    def test_empty(self):
        assert split_uid_response(b"") == []


class _FakeConnection:
    def __init__(self, data: bytes, sw: tuple[int, int]) -> None:
        self._data, self._sw = data, sw

    def connect(self) -> None:
        pass

    def transmit(self, apdu):
        assert apdu == [0xFF, 0xCA, 0x00, 0x00, 0x00]   # 契約 §6 Get UID
        return list(self._data), self._sw[0], self._sw[1]

    def disconnect(self) -> None:
        pass


class _FakeReader:
    def __init__(self, data: bytes, sw: tuple[int, int] = (0x90, 0x00), exc: Exception | None = None):
        self._data, self._sw, self._exc = data, sw, exc

    def createConnection(self):
        if self._exc is not None:
            raise self._exc
        return _FakeConnection(self._data, self._sw)


def _connected_bridge(data: bytes, sw: tuple[int, int] = (0x90, 0x00),
                      exc: Exception | None = None) -> PCSCBridge:
    """pyscard 無しで PCSCBridge の transmit 経路を通すための接続済み bridge。"""
    bridge = PCSCBridge("fake reader")
    bridge._reader = _FakeReader(data, sw, exc)   # noqa: SLF001 (テスト用シーム)
    bridge._connected = True                      # noqa: SLF001
    return bridge


class TestPCSCBridgeReadUids:
    _A = b"\xE0\x04\x00\x00\x00\x00\x00\x01"
    _B = b"\xE0\x04\x00\x00\x00\x00\x00\x02"

    def test_two_stacked_cards(self):
        assert _connected_bridge(self._A + self._B).read_uids() == [
            "E0:04:00:00:00:00:00:01", "E0:04:00:00:00:00:00:02",
        ]

    def test_single_card(self):
        assert _connected_bridge(self._A).read_uids() == ["E0:04:00:00:00:00:00:01"]

    def test_non_ok_sw_returns_empty(self):
        # カード無し = 6A 81（契約 §6/§8）。
        assert _connected_bridge(b"", sw=(0x6A, 0x81)).read_uids() == []

    def test_exception_returns_empty(self):
        assert _connected_bridge(self._A, exc=RuntimeError("no card")).read_uids() == []

    def test_not_connected_returns_empty(self):
        assert PCSCBridge("nope").read_uids() == []

    def test_read_uid_is_first_or_none(self):
        assert _connected_bridge(self._A + self._B).read_uid() == "E0:04:00:00:00:00:00:01"
        assert _connected_bridge(b"", sw=(0x6A, 0x81)).read_uid() is None


class TestBridgeReadUidsShim:
    """旧 bridge（`read_uid` のみ）互換シム。"""

    class _LegacyBridge:
        def __init__(self, uid=None):
            self.uid = uid

        def read_uid(self):
            return self.uid

    def test_legacy_bridge_is_listified(self):
        assert bridge_read_uids(self._LegacyBridge("04:AA")) == ["04:AA"]
        assert bridge_read_uids(self._LegacyBridge(None)) == []

    def test_new_bridge_uses_read_uids(self):
        assert bridge_read_uids(MockPCSCBridge("r", [["04:AA", "04:BB"]])) == ["04:AA", "04:BB"]


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

    def test_two_stacked_seat_cards_fire_two_events(self, tmp_path: Path):
        """席 reader に hole card 2 枚を重ねて置く → 同じ seat で 2 event（契約 v1.1 §6）。"""
        configs = [{"name": "reader_A", "role": "seat", "seat": 4}]
        sequences = {"reader_A": [None, ["04:AA", "04:BB"], ["04:AA", "04:BB"]]}
        thread, rfid_q, stop = _make_rfid_thread(tmp_path, sequences, configs)
        thread.start()
        time.sleep(0.15)
        stop.set()
        thread.join(timeout=2)

        events = []
        while not rfid_q.empty():
            events.append(rfid_q.get_nowait())
        assert [e.tag_id for e in events] == ["04:AA", "04:BB"]
        assert {e.seat for e in events} == {4}
        assert all(e.board_index is None for e in events)


# ――― 重ね置き: UID 集合デバウンス + board 位置割り当て（契約 v1.1 §4/§6/§8） ―――

class _ScriptedBridge:
    """`read_uids()` が返す UID 集合をテストから直接操作できる bridge。"""

    def __init__(self, uids: list[str] | None = None) -> None:
        self.uids = list(uids or [])

    def connect(self) -> bool:
        return True

    def read_uids(self) -> list[str]:
        return list(self.uids)

    def close(self) -> None:
        pass


class _LegacySingleBridge:
    """`read_uid()` しか持たない旧 bridge（互換経路の確認用）。"""

    def __init__(self, uid: str | None = None) -> None:
        self.uid = uid

    def connect(self) -> bool:
        return True

    def read_uid(self):
        return self.uid

    def close(self) -> None:
        pass


class _Poller:
    """RFIDThread を起動せず `_poll_reader` を手動で回す決定的ドライバ。"""

    def __init__(self, tmp_path: Path, cfg: dict, bridge, card_master: CardMaster | None = None):
        from core.event_queue import make_rfid_queue
        self.queue = make_rfid_queue()
        self.cfg = cfg
        self.bridge = bridge
        self.thread = RFIDThread(
            rfid_queue=self.queue,
            card_master=card_master or CardMaster(tmp_path / "cards.json"),
            reader_configs=[cfg],
            poll_interval_ms=10,
            stop_event=threading.Event(),
        )

    def poll(self) -> list[RFIDEvent]:
        """1 回ポーリングし、そこで発火した event を返す。"""
        self.thread._poll_reader(self.bridge, self.cfg, "reader_0")  # noqa: SLF001
        events = []
        while not self.queue.empty():
            events.append(self.queue.get_nowait())
        return events


class TestStackedDebounce:
    def test_two_cards_at_once_then_no_repeat(self, tmp_path: Path):
        p = _Poller(tmp_path, {"name": "R", "role": "seat", "seat": 1},
                    _ScriptedBridge(["04:AA", "04:BB"]))
        assert [e.tag_id for e in p.poll()] == ["04:AA", "04:BB"]
        assert p.poll() == []          # 置きっぱなしは再発火しない
        assert p.poll() == []

    def test_second_card_added_later_fires_only_new_uid(self, tmp_path: Path):
        p = _Poller(tmp_path, {"name": "R", "role": "seat", "seat": 1},
                    _ScriptedBridge(["04:AA"]))
        assert [e.tag_id for e in p.poll()] == ["04:AA"]
        p.bridge.uids = ["04:AA", "04:BB"]
        assert [e.tag_id for e in p.poll()] == ["04:BB"]

    def test_removing_one_card_emits_nothing_and_retouch_refires_only_it(self, tmp_path: Path):
        p = _Poller(tmp_path, {"name": "R", "role": "seat", "seat": 1},
                    _ScriptedBridge(["04:AA", "04:BB"]))
        p.poll()
        p.bridge.uids = ["04:AA"]
        assert p.poll() == []          # 外れた UID では event を出さない（状態更新のみ）
        p.bridge.uids = ["04:AA", "04:BB"]
        assert [e.tag_id for e in p.poll()] == ["04:BB"]   # 戻した UID だけ再発火

    def test_all_removed_then_all_returned_refires_both(self, tmp_path: Path):
        p = _Poller(tmp_path, {"name": "R", "role": "seat", "seat": 1},
                    _ScriptedBridge(["04:AA", "04:BB"]))
        p.poll()
        p.bridge.uids = []
        assert p.poll() == []
        p.bridge.uids = ["04:AA", "04:BB"]
        assert len(p.poll()) == 2

    def test_legacy_bridge_without_read_uids(self, tmp_path: Path):
        """旧 bridge（read_uid のみ）でも従来どおり 1 枚デバウンスで動く。"""
        bridge = _LegacySingleBridge(None)
        p = _Poller(tmp_path, {"name": "R", "role": "seat", "seat": 2}, bridge)
        assert p.poll() == []
        bridge.uid = "04:AA"
        assert [e.tag_id for e in p.poll()] == ["04:AA"]
        assert p.poll() == []
        bridge.uid = None
        assert p.poll() == []
        bridge.uid = "04:AA"
        assert [e.tag_id for e in p.poll()] == ["04:AA"]


class TestBoardStackPositions:
    """board reader の `cards` = 重ね置き枚数 → `index + offset` の位置割り当て。"""

    FLOP = {"name": "B1", "role": "board", "index": 1, "cards": 3}

    def test_flop_three_cards_get_positions_1_2_3(self, tmp_path: Path):
        p = _Poller(tmp_path, self.FLOP, _ScriptedBridge(["04:AA", "04:BB", "04:CC"]))
        events = p.poll()
        assert [e.board_index for e in events] == [1, 2, 3]
        assert len({e.board_index for e in events}) == 3
        assert all(e.role == "board" and e.seat is None for e in events)

    def test_incremental_placement_fills_lowest_free_offset(self, tmp_path: Path):
        p = _Poller(tmp_path, self.FLOP, _ScriptedBridge(["04:AA"]))
        assert [e.board_index for e in p.poll()] == [1]
        p.bridge.uids = ["04:AA", "04:BB"]
        assert [e.board_index for e in p.poll()] == [2]
        p.bridge.uids = ["04:AA", "04:BB", "04:CC"]
        assert [e.board_index for e in p.poll()] == [3]

    def test_removed_card_returns_to_same_position(self, tmp_path: Path):
        p = _Poller(tmp_path, self.FLOP, _ScriptedBridge(["04:AA", "04:BB", "04:CC"]))
        p.poll()
        p.bridge.uids = ["04:AA", "04:CC"]     # 真ん中（offset 1）を外す
        assert p.poll() == []
        p.bridge.uids = ["04:AA", "04:BB", "04:CC"]
        assert [e.board_index for e in p.poll()] == [2]   # 同じ位置に戻る

    def test_freed_position_is_reusable_by_another_card(self, tmp_path: Path):
        p = _Poller(tmp_path, self.FLOP, _ScriptedBridge(["04:AA", "04:BB"]))
        p.poll()
        p.bridge.uids = ["04:BB"]              # offset 0 が空く
        p.poll()
        p.bridge.uids = ["04:BB", "04:DD"]
        assert [e.board_index for e in p.poll()] == [1]

    def test_fourth_card_over_capacity_warns_and_has_no_index(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ):
        p = _Poller(tmp_path, self.FLOP, _ScriptedBridge(["04:AA", "04:BB", "04:CC"]))
        p.poll()
        p.bridge.uids = ["04:AA", "04:BB", "04:CC", "04:DD"]
        with caplog.at_level(logging.WARNING, logger="rfid.reader_thread"):
            events = p.poll()
        assert [e.tag_id for e in events] == ["04:DD"]
        assert events[0].board_index is None            # engine の「末尾に追記」経路へ
        assert any("cards=3" in r.getMessage() for r in caplog.records)

    def test_single_card_board_reader_uses_index_as_is(self, tmp_path: Path):
        cfg = {"name": "B2", "role": "board", "index": 4}     # cards 省略 = 1
        p = _Poller(tmp_path, cfg, _ScriptedBridge(["04:11"]))
        assert [e.board_index for e in p.poll()] == [4]

    def test_board_reader_without_index_keeps_none(self, tmp_path: Path):
        cfg = {"name": "B3", "role": "board"}
        p = _Poller(tmp_path, cfg, _ScriptedBridge(["04:AA", "04:BB"]))
        assert [e.board_index for e in p.poll()] == [None, None]

    def test_invalid_cards_value_falls_back_to_one(self, tmp_path: Path):
        cfg = {"name": "B4", "role": "board", "index": 2, "cards": 0}
        p = _Poller(tmp_path, cfg, _ScriptedBridge(["04:AA", "04:BB"]))
        assert [e.board_index for e in p.poll()] == [2, None]
