from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

# numpy は CameraEvent.frame の型注釈だけで使う。`from __future__ import annotations` により
# 注釈は実行時評価されない文字列なので、実行時に numpy を import する必要はない。TYPE_CHECKING
# ガードに入れることで、RFID canonical 経路（rfid.reader_thread / tools/probe_pcsc.py watch）が
# numpy 未導入の最小環境（pyscard だけ）でも動く。numpy が要るのは camera(legacy)/audio 経路のみ。
if TYPE_CHECKING:
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
    # 以下は additive (R3/R4 用)。既存経路は未使用 = 挙動不変。
    seat: Optional[int] = None        # 明示発話された席番号（"シート3"）。actor 推定/replay 用
    confidence: Optional[float] = None  # Whisper per-segment 信頼度 [0,1]（派生 confidence の入力）
