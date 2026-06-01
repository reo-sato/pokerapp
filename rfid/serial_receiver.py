"""rfid/serial_receiver.py

ESP32-S3 を USB ケーブルで直結したときの RFID 受信スレッド。

ESP32-S3 はネイティブ USB-OTG を持つため、USB 直結時は PC からは USB-CDC（仮想
シリアルポート, 例: /dev/ttyACM0 / COM3）として見える。本スレッドはそのポートを開き、
ESP32-S3 が送る **改行区切り JSON**（1 行 1 イベント）を読んで RFIDEvent に変換する。

受信ペイロード契約は HTTP transport と完全に同一（transport 非依存, ADR-0006/0007）:
    {"reader_id": "seat_3", "tag_id": "04A1B2C3D4E5F6", "timestamp": "2026-..."}
ペイロード → RFIDEvent 変換は `rfid.event_builder.build_rfid_event` に集約されており、
validation を transport 側に複製しない。

設計方針:
- pyserial は実行時に遅延インポートする（未インストール環境でもこのモジュールは import 可能）。
- ESP32-S3 の再起動・USB 抜き差しに耐えるよう、オープン失敗・読み取りエラー時は
  reconnect_interval 間隔で自動再接続する（クラッシュしない）。
- ブートログ等の非 JSON 行は debug ログで握りつぶしてスキップする。

設定例 (config.json):
    "rfid": {
      "enabled": true,
      "transport": "serial",
      "serial_port": "/dev/ttyACM0",
      "baudrate": 115200,
      "reconnect_interval_ms": 2000,
      "card_master_file": "./rfid_cards.json",
      "readers": { "seat_1": {"role": "seat", "seat": 1}, ... }
    }
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Callable, Optional

from core.event_queue import EventQueue
from rfid.card_master import CardMaster
from rfid.event_builder import build_rfid_event

logger = logging.getLogger(__name__)

# serial_factory の型: (port, baudrate) -> serial-like object (readline()/close())
SerialFactory = Callable[[str, int], object]


def _default_serial_factory(port: str, baudrate: int) -> object:
    """pyserial を遅延インポートして Serial を開く。

    timeout=1.0 により readline() は最大 1 秒でリターンするため、ループ毎に
    stop_event を確認でき、stop() に速やかに応答できる。
    """
    import serial  # 遅延インポート（pyserial）

    return serial.Serial(port, baudrate, timeout=1.0)


class RFIDSerialReceiver(threading.Thread):
    """USB-CDC シリアルから改行区切り JSON を読み、RFIDEvent を rfid_queue に投入する。

    スレッド安全: rfid_queue への put() のみ使用。stop() 後に join() 可能。
    """

    def __init__(
        self,
        rfid_queue: EventQueue,
        card_master: CardMaster,
        reader_configs: dict[str, dict],
        serial_port: str,
        baudrate: int = 115200,
        reconnect_interval_ms: int = 2000,
        stop_event: Optional[threading.Event] = None,
        serial_factory: Optional[SerialFactory] = None,
    ) -> None:
        super().__init__(daemon=True, name="RFIDSerialReceiver")
        self._rfid_queue = rfid_queue
        self._card_master = card_master
        self._reader_configs = reader_configs
        self._serial_port = serial_port
        self._baudrate = baudrate
        self._reconnect_interval = reconnect_interval_ms / 1000.0
        self._stop_event = stop_event or threading.Event()
        self._serial_factory: SerialFactory = serial_factory or _default_serial_factory

        # ステータス（GUI 表示用）
        self._connected: bool = False
        self._events_received: int = 0
        self._last_event_time: Optional[float] = None
        self._serial: Optional[object] = None

    # ――― 公開 API ―――

    def stop(self) -> None:
        self._stop_event.set()
        self._safe_close(self._serial)

    @property
    def status(self) -> dict:
        """GUI 表示用ステータス dict。メインスレッドから安全に読める。"""
        return {
            "running":         self.is_alive(),
            "connected":       self._connected,
            "serial_port":     self._serial_port,
            "baudrate":        self._baudrate,
            "events_received": self._events_received,
            "last_event_time": self._last_event_time,
        }

    # ――― スレッドライフサイクル ―――

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                ser = self._serial_factory(self._serial_port, self._baudrate)
            except Exception as exc:
                logger.warning(
                    "RFIDSerialReceiver: open %s failed: %s — retry in %.1fs",
                    self._serial_port, exc, self._reconnect_interval,
                )
                self._connected = False
                self._stop_event.wait(self._reconnect_interval)
                continue

            self._serial = ser
            self._connected = True
            logger.info(
                "RFIDSerialReceiver: opened %s @ %d baud",
                self._serial_port, self._baudrate,
            )
            try:
                self._read_loop(ser)
            except Exception as exc:
                logger.warning(
                    "RFIDSerialReceiver: read error on %s: %s — reconnecting",
                    self._serial_port, exc,
                )
            finally:
                self._connected = False
                self._safe_close(ser)
                self._serial = None

            if not self._stop_event.is_set():
                self._stop_event.wait(self._reconnect_interval)

        logger.info("RFIDSerialReceiver stopped")

    # ――― 内部 ―――

    def _read_loop(self, ser: object) -> None:
        """改行区切り JSON を 1 行ずつ読む。切断時は例外を投げて再接続に委ねる。"""
        while not self._stop_event.is_set():
            line: bytes = ser.readline()  # type: ignore[attr-defined]
            if not line:
                continue  # timeout（データなし）
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            self._handle_line(text)

    def _handle_line(self, text: str) -> None:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # ブートログ等の非 JSON 行。スキップする。
            logger.debug("RFIDSerialReceiver: non-JSON line skipped: %r", text)
            return
        if not isinstance(data, dict):
            logger.debug("RFIDSerialReceiver: non-object JSON skipped: %r", text)
            return

        event, _status = build_rfid_event(
            data, self._reader_configs, self._card_master
        )
        if event is None:
            return

        self._rfid_queue.put(event)
        self._events_received += 1
        self._last_event_time = time.time()

        logger.info(
            "RFIDSerialReceiver: received reader=%s tag=%s card=%r role=%s seat=%s board_index=%s",
            event.reader_id, event.tag_id, event.card or "(unknown)",
            event.role, event.seat, event.board_index,
        )

    @staticmethod
    def _safe_close(ser: Optional[object]) -> None:
        if ser is None:
            return
        try:
            ser.close()  # type: ignore[attr-defined]
        except Exception:
            pass
