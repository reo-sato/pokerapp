"""rfid/bridge.py

PC/SC (pyscard) ラッパー。NFC リーダーからカード UID を読み取る低レベル層。

設計方針:
- pyscard は実行時に遅延インポートする。インポートできない環境でも
  このモジュール自体はインポート可能にする（テスト・GUI での import エラー防止）。
- UID 取得 APDU: FF CA 00 00 00 (ISO 7816 Get UID)
- レスポンス末尾 2 バイト: SW1=0x90, SW2=0x00 が成功を示す。
"""
from __future__ import annotations

import logging
from typing import Optional

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
