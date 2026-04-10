"""rfid/bridge.py

spec.md v4.0:
  - RFIDThread: Flask HTTP サーバー経由で ESP32 イベントを受信し event_queue へ送出する。
  - PCSCBridge / MockPCSCBridge: PC/SC 方式（旧仕様、後方互換のため保持）。

RFIDThread を使う場合は rfid/server.py の RFIDFlaskServer を内部で起動する。
"""
from __future__ import annotations

import logging
import queue
import threading
from typing import Optional

from rfid.card_master import CardMaster
from rfid.server import RFIDFlaskServer

logger = logging.getLogger(__name__)

# ストリート遷移: ボードカード枚数 → ストリート名
_BOARD_COUNT_TO_STREET: dict[int, str] = {3: "flop", 4: "turn", 5: "river"}


class RFIDThread(threading.Thread):
    """spec.md v4.0: Flask HTTP サーバー経由で ESP32 イベントを受信し
    event_queue へ送出するスレッド（spec.md FR-06–FR-12）。

    スレッド構成上の責務:
    - RFIDFlaskServer を起動し ESP32 POST を受信する
    - tag_id → card_code を CardMaster で解決する
    - seat 用リーダーの fold_absent_threshold 連続未検出でフォールドイベントを発火する
    - board 用リーダーのカード枚数変化でストリート遷移イベントを発火する

    event_queue に put する dict のフォーマット:
        hole_card:  {"type": "hole_card",  "seat": int, "card_code": str,
                     "event_type": "present", "timestamp": str,
                     "reader_id": str, "tag_id": str}
        board_card: {"type": "board_card", "board_index": int, "card_code": str,
                     "event_type": "present", "timestamp": str, "reader_id": str}
        street:     {"type": "street", "street": "flop"|"turn"|"river", "timestamp": str}
        fold:       {"type": "fold", "seat": int, "timestamp": str, "reader_id": str}

    未登録タグは event_queue には送出せず、needs_review_items リストに追加する。
    """

    def __init__(
        self,
        event_queue: queue.Queue,
        card_master: CardMaster,
        host: str,
        port: int,
        fold_absent_threshold: int,
        readers_config: dict,
        stop_event: Optional[threading.Event] = None,
    ) -> None:
        super().__init__(name="RFIDThread", daemon=True)
        self._event_queue = event_queue
        self._card_master = card_master
        self._fold_threshold = fold_absent_threshold
        self._readers_config = readers_config
        self._stop_event = stop_event or threading.Event()
        self._server = RFIDFlaskServer(host, port, self._on_rfid_event)

        # 状態
        self._absent_counts: dict[str, int] = {}    # reader_id → 連続未検出回数
        self._board_positions: dict[str, str] = {}  # reader_id → card_code
        self._streets_emitted: set[str] = set()     # 発火済みストリート名
        self._current_street: str = "preflop"       # ショーダウンガード用

        # 未登録タグ蓄積（テスト・GUI レビュー用）
        self.needs_review_items: list[dict] = []

    def run(self) -> None:
        self._server.start()
        logger.info("RFIDThread running")
        self._stop_event.wait()
        self._server.stop()
        logger.info("RFIDThread stopped")

    def stop(self) -> None:
        """スレッドを停止する。"""
        self._stop_event.set()

    def set_street(self, street: str) -> None:
        """外部からストリートを更新する（ショーダウンガード用）。"""
        self._current_street = street
        logger.debug("RFIDThread street → %s", street)

    def reset_board(self) -> None:
        """ハンド終了時にボード状態をリセットする。"""
        self._board_positions.clear()
        self._streets_emitted.clear()
        logger.debug("RFIDThread board reset")

    # ── コールバック ──────────────────────────────────────────────────────────

    def _on_rfid_event(self, event_data: dict) -> None:
        """RFIDFlaskServer から呼ばれるコールバック。event_type に応じてディスパッチ。"""
        reader_id  = event_data["reader_id"]
        tag_id     = event_data.get("tag_id", "")
        timestamp  = event_data.get("timestamp", "")
        event_type = event_data["event_type"]

        if event_type == "present":
            self._handle_present(reader_id, tag_id, timestamp)
        else:
            self._handle_absent(reader_id, timestamp)

    # ── イベントハンドラ ──────────────────────────────────────────────────────

    def _handle_present(self, reader_id: str, tag_id: str, timestamp: str) -> None:
        # IMPORTANT: まず absent カウンタをリセットする。
        # リセットしないと次ハンドのディール時に誤フォールドが発火する。
        self._absent_counts[reader_id] = 0

        cfg = self._get_reader_config(reader_id)
        if cfg is None:
            logger.warning("Unknown reader_id: %s", reader_id)
            return

        card_code = self._resolve_card(tag_id)
        role = cfg.get("role", "")

        if card_code is None:
            # 未登録タグ → needs_review に追加（queue には送出しない）
            self.needs_review_items.append({
                "reader_id": reader_id,
                "tag_id":    tag_id,
                "timestamp": timestamp,
                "role":      role,
            })
            logger.warning("Unregistered tag %s at reader %s → needs_review", tag_id, reader_id)
            return

        if role == "seat":
            seat = cfg.get("seat")
            self._event_queue.put({
                "type":       "hole_card",
                "seat":       seat,
                "card_code":  card_code,
                "event_type": "present",
                "timestamp":  timestamp,
                "reader_id":  reader_id,
                "tag_id":     tag_id,
            })
            logger.info("hole_card seat=%s card=%s", seat, card_code)

        elif role == "board":
            board_index = cfg.get("index")
            # 同一リーダーの再検出は上書きして重複カウントを防ぐ
            self._board_positions[reader_id] = card_code
            self._event_queue.put({
                "type":        "board_card",
                "board_index": board_index,
                "card_code":   card_code,
                "event_type":  "present",
                "timestamp":   timestamp,
                "reader_id":   reader_id,
            })
            logger.info("board_card index=%s card=%s", board_index, card_code)
            self._check_street_transition(timestamp)

    def _handle_absent(self, reader_id: str, timestamp: str) -> None:
        # IMPORTANT: ショーダウンフェーズ中はフォールドイベントを発火しない（FR-11）。
        if self._current_street == "showdown":
            logger.debug("Absent at %s ignored (showdown phase)", reader_id)
            return

        cfg = self._get_reader_config(reader_id)
        if cfg is None:
            return

        role = cfg.get("role", "")

        if role == "board":
            # ボードカードが外れた場合は位置を削除
            self._board_positions.pop(reader_id, None)
            return

        if role != "seat":
            return

        count = self._absent_counts.get(reader_id, 0) + 1
        self._absent_counts[reader_id] = count
        logger.debug("Absent count %s: %d/%d", reader_id, count, self._fold_threshold)

        if count >= self._fold_threshold:
            seat = cfg.get("seat")
            # カウンタをリセットして二重発火を防ぐ
            self._absent_counts[reader_id] = 0
            self._event_queue.put({
                "type":      "fold",
                "seat":      seat,
                "timestamp": timestamp,
                "reader_id": reader_id,
            })
            logger.info("fold seat=%s (absent threshold %d reached)", seat, self._fold_threshold)

    # ── 内部ヘルパー ──────────────────────────────────────────────────────────

    def _check_street_transition(self, timestamp: str) -> None:
        """ボードカード枚数に応じてストリート遷移イベントを発火する。"""
        count = len(self._board_positions)
        street = _BOARD_COUNT_TO_STREET.get(count)
        if street and street not in self._streets_emitted:
            self._streets_emitted.add(street)
            self._event_queue.put({
                "type":      "street",
                "street":    street,
                "timestamp": timestamp,
            })
            logger.info("Street → %s (%d board cards)", street, count)

    def _resolve_card(self, tag_id: str) -> Optional[str]:
        """tag_id → card_code。未登録の場合は None。"""
        return self._card_master.resolve(tag_id)

    def _get_reader_config(self, reader_id: str) -> Optional[dict]:
        """readers_config から reader_id のエントリを返す。存在しない場合は None。"""
        return self._readers_config.get(reader_id)


# ── 以下は PC/SC 方式（旧仕様）。後方互換のため保持。 ──────────────────────
# spec.md v4.0 では transport="http" が標準。transport="pcsc" 時のみ参照する。

logger = logging.getLogger(__name__)

# UID 取得 APDU コマンド
_GET_UID_APDU = [0xFF, 0xCA, 0x00, 0x00, 0x00]
# 成功ステータスワード
_SW_OK = (0x90, 0x00)


def list_readers() -> list[str]:
    """接続中のすべての PC/SC リーダー名を返す。

    pyscard が未インストールまたはリーダーが接続されていない場合は空リストを返す。
    """
    try:
        from smartcard.System import readers as sc_readers
        return [str(r) for r in sc_readers()]
    except ImportError:
        logger.debug("pyscard not installed; no RFID readers available.")
        return []
    except Exception as e:
        logger.warning("Failed to list readers: %s", e)
        return []


class PCSCBridge:
    """単一 PC/SC リーダーとの接続を管理する。

    使い方:
        bridge = PCSCBridge("ACS ACR122U 0")
        uid = bridge.read_uid()   # "04:AB:CD:EF:12:34" or None
        bridge.close()
    """

    def __init__(self, reader_name: str) -> None:
        self._reader_name = reader_name
        self._connection = None
        self._reader = None
        self._connected = False

    @property
    def reader_name(self) -> str:
        return self._reader_name

    def connect(self) -> bool:
        """リーダーに接続する。成功時 True、失敗時 False。"""
        try:
            from smartcard.System import readers as sc_readers

            all_readers = sc_readers()
            matched = [r for r in all_readers if str(r) == self._reader_name]
            if not matched:
                logger.warning("Reader not found: %r", self._reader_name)
                return False
            self._reader = matched[0]
            self._connected = True
            logger.info("Connected to reader: %s", self._reader_name)
            return True
        except ImportError:
            logger.debug("pyscard not installed.")
            return False
        except Exception as e:
            logger.warning("Failed to connect to reader %r: %s", self._reader_name, e)
            return False

    def read_uid(self) -> Optional[str]:
        """現在タッチされているカードの UID を正規化済み文字列で返す。

        カードがなければ None。エラー時も None。
        """
        if not self._connected or self._reader is None:
            return None
        try:
            from smartcard.Exceptions import NoCardException

            conn = self._reader.createConnection()
            conn.connect()
            data, sw1, sw2 = conn.transmit(_GET_UID_APDU)
            conn.disconnect()

            if (sw1, sw2) != _SW_OK:
                logger.debug("UID APDU returned SW=%02X%02X", sw1, sw2)
                return None

            from rfid.card_master import bytes_to_tag_id
            return bytes_to_tag_id(bytes(data))

        except Exception as e:
            # NoCardException も含むすべての例外を統一処理
            # (カード未タッチは正常状態なのでログレベルは debug)
            logger.debug("read_uid on %r: %s", self._reader_name, e)
            return None

    def close(self) -> None:
        """リーダーとの接続をクリーンアップする。"""
        self._connected = False
        self._reader = None
        self._connection = None


class MockPCSCBridge:
    """テスト・デモ用のモック PC/SC ブリッジ。

    uid_sequence に UID 文字列 (or None) を渡すと read_uid() がその順に返す。
    """

    def __init__(self, reader_name: str, uid_sequence: list[Optional[str]] | None = None) -> None:
        self._reader_name = reader_name
        self._sequence = list(uid_sequence or [])
        self._index = 0

    @property
    def reader_name(self) -> str:
        return self._reader_name

    def connect(self) -> bool:
        return True

    def read_uid(self) -> Optional[str]:
        if not self._sequence:
            return None
        # シーケンス終端に達したら最後の値を返し続ける（循環しない）
        idx = min(self._index, len(self._sequence) - 1)
        uid = self._sequence[idx]
        if self._index < len(self._sequence) - 1:
            self._index += 1
        return uid

    def close(self) -> None:
        pass
