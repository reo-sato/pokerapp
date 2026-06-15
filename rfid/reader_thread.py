"""rfid/reader_thread.py

Phase 6: RFID リーダーをポーリングして RFIDEvent を rfid_queue に投入するスレッド。

設計方針:
- 各リーダーを poll_interval_ms 間隔でポーリングする。
- デバウンス: 同一タグが連続検出された場合はイベントを投入しない。
  タグが外れる（None）→ 再タッチ（同一 UID）した場合のみ再度イベントを投入する。
- reader_configs: [{"name": "...", "role": "seat", "seat": 1}, ...] の形式。
  role="board" の場合は seat フィールド不要。

設定例 (config.json):
    "rfid": {
      "enabled": true,
      "reader_type": "pcsc",
      "poll_interval_ms": 100,
      "readers": [
        {"name": "ACS ACR122U PICC Interface 0", "role": "seat", "seat": 1},
        {"name": "ACS ACR122U PICC Interface 1", "role": "board"}
      ],
      "card_master_file": "./rfid_cards.json"
    }
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from core.event_queue import EventQueue
from core.events import RFIDEvent
from rfid.bridge import PCSCBridge
from rfid.card_master import CardMaster

logger = logging.getLogger(__name__)


class RFIDThread(threading.Thread):
    """全 RFID リーダーをポーリングし、タッチ検出時に RFIDEvent を rfid_queue に投入する。"""

    def __init__(
        self,
        rfid_queue: EventQueue,
        card_master: CardMaster,
        reader_configs: list[dict],
        poll_interval_ms: int = 100,
        stop_event: Optional[threading.Event] = None,
        bridge_factory: Optional[object] = None,
    ) -> None:
        """
        Args:
            rfid_queue:       RFIDEvent を投入するキュー。
            card_master:      タグ ID → カード文字列 の対応表。
            reader_configs:   リーダー設定リスト。各要素は:
                              {"name": str, "role": "seat"|"board", "seat": int (roleが"seat"の場合)}
            poll_interval_ms: ポーリング間隔 (ミリ秒)。
            stop_event:       セット時にスレッドを停止する。
            bridge_factory:   テスト用ブリッジファクトリ (reader_name: str) -> bridge。
                              省略時は PCSCBridge を使用。
        """
        super().__init__(daemon=True, name="RFIDThread")
        self._queue = rfid_queue
        self._card_master = card_master
        self._reader_configs = reader_configs
        self._poll_interval = poll_interval_ms / 1000.0
        self._stop_event = stop_event or threading.Event()
        self._bridge_factory = bridge_factory or PCSCBridge

        # デバウンス用: reader_id → 最後に検出した UID (None = カードなし)
        self._last_uid: dict[str, Optional[str]] = {}

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        logger.info("RFIDThread started (%d reader(s))", len(self._reader_configs))

        # ブリッジを初期化
        bridges: dict[str, object] = {}
        for i, cfg in enumerate(self._reader_configs):
            reader_name = cfg.get("name", "")
            reader_id = f"reader_{i}"
            bridge = self._bridge_factory(reader_name)
            if bridge.connect():
                bridges[reader_id] = (bridge, cfg)
                self._last_uid[reader_id] = None
                logger.info("RFID reader ready: %s (%s)", reader_name, reader_id)
            else:
                logger.warning("Could not connect to RFID reader: %s", reader_name)

        if not bridges:
            logger.warning("No RFID readers connected. RFIDThread exiting.")
            return

        try:
            while not self._stop_event.is_set():
                for reader_id, (bridge, cfg) in bridges.items():
                    self._poll_reader(bridge, cfg, reader_id)
                time.sleep(self._poll_interval)
        finally:
            for reader_id, (bridge, _) in bridges.items():
                bridge.close()
            logger.info("RFIDThread stopped")

    def _poll_reader(self, bridge: object, cfg: dict, reader_id: str) -> None:
        """1 リーダーをポーリングし、新規タッチ時のみ RFIDEvent を投入する。"""
        uid: Optional[str] = bridge.read_uid()
        prev_uid = self._last_uid.get(reader_id)

        # デバウンス: 前回と同じ状態なら何もしない
        if uid == prev_uid:
            return

        self._last_uid[reader_id] = uid

        if uid is None:
            # カードが外れた → デバウンス状態をリセットするのみ（イベント不要）
            logger.debug("Card removed from %s", reader_id)
            return

        # 新規タッチ検出
        card = self._card_master.lookup(uid)
        role = cfg.get("role", "seat")
        seat = cfg.get("seat") if role == "seat" else None
        # board_index は board street 自動遷移に必須（engine が board_index!=None を分岐条件にする,
        # engine.py:294）。http_receiver と同じく cfg["index"] から設定する（ADR-0034, B3 修正）。
        board_index = cfg.get("index") if role == "board" else None

        event = RFIDEvent(
            tag_id=uid,
            card=card,
            reader_id=reader_id,
            role=role,
            seat=seat,
            timestamp=time.time(),
            raw_tag_id=uid,
            board_index=board_index,
        )
        self._queue.put(event)
        logger.debug(
            "RFIDEvent: reader=%s role=%s seat=%s board_index=%s tag=%s card=%r",
            reader_id, role, seat, board_index, uid, card,
        )
