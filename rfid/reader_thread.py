"""rfid/reader_thread.py

Phase 6: RFID リーダーをポーリングして RFIDEvent を rfid_queue に投入するスレッド。

設計方針:
- 各リーダーを poll_interval_ms 間隔でポーリングする。
- **1 リーダーに複数枚**（席 = hole card 2 枚重ね / board1 = flop 3 枚重ね, 契約 v1.1 §6）を
  想定し、bridge からは UID の**集合**を読む（`read_uids()`）。
- デバウンス: リーダーごとに前回の UID 集合を保持し、**新しく増えた UID だけ** RFIDEvent を
  1 件ずつ投入する。置きっぱなしは再発火しない。外れた UID は状態更新のみ（イベントなし）で、
  外して再度置けば同じ UID がもう一度発火する（契約 §8 を UID 単位に拡張）。
- reader_configs: [{"name": "...", "role": "seat", "seat": 1}, ...] の形式。
  role="board" は `index`（ボード位置 1..5, 任意）と `cards`（そのリーダーに重ねる枚数, 任意・既定 1）
  を持つ。複数枚の board reader では検出順に `index + offset` を割り当てる（契約 v1.1 §4）。

設定例 (config.json, 本番 11 slot = 席 8 + board 3):
    "rfid": {
      "enabled": true,
      "transport": "pcsc",
      "poll_interval_ms": 100,
      "pcsc_readers": [
        {"name": "PokerRFID PN5180-CCID 0", "role": "seat", "seat": 1},
        {"name": "PokerRFID PN5180-CCID 8", "role": "board", "index": 1, "cards": 3},
        {"name": "PokerRFID PN5180-CCID 9", "role": "board", "index": 4},
        {"name": "PokerRFID PN5180-CCID 10", "role": "board", "index": 5}
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
from rfid.bridge import PCSCBridge, bridge_read_uids
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
                              {"name": str, "role": "seat"|"board", "seat": int (roleが"seat"の場合),
                               "index": int (role="board", 任意), "cards": int (role="board", 任意・既定 1)}
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

        # デバウンス用: reader_id → 現在載っている UID の集合（空 = カードなし）
        self._last_uids: dict[str, set[str]] = {}
        # board 位置割り当て: reader_id → {uid: offset}（現在 slot を占有している UID）
        self._board_offsets: dict[str, dict[str, int]] = {}
        # 同上の記憶: 一度外れた UID が「前と同じ位置」に戻れるように offset を覚えておく
        self._board_offset_memory: dict[str, dict[str, int]] = {}

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
                self._last_uids[reader_id] = set()
                self._board_offsets[reader_id] = {}
                self._board_offset_memory[reader_id] = {}
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
        """1 リーダーをポーリングし、**新しく増えた UID ごとに** RFIDEvent を投入する。

        重ね置き（席 2 枚 / flop 3 枚）に対応するため、状態は UID 1 個ではなく集合で持つ。
        """
        uids = bridge_read_uids(bridge)   # 旧 bridge（read_uid のみ）互換シム
        current: set[str] = set(uids)
        prev = self._last_uids.get(reader_id, set())

        # デバウンス: 集合が前回と同じなら何もしない
        if current == prev:
            return

        self._last_uids[reader_id] = current

        removed = prev - current
        if removed:
            # カードが外れた → 位置を解放するだけ（イベント不要。以前の offset は記憶に残す）
            self._release_board_offsets(reader_id, removed)
            logger.debug("Card(s) removed from %s: %s", reader_id, sorted(removed))

        # 新規タッチ検出（読み取り順を保ったまま、増えた UID ごとに 1 event）
        seen: set[str] = set()
        for uid in uids:
            if uid in prev or uid in seen:
                continue
            seen.add(uid)
            self._emit_event(cfg, reader_id, uid)

    def _emit_event(self, cfg: dict, reader_id: str, uid: str) -> None:
        """新規検出 UID 1 件を RFIDEvent にして投入する。"""
        card = self._card_master.lookup(uid)
        role = cfg.get("role", "seat")
        seat = cfg.get("seat") if role == "seat" else None
        # board_index は board street 自動遷移に必須（engine が board_index!=None を分岐条件にする,
        # engine.py:294）。重ね置き対応で cfg["index"] + 割り当て offset にする（契約 v1.1 §4）。
        board_index = self._board_index_for(cfg, reader_id, uid) if role == "board" else None

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

    # ――― board の位置割り当て（契約 v1.1 §4） ―――

    def _board_index_for(self, cfg: dict, reader_id: str, uid: str) -> Optional[int]:
        """board reader 上の新規 UID にボード位置（1..5）を割り当てる。

        `index` が無い board reader は従来どおり None（engine は末尾に追記）。
        `cards`（既定 1）ぶんの offset を持ち、UID には「前回使っていた offset が空いていれば
        それ、無ければ最小の空き offset」を与える（外して戻すと同じ位置に戻る）。
        空きが無い（cards 超過）ときは WARN して None を返す。
        """
        base = cfg.get("index")
        if base is None:
            return None

        capacity = cfg.get("cards", 1)
        if not isinstance(capacity, int) or isinstance(capacity, bool) or capacity < 1:
            logger.warning(
                "Invalid 'cards' %r on %s — 1 として扱います", capacity, reader_id,
            )
            capacity = 1

        assigned = self._board_offsets.setdefault(reader_id, {})
        memory = self._board_offset_memory.setdefault(reader_id, {})
        taken = set(assigned.values())

        offset = memory.get(uid)
        if offset is None or offset in taken or offset >= capacity:
            offset = next((o for o in range(capacity) if o not in taken), None)

        if offset is None:
            logger.warning(
                "Board reader %s: cards=%d を超えるカード（tag=%s）— board_index なしで記録します",
                reader_id, capacity, uid,
            )
            return None

        assigned[uid] = offset
        memory[uid] = offset
        return base + offset

    def _release_board_offsets(self, reader_id: str, removed: set[str]) -> None:
        """外れた UID の board 位置を解放する（memory には残すので戻せば同じ位置）。"""
        assigned = self._board_offsets.get(reader_id)
        if not assigned:
            return
        for uid in removed:
            assigned.pop(uid, None)
