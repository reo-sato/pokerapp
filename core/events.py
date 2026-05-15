from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np


@dataclass
class RFIDEvent:
    """RFID リーダースレッドが検出したカードタッチイベント。"""

    tag_id: str                  # リーダーから受け取った UID（正規化済み）
    card: str                    # "Ah", "Kd" など（カードマスター未登録時は空文字）
    reader_id: str               # "seat_1", "board_3" など設定ファイルのキー
    role: str                    # "seat" | "board"
    seat: Optional[int]          # role="seat" 時の席番号、role="board" 時は None
    timestamp: float             # time.time()
    raw_tag_id: str              # デバッグ用の生タグ ID
    board_index: Optional[int] = None  # role="board" 時のボード位置 (1=flop1…5=river)


@dataclass
class CameraEvent:
    """カメラスレッドが検出したチップ動作イベント。"""

    seat: int
    timestamp: float  # time.time()
    frame: Optional[np.ndarray] = field(default=None, repr=False)  # Phase 2 以降で使用


@dataclass
class AudioEvent:
    """音声認識スレッドが検出したアクションイベント。"""

    action: str  # "bet"/"call"/"raise"/"check"/"fold"/"allin"/"showdown"/"winner"/"new_hand"
    amount: int  # 金額なしの場合は 0
    timestamp: float  # time.time()
    raw_text: str
    # 音声に席番号言及があれば設定 (state-aware 推定で seat 矛盾検出に使う)。
    seat: Optional[int] = None
    # ハンド開始時の button seat 指定など、追加メタデータ用。
    metadata: Optional[dict] = None
