"""rfid/bridge.py

spec.md v4.0:
  - RFIDThread: Flask HTTP サーバー経由で ESP32 イベントを受信し event_queue へ送出する。
  - PCSCBridge / MockPCSCBridge: PC/SC 方式（旧仕様、後方互換のため保持）。

RFIDThread を使う場合は rfid/server.py の RFIDFlaskServer を内部で起動する。
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

from core.event_queue import EventQueue
from rfid.card_master import CardMaster
from rfid.server import RFIDFlaskServer

logger = logging.getLogger(__name__)


class RFIDThread(threading.Thread):
    """spec.md v4.0: Flask HTTP サーバー経由で ESP32 イベントを受信し
    event_queue へ送出するスレッド（spec.md FR-06–FR-12）。

    スレッド構成上の責務:
    - RFIDFlaskServer を起動し ESP32 POST を受信する
    - tag_id → card_code を CardMaster で解決する
    - seat 用リーダーの fold_absent_threshold 連続未検出でフォールドイベントを発火する
    - board 用リーダーのカード枚数変化でストリート遷移イベントを発火する
    - active_seats を更新する
    """

    _event_queue: EventQueue
    _card_master: CardMaster
    _server: RFIDFlaskServer
    _absent_counts: dict[str, int]   # reader_id → 連続未検出回数
    _fold_threshold: int
    _stop_event: threading.Event
    _readers_config: dict            # config["rfid"]["readers"]

    def __init__(
        self,
        event_queue: EventQueue,
        card_master: CardMaster,
        host: str,
        port: int,
        fold_absent_threshold: int,
        readers_config: dict,
        stop_event: Optional[threading.Event] = None,
    ) -> None: ...

    def run(self) -> None: ...

    def stop(self) -> None: ...

    def _on_rfid_event(self, event_data: dict) -> None:
        """RFIDFlaskServer から呼ばれるコールバック。event_type に応じてディスパッチ。"""
        ...

    def _handle_present(self, reader_id: str, tag_id: str, timestamp: str) -> None:
        # IMPORTANT: self._absent_counts[reader_id] = 0 でカウンタをリセットすること。
        # リセットしないと次ハンドのディール時に誤フォールドが発火する。
        ...

    def _handle_absent(self, reader_id: str, timestamp: str) -> None:
        # IMPORTANT: ショーダウンフェーズ中（state.street == "showdown"）は
        # フォールドイベントを発火しないこと（spec.md FR-11）。
        # _absent_counts が _fold_threshold を超えた場合のみ発火する。
        ...

    def _resolve_card(self, tag_id: str) -> Optional[str]:
        """tag_id → card_code に変換する。未登録の場合は None を返す。"""
        ...

    def _get_reader_config(self, reader_id: str) -> Optional[dict]:
        """readers_config から reader_id のエントリを返す。存在しない場合は None。"""
        ...


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
