from __future__ import annotations

import warnings as _warnings
from dataclasses import dataclass
from typing import Optional


@dataclass
class RFIDEvent:
    """RFID リーダースレッドが検出したカードタッチイベント（PC/SC 形式・旧仕様）。

    spec.md v4.0 以降の HTTP 形式イベントは rfid/events.py の RFIDEvent を使用する。
    テストでは `from core.events import RFIDEvent as CoreRFIDEvent` としてインポート可能。
    """

    tag_id: str                   # リーダーから受け取った UID（正規化済み）
    card: str                     # "Ah", "Kd" など（カードマスター未登録時は空文字）
    reader_id: str                # "seat_1", "board_3" など設定ファイルのキー
    role: str                     # "seat" | "board"
    seat: Optional[int]           # role="seat" 時の席番号、role="board" 時は None
    timestamp: float              # time.time()
    raw_tag_id: str               # デバッグ用の生タグ ID
    board_index: Optional[int] = None  # role="board" 時のボード位置 (1=flop1…5=river)


class CameraEvent:
    """Deprecated: camera removed in spec v4.0 (2026-04-09).

    既存コードの import を壊さないよう定義を残している。
    インスタンス化しようとすると TypeError を送出する。
    """

    def __new__(cls, *args, **kwargs):
        _warnings.warn(
            "CameraEvent is deprecated and removed in spec v4.0 (camera input abolished).",
            DeprecationWarning,
            stacklevel=2,
        )
        raise TypeError("CameraEvent is no longer supported. Remove all usages.")


@dataclass
class AudioEvent:
    """音声認識スレッドが検出したアクションイベント。"""

    action: str           # "bet"/"call"/"raise"/"check"/"fold"/"allin"/"showdown"/"winner"/"new_hand"
    amount: int | None    # 金額なしの場合は None
    timestamp: str        # ISO 8601 形式
    raw_text: str
    mentioned_seat: int | None = None      # 音声中の席番号言及（spec FR-26 boost用）
    mentioned_position: str | None = None  # 音声中のポジション言及（"BTN","UTG"等）
