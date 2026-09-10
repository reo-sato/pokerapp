"""rfid/bridge.py

PC/SC (pyscard) ラッパー。NFC リーダーからカード UID を読み取る低レベル層。

設計方針:
- pyscard は実行時に遅延インポートする。インポートできない環境でも
  このモジュール自体はインポート可能にする（テスト・GUI での import エラー防止）。
- UID 取得 APDU: FF CA 00 00 00 (ISO 7816 Get UID)
- レスポンス末尾 2 バイト: SW1=0x90, SW2=0x00 が成功を示す。
- **1 slot に複数枚**（席の hole card 2 枚重ね / flop 3 枚重ね）に対応する
  （契約 `docs/contracts/rfid-usb-ccid.md` v1.1 §6）。firmware は slot 上の ISO 15693 カードの
  8 バイト UID を枚数ぶん連結して返すので、host は応答長が 16/24/32 のときだけ 8 バイトずつ
  分割する（4/7/8 バイトは従来どおり単一 UID）。
"""
from __future__ import annotations

import logging
from typing import Optional

from rfid.card_master import bytes_to_tag_id

logger = logging.getLogger(__name__)

# UID 取得 APDU コマンド
_GET_UID_APDU = [0xFF, 0xCA, 0x00, 0x00, 0x00]
# 成功ステータスワード
_SW_OK = (0x90, 0x00)

# 契約 v1.1 §6: 8B UID × k 枚（k ≤ 4）の連結。この長さのときだけ複数枚として分割する。
# 8（1 枚）/ 4 / 7（ISO 14443A）は単一 UID として扱う（16=2 枚, 24=3 枚, 32=4 枚）。
UID_CHUNK_BYTES = 8
MULTI_UID_LENGTHS = (16, 24, 32)


def split_uid_response(data: bytes) -> list[str]:
    """Get UID 応答のデータ部を UID 文字列のリストにする（契約 v1.1 §6）。

    長さが 16/24/32 バイトのときのみ 8 バイトずつ分割し、それ以外（4/7/8 等）は単一 UID。
    空データは空リスト。
    """
    if not data:
        return []
    if len(data) in MULTI_UID_LENGTHS:
        return [
            bytes_to_tag_id(data[i:i + UID_CHUNK_BYTES])
            for i in range(0, len(data), UID_CHUNK_BYTES)
        ]
    return [bytes_to_tag_id(data)]


def bridge_read_uids(bridge: object) -> list[str]:
    """bridge から現在載っている UID を読む（旧 bridge 互換シム）。

    `read_uids()` を持つ bridge はそれを使い、`read_uid()` しか無い旧実装は 0/1 件に正規化する。
    """
    read_uids = getattr(bridge, "read_uids", None)
    if callable(read_uids):
        return [u for u in (read_uids() or []) if u]
    uid = bridge.read_uid()  # type: ignore[attr-defined]
    return [uid] if uid else []


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
        uids = bridge.read_uids()  # ["04:AB:CD:EF:12:34", ...]（重ね置きは複数件）
        uid = bridge.read_uid()    # "04:AB:CD:EF:12:34" or None（先頭 1 件・後方互換）
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

    def read_uids(self) -> list[str]:
        """現在このリーダーに載っている全カードの UID を正規化済み文字列のリストで返す。

        重ね置き（席 2 枚 / flop 3 枚）は firmware が 8B UID を連結して返すので、
        `split_uid_response` で分割する（契約 v1.1 §6）。カードなし・エラー時は空リスト。
        """
        if not self._connected or self._reader is None:
            return []
        try:

            conn = self._reader.createConnection()
            conn.connect()
            data, sw1, sw2 = conn.transmit(_GET_UID_APDU)
            conn.disconnect()

            if (sw1, sw2) != _SW_OK:
                logger.debug("UID APDU returned SW=%02X%02X", sw1, sw2)
                return []

            return split_uid_response(bytes(data))

        except Exception as e:
            # NoCardException も含むすべての例外を統一処理
            # (カード未タッチは正常状態なのでログレベルは debug)
            logger.debug("read_uids on %r: %s", self._reader_name, e)
            return []

    def read_uid(self) -> Optional[str]:
        """現在タッチされているカードの UID を 1 件だけ返す（後方互換の薄いラッパ）。

        重ね置きのときは先頭 1 件のみ。全件が要るときは `read_uids()` を使う。
        カードがなければ None。エラー時も None。
        """
        uids = self.read_uids()
        return uids[0] if uids else None

    def close(self) -> None:
        """リーダーとの接続をクリーンアップする。"""
        self._connected = False
        self._reader = None
        self._connection = None


class MockPCSCBridge:
    """テスト・デモ用のモック PC/SC ブリッジ。

    uid_sequence の各要素は 1 poll ぶんの状態で、次のいずれでもよい:
      - `str`       … その UID 1 枚
      - `None`      … カードなし
      - `list[str]` … 重ね置き（複数枚, 契約 v1.1 §6）
    `read_uids()` は常に list、`read_uid()` は先頭 1 件 or None を返す。
    """

    def __init__(
        self,
        reader_name: str,
        uid_sequence: list[Optional[str] | list[str]] | None = None,
    ) -> None:
        self._reader_name = reader_name
        self._sequence = list(uid_sequence or [])
        self._index = 0

    @property
    def reader_name(self) -> str:
        return self._reader_name

    def connect(self) -> bool:
        return True

    def read_uids(self) -> list[str]:
        if not self._sequence:
            return []
        # シーケンス終端に達したら最後の値を返し続ける（循環しない）
        idx = min(self._index, len(self._sequence) - 1)
        entry = self._sequence[idx]
        if self._index < len(self._sequence) - 1:
            self._index += 1
        if entry is None:
            return []
        if isinstance(entry, str):
            return [entry]
        return [u for u in entry if u]

    def read_uid(self) -> Optional[str]:
        uids = self.read_uids()
        return uids[0] if uids else None

    def close(self) -> None:
        pass
