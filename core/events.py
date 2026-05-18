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
    t_end: Optional[float] = None      # ベイズ層 (v6.0+) 用: カード保持区間の終端絶対時刻 (fold-on-release)


@dataclass
class CameraEvent:
    """カメラスレッドが検出したチップ動作イベント。"""

    seat: int
    timestamp: float  # time.time()
    frame: Optional[np.ndarray] = field(default=None, repr=False)  # Phase 2 以降で使用


@dataclass
class WordTiming:
    """ASR が出力する単語単位の時刻情報。時刻は絶対時刻 (unix time) で保持する。"""

    word: str
    start: float        # 単語発話開始の絶対時刻
    end: float          # 単語発話終了の絶対時刻
    confidence: float   # ASR の信頼度 [0, 1]


@dataclass
class ASRAlternative:
    """ASR の N-best 候補 1 件。"""

    text: str
    confidence: float
    words: list[WordTiming] = field(default_factory=list)


@dataclass
class AudioEvent:
    """音声認識スレッドが検出したアクションイベント。"""

    action: str  # "bet"/"call"/"raise"/"check"/"fold"/"allin"/"showdown"/"winner"/"new_hand"
    amount: int  # 金額なしの場合は 0
    timestamp: float  # time.time()
    raw_text: str
    # ベイズ推定レイヤ (v6.0+ M1) 向け追加情報。default 付きで既存 positional 構築を破壊しない
    alternatives: list[ASRAlternative] = field(default_factory=list)  # ASR の N-best (Vosk は >=1、Whisper は通常 1)
    word_timestamps: list[WordTiming] = field(default_factory=list)   # top-1 仮説の単語列 (絶対時刻)
    t_end: Optional[float] = None                                      # 発話終了の絶対時刻 (φ_time の入力)
