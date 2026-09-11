"""rfid/reader_thread.py

Phase 6: RFID リーダーをポーリングして RFIDEvent を rfid_queue に投入するスレッド。

設計方針:
- 各リーダーを poll_interval_ms 間隔でポーリングする。
- **1 リーダーに複数枚**（席 = hole card 2 枚重ね / board = 1 台に 1〜3 枚, 契約 v1.1 §6）を
  想定し、bridge からは UID の**集合**を読む（`read_uids()`）。
- デバウンス: リーダーごとに前回の UID 集合を保持し、**新しく増えた UID だけ** RFIDEvent を
  1 件ずつ投入する。置きっぱなしは再発火しない。外れた UID は状態更新のみ（イベントなし）で、
  外して再度置けば同じ UID がもう一度発火する（契約 §8 を UID 単位に拡張）。
- reader_configs: [{"name": "...", "reader": 0, "role": "seat", "seat": 1}, ...] の形式。
  `reader`（任意・既定 0）は **物理リーダーの index**（Get UID の P2, 契約 v1.2 §6 / ADR-0041）。
  Windows の汎用 CCID ドライバは 1 インターフェース 1 slot しか公開しないため、PC/SC reader
  （`name`）は 1 つで、物理リーダー N 台は `reader` で選ぶ。`(name, reader)` の組で一意。
  role="board" は **役割だけ**を書く（位置は書かない）。ボード領域には board reader が N 台
  並んでいるだけで、どの台がどのストリートを受けるかは置き方次第（flop 3 枚が 3 台に散ることも、
  真ん中の 1 台に 2 枚載ることもある）。よって **board reader 全台を 1 つの論理ボード**として扱い、
  `board_index`（1..5）は **全台を通した検出順** = ディーラーが配った順で決める（契約 v1.3 §4 /
  ADR-0042）。旧 config の `index` / `cards` は廃止（あれば WARN して無視）。

設定例 (config.json, 本番 11 台 = 席 8 + board 3。reader 名は 1 つだけ):
    "rfid": {
      "enabled": true,
      "transport": "pcsc",
      "poll_interval_ms": 100,
      "pcsc_readers": [
        {"name": "PokerRFID PN5180-CCID 0", "reader": 0,  "role": "seat", "seat": 1},
        {"name": "PokerRFID PN5180-CCID 0", "reader": 8,  "role": "board"},
        {"name": "PokerRFID PN5180-CCID 0", "reader": 9,  "role": "board"},
        {"name": "PokerRFID PN5180-CCID 0", "reader": 10, "role": "board"}
      ],
      "card_master_file": "./rfid_cards.json"
    }
board reader は **左から右の順に並べて書く**（同じ poll で 2 枚以上増えたときの位置順が
config の記載順で決まるため。1 台に複数枚載ったぶんの左右順は UID 順で不定 = §4 の既知の制約）。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from core.event_queue import EventQueue
from core.events import RFIDEvent
from rfid.bridge import MAX_READER_INDEX, PCSCBridge, bridge_read_uids, call_bridge_factory
from rfid.card_master import CardMaster

logger = logging.getLogger(__name__)

# コミュニティカードの最大枚数（flop 3 + turn + river）。board 位置は 1..5。
_BOARD_MAX_CARDS = 5


def _reader_index_of(cfg: dict, position: int) -> int:
    """config 要素の物理 reader index（Get UID の P2, 契約 v1.2 §6）。既定 0。

    不正値（int でない / 範囲外）は WARN して 0 にフォールバックする（起動は落とさない）。
    """
    value = cfg.get("reader", 0)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_READER_INDEX:
        logger.warning(
            "Invalid 'reader' %r on pcsc_readers[%d] — 0 として扱います（0..%d, 契約 v1.2 §4）",
            value, position, MAX_READER_INDEX,
        )
        return 0
    return value


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
                              {"name": str, "reader": int (任意・既定 0 = Get UID の P2, 契約 v1.2 §6),
                               "role": "seat"|"board", "seat": int (roleが"seat"の場合)}
                              role="board" に位置指定は無い（全台を通した検出順で 1..5 を振る,
                              契約 v1.3 §4）。board reader は左から右の順に並べて書く。
            poll_interval_ms: ポーリング間隔 (ミリ秒)。
            stop_event:       セット時にスレッドを停止する。
            bridge_factory:   テスト用ブリッジファクトリ (reader_name: str, reader_index: int) -> bridge。
                              旧シグネチャ (reader_name) -> bridge も互換で受け付ける。
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
        # board 位置割り当て（**board reader 全台で 1 つの論理ボードを共有**, 契約 v1.3 §4）:
        # uid → board_index 1..5。どの台に載ったかではなく「ボード全体で何枚目か」で決まる。
        self._board_index_by_uid: dict[str, int] = {}
        # 同上の記憶: 一度外れた UID が「前と同じ位置」に戻れるように覚えておく。
        # ボードが 0 枚になった時点でクリアする（= ハンドの切れ目）。
        self._board_index_memory: dict[str, int] = {}

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        logger.info("RFIDThread started (%d reader(s))", len(self._reader_configs))

        # ブリッジを初期化
        bridges: dict[str, object] = {}
        for i, cfg in enumerate(self._reader_configs):
            reader_name = cfg.get("name", "")
            reader_index = _reader_index_of(cfg, i)
            reader_id = f"reader_{i}"
            bridge = call_bridge_factory(self._bridge_factory, reader_name, reader_index)
            if bridge.connect():
                bridges[reader_id] = (bridge, cfg)
                self._last_uids[reader_id] = set()
                if cfg.get("role") == "board":
                    self._warn_obsolete_board_fields(cfg, reader_id)
                logger.info(
                    "RFID reader ready: %s (reader %d, %s)", reader_name, reader_index, reader_id,
                )
            else:
                logger.warning(
                    "Could not connect to RFID reader: %s (reader %d)", reader_name, reader_index,
                )

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
            # カードが外れた → 位置を解放するだけ（イベント不要。以前の位置は記憶に残す）
            self._release_board_indexes(removed)
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
        # engine.py:294）。**board reader 全台で共有する論理ボードの「何枚目か」**（契約 v1.3 §4）。
        board_index = self._assign_board_index(uid) if role == "board" else None

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

    # ――― board の位置割り当て（契約 v1.3 §4: board reader 全台で 1 つの論理ボード） ―――

    def _assign_board_index(self, uid: str) -> Optional[int]:
        """新規 board UID に **ボード全体での位置**（1..5）を割り当てる。

        物理配置は「ボード領域に board reader が N 台並んでいるだけ」で、どの台がどの
        ストリートを受けるかは **置き方次第**（flop 3 枚が 3 台に散ることも、真ん中の 1 台に
        2 枚載ることもある）。よって位置は reader ごとの固定 offset ではなく
        **全 board reader を通した検出順**（= ディーラーが配った順）で決める。

        - 空き位置のうち最小を与える。前回と同じ UID には**覚えていた位置**を優先して返す
          （1 枚だけ浮かせて戻してもボードの並びが変わらない）。
        - 5 枚を超えたら WARN + None（engine は末尾に追記する）。
        - **ボードが 0 枚になったら記憶をクリア**する（= ハンドの切れ目。次の flop 1 枚目が
          前ハンドの位置を引き継がないようにする）。
        """
        taken = set(self._board_index_by_uid.values())

        index = self._board_index_memory.get(uid)
        if index is None or index in taken:
            index = next((i for i in range(1, _BOARD_MAX_CARDS + 1) if i not in taken), None)

        if index is None:
            logger.warning(
                "ボードが %d 枚を超えました（tag=%s）— board_index なしで記録します",
                _BOARD_MAX_CARDS, uid,
            )
            return None

        self._board_index_by_uid[uid] = index
        self._board_index_memory[uid] = index
        return index

    def _release_board_indexes(self, removed: set[str]) -> None:
        """外れた UID のボード位置を解放する（memory には残すので戻せば同じ位置）。"""
        for uid in removed:
            self._board_index_by_uid.pop(uid, None)
        if not self._board_index_by_uid and self._board_index_memory:
            # ボードが空 = ハンドの切れ目。次のハンドは 1 枚目から数え直す。
            self._board_index_memory.clear()
            logger.debug("ボードが空になりました — board 位置の記憶をクリア")

    def _warn_obsolete_board_fields(self, cfg: dict, reader_id: str) -> None:
        """旧 config（board reader ごとの `index` / `cards`）を使っていたら一度だけ警告する。

        v1.1/v1.2 は「1 台 = 1 ストリート専用（flop は 1 台に 3 枚重ね）」前提だったが、
        実機は 3 台が並んでいるだけなので前提が成立しない（ISSUE-0024 / ADR-0042）。
        現在は全台を 1 つの論理ボードとして検出順に 1..5 を振るため、両フィールドは無視する。
        """
        stale = [k for k in ("index", "cards") if k in cfg]
        if stale:
            logger.warning(
                "%s: board reader の %s は廃止されました（無視します）。ボード位置は "
                "board reader 全台を通した検出順で決まります（ADR-0042）。config から削除してください",
                reader_id, " / ".join(stale),
            )
