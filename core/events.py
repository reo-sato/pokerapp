from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np


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
