"""rfid/bridge.py

PC/SC (pyscard) ラッパー。NFC リーダーからカード UID を読み取る低レベル層。

設計方針:
- pyscard は実行時に遅延インポートする。インポートできない環境でも
  このモジュール自体はインポート可能にする（テスト・GUI での import エラー防止）。
- UID 取得 APDU: `FF CA 00 <k> 00`（ISO 7816 Get UID）。**P2 = 物理 reader index k**
  （契約 `docs/contracts/rfid-usb-ccid.md` v1.2 §6 / ADR-0041）。Windows の汎用 CCID ドライバは
  1 インターフェース 1 slot しか公開しないため、PC/SC の reader（slot）は 1 つだけにして、
  物理リーダー N 台は P2 で選ぶ。`k=0` は v1.0/1.1 の `FF CA 00 00 00` と同一。
- レスポンス末尾 2 バイト: SW1=0x90, SW2=0x00 が成功を示す。`6A 86` = P2 が firmware の
  台数を超えている（config の `reader` が誤り）。
- **1 reader に複数枚**（席の hole card 2 枚重ね / flop 3 枚重ね）に対応する
  （契約 v1.1 §6）。firmware は該当 reader 上の ISO 15693 カードの 8 バイト UID を
  枚数ぶん連結して返すので、host は応答長が 16/24/32 のときだけ 8 バイトずつ
  分割する（4/7/8 バイトは従来どおり単一 UID）。
- **接続は reader_name ごとに 1 本を持続**させる（ADR-0040 で slot は仮想カード常時挿入なので
  接続は切れない / ADR-0041 で 1 接続に N 個の APDU を流す）。同じ reader_name を使う複数の
  `PCSCBridge` はモジュール内の共有接続（参照カウント）を使い、transmit を lock で直列化する。
"""
from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

from rfid.card_master import bytes_to_tag_id

logger = logging.getLogger(__name__)

# 成功ステータスワード
_SW_OK = (0x90, 0x00)
# P2（物理 reader index）が firmware の台数を超えている（契約 v1.2 §6）
_SW_WRONG_P1P2 = (0x6A, 0x86)

# 物理 reader 台数の問い合わせ（契約 v1.2 §6）: `FF CA 00 FF 00` → <N> + 90 00。
READER_COUNT_P2 = 0xFF
_READER_COUNT_APDU = [0xFF, 0xCA, 0x00, READER_COUNT_P2, 0x00]

# reader_index として指定できる最大値（255=0xFF は台数問い合わせ用に予約）。
MAX_READER_INDEX = 0xFE

# 契約 v1.1 §6: 8B UID × k 枚（k ≤ 4）の連結。この長さのときだけ複数枚として分割する。
# 8（1 枚）/ 4 / 7（ISO 14443A）は単一 UID として扱う（16=2 枚, 24=3 枚, 32=4 枚）。
UID_CHUNK_BYTES = 8
MULTI_UID_LENGTHS = (16, 24, 32)


def get_uid_apdu(reader_index: int = 0) -> list[int]:
    """物理 reader `reader_index` に対する Get UID APDU（契約 v1.2 §6）。"""
    return [0xFF, 0xCA, 0x00, reader_index, 0x00]


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


def call_bridge_factory(factory: Callable, reader_name: str, reader_index: int = 0) -> object:
    """bridge factory を `(reader_name, reader_index)` で呼ぶ（旧 1 引数 factory 互換）。

    既存の注入 factory（`lambda name: ...`）は `reader_index` を受けないので `TypeError` を
    捕まえて 1 引数で呼び直す。factory 内部の `TypeError` と区別できないため、2 引数呼び出しが
    TypeError になったときだけ 1 引数で再試行する（呼び直しでも失敗すれば例外はそのまま）。
    """
    try:
        return factory(reader_name, reader_index)
    except TypeError:
        return factory(reader_name)


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


def find_reader(reader_name: str):
    """接続中の PC/SC reader のうち reader_name と**等値**のものを返す（無ければ None）。"""
    from smartcard.System import readers as sc_readers
    return next((r for r in sc_readers() if str(r) == reader_name), None)


# ――― reader_name ごとの共有接続（契約 v1.2 §8 / ADR-0041） ―――

class _SharedConnection:
    """1 つの reader_name に対する PC/SC 接続 1 本（複数 `PCSCBridge` で共有）。

    ADR-0040 で slot は仮想カード常時挿入になったため、接続は poll ごとに張り直さず持続させる
    （11 台ぶんの Get UID を 1 接続で流す）。USB 抜け等で transmit が失敗したら `invalidate()`
    して次回の transmit で張り直す。
    """

    def __init__(self, reader_name: str) -> None:
        self.reader_name = reader_name
        self.refs = 0
        self.lock = threading.RLock()
        self._conn = None

    def transmit(self, reader, apdu: list[int]):
        """APDU を送る（未接続なら connect してから）。呼び出しは lock で直列化される。"""
        with self.lock:
            if self._conn is None:
                conn = reader.createConnection()
                conn.connect()
                self._conn = conn
            return self._conn.transmit(apdu)

    def invalidate(self) -> None:
        """接続を破棄する（次回 transmit で再接続）。"""
        with self.lock:
            conn, self._conn = self._conn, None
        if conn is None:
            return
        try:
            conn.disconnect()
        except Exception as e:  # 切断済み USB 等。破棄が目的なので握りつぶす
            logger.debug("disconnect on %r: %s", self.reader_name, e)


_shared_connections: dict[str, _SharedConnection] = {}
_shared_registry_lock = threading.Lock()


def acquire_shared_connection(reader_name: str) -> _SharedConnection:
    """reader_name の共有接続を取得する（参照カウント +1）。"""
    with _shared_registry_lock:
        shared = _shared_connections.get(reader_name)
        if shared is None:
            shared = _SharedConnection(reader_name)
            _shared_connections[reader_name] = shared
        shared.refs += 1
        return shared


def release_shared_connection(shared: _SharedConnection) -> None:
    """共有接続を返す（参照カウント -1。0 で切断して登録を落とす）。"""
    with _shared_registry_lock:
        shared.refs -= 1
        if shared.refs > 0:
            return
        if _shared_connections.get(shared.reader_name) is shared:
            del _shared_connections[shared.reader_name]
    shared.invalidate()


def query_reader_count(reader_name: str) -> Optional[int]:
    """firmware が公開する**物理 reader 台数 N** を問い合わせる（契約 v1.2 §6）。

    `FF CA 00 FF 00` に 1 バイト + `90 00` が返れば int、それ以外（旧 v1.1 firmware は
    `6A 81` / `6D 00` を返す）や例外は None。
    """
    try:
        reader = find_reader(reader_name)
        if reader is None:
            return None
        shared = acquire_shared_connection(reader_name)
        try:
            data, sw1, sw2 = shared.transmit(reader, list(_READER_COUNT_APDU))
        except Exception:
            shared.invalidate()   # 壊れた接続を他の持ち主に残さない
            raise
        finally:
            release_shared_connection(shared)
        if (sw1, sw2) != _SW_OK or len(data) != 1:
            logger.debug(
                "reader count query on %r: SW=%02X%02X len=%d（v1.1 firmware は非対応）",
                reader_name, sw1, sw2, len(data),
            )
            return None
        return int(data[0])
    except Exception as e:
        logger.debug("query_reader_count on %r: %s", reader_name, e)
        return None


class PCSCBridge:
    """単一 PC/SC リーダー上の**物理 reader 1 台**との接続を管理する。

    使い方:
        bridge = PCSCBridge("PokerRFID PN5180-CCID 0", reader_index=3)
        uids = bridge.read_uids()  # ["04:AB:CD:EF:12:34", ...]（重ね置きは複数件）
        uid = bridge.read_uid()    # "04:AB:CD:EF:12:34" or None（先頭 1 件・後方互換）
        bridge.close()

    `reader_index`（= Get UID の P2）は 0..254。同じ reader_name の bridge は PC/SC 接続を
    共有する（接続本数を reader 名ごとに 1 本に保つ, ADR-0041）。
    """

    def __init__(self, reader_name: str, reader_index: int = 0) -> None:
        if isinstance(reader_index, bool) or not isinstance(reader_index, int):
            raise ValueError(f"reader_index must be int 0..{MAX_READER_INDEX}: {reader_index!r}")
        if not 0 <= reader_index <= MAX_READER_INDEX:
            raise ValueError(
                f"reader_index must be 0..{MAX_READER_INDEX} "
                f"(0xFF は台数問い合わせ用に予約): {reader_index!r}"
            )
        self._reader_name = reader_name
        self._reader_index = reader_index
        self._apdu = get_uid_apdu(reader_index)
        self._reader = None
        self._shared: Optional[_SharedConnection] = None
        self._connected = False
        self._warned_wrong_index = False
        self._last_failure: Optional[str] = None

    @property
    def reader_name(self) -> str:
        return self._reader_name

    @property
    def reader_index(self) -> int:
        return self._reader_index

    def connect(self) -> bool:
        """リーダーに接続する。成功時 True、失敗時 False。"""
        try:
            reader = find_reader(self._reader_name)
            if reader is None:
                logger.warning("Reader not found: %r", self._reader_name)
                return False
            self._reader = reader
            self._connected = True
            self._acquire()
            logger.info(
                "Connected to reader: %s (physical reader %d)",
                self._reader_name, self._reader_index,
            )
            return True
        except ImportError:
            logger.debug("pyscard not installed.")
            return False
        except Exception as e:
            logger.warning("Failed to connect to reader %r: %s", self._reader_name, e)
            return False

    def _acquire(self) -> _SharedConnection:
        if self._shared is None:
            self._shared = acquire_shared_connection(self._reader_name)
        return self._shared

    def _transmit(self, apdu: list[int]):
        """共有接続で APDU を 1 回送る（失敗時は接続を破棄して例外を上げる）。"""
        shared = self._acquire()
        try:
            return shared.transmit(self._reader, apdu)
        except Exception:
            # USB 抜け / 再列挙 / 占有など。接続を捨てて次回 poll で張り直す。
            shared.invalidate()
            raise

    def probe(self) -> Optional[tuple[int, int]]:
        """Get UID を 1 回だけ送り SW を返す（診断用。送れなければ None）。"""
        if not self._connected or self._reader is None:
            return None
        try:
            _data, sw1, sw2 = self._transmit(self._apdu)
        except Exception as e:
            logger.debug("probe on %r r%d: %s", self._reader_name, self._reader_index, e)
            return None
        return (sw1, sw2)

    def read_uids(self) -> list[str]:
        """この物理 reader に載っている全カードの UID を正規化済み文字列のリストで返す。

        重ね置き（席 2 枚 / flop 3 枚）は firmware が 8B UID を連結して返すので、
        `split_uid_response` で分割する（契約 v1.1 §6）。カードなし・エラー時は空リスト。
        """
        if not self._connected or self._reader is None:
            return []
        try:
            data, sw1, sw2 = self._transmit(self._apdu)
        except Exception as e:
            # NoCardException も含むすべての例外を統一処理
            # (カード未タッチは正常状態なのでログレベルは debug。同一原因の連打は 1 回だけ出す)
            self._log_failure_once(f"read_uids: {e}")
            return []

        if (sw1, sw2) == _SW_WRONG_P1P2:
            # P2 が firmware の台数を超えている = config の `reader` が誤り（契約 v1.2 §6）。
            if not self._warned_wrong_index:
                logger.warning(
                    "Get UID SW=6A86 on %r: 物理 reader %d は firmware の範囲外です"
                    "（config.rfid.pcsc_readers[].reader を確認, 契約 v1.2 §6）",
                    self._reader_name, self._reader_index,
                )
                self._warned_wrong_index = True
            return []

        if (sw1, sw2) != _SW_OK:
            self._log_failure_once(f"UID APDU returned SW={sw1:02X}{sw2:02X}")
            return []

        self._last_failure = None
        return split_uid_response(bytes(data))

    def _log_failure_once(self, message: str) -> None:
        """同一内容の失敗が続く間はログを 1 回だけ出す（100ms poll の連打を避ける）。"""
        if message == self._last_failure:
            return
        self._last_failure = message
        logger.debug("%r r%d: %s", self._reader_name, self._reader_index, message)

    def read_uid(self) -> Optional[str]:
        """現在タッチされているカードの UID を 1 件だけ返す（後方互換の薄いラッパ）。

        重ね置きのときは先頭 1 件のみ。全件が要るときは `read_uids()` を使う。
        カードがなければ None。エラー時も None。
        """
        uids = self.read_uids()
        return uids[0] if uids else None

    def close(self) -> None:
        """リーダーとの接続をクリーンアップする（共有接続は最後の 1 本で切断）。"""
        self._connected = False
        self._reader = None
        shared, self._shared = self._shared, None
        if shared is not None:
            release_shared_connection(shared)


class MockPCSCBridge:
    """テスト・デモ用のモック PC/SC ブリッジ。

    uid_sequence の各要素は 1 poll ぶんの状態で、次のいずれでもよい:
      - `str`       … その UID 1 枚
      - `None`      … カードなし
      - `list[str]` … 重ね置き（複数枚, 契約 v1.1 §6）
    `read_uids()` は常に list、`read_uid()` は先頭 1 件 or None を返す。
    `reader_index`（契約 v1.2 §6 の P2）は受け取るだけで使わない。
    """

    def __init__(
        self,
        reader_name: str,
        uid_sequence: list[Optional[str] | list[str]] | None = None,
        reader_index: int = 0,
    ) -> None:
        self._reader_name = reader_name
        self._reader_index = reader_index
        self._sequence = list(uid_sequence or [])
        self._index = 0

    @property
    def reader_name(self) -> str:
        return self._reader_name

    @property
    def reader_index(self) -> int:
        return self._reader_index

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
