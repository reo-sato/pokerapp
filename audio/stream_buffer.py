from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from core.events import AudioEvent

logger = logging.getLogger(__name__)


class UtteranceType(Enum):
    """ディーラー発話の分類（spec.md FR-16）。"""
    CONFIRMATORY = "confirmatory"   # 確定型: 即 ACTION_CONFIRMED を発火
    PENDING      = "pending"        # 確認型: PENDING_CONFIRM 状態へ遷移
    DECLARATORY  = "declaratory"   # 宣言型: 即 PHASE_EVENT を発火


class BufferState(Enum):
    """AudioStreamBuffer の状態機械の状態（spec.md FR-15–17）。"""
    IDLE            = "idle"
    PENDING_CONFIRM = "pending_confirm"


@dataclass
class _PendingItem:
    """PENDING_CONFIRM 状態で保持する保留アクション。"""
    action: str
    amount: int | None


class AudioStreamBuffer:
    """ストリート単位で発話を蓄積し、確定型/確認型/宣言型に分類する状態機械。

    spec.md FR-15–17 の状態遷移を実装する:

        IDLE
          ├─ 確定型 → ACTION_CONFIRMED（即発火） → IDLE
          ├─ 確認型 → PENDING_CONFIRM(action, amount)
          └─ 宣言型 → PHASE_EVENT（即発火）    → IDLE

        PENDING_CONFIRM(action, amount)
          ├─ 確定型（同一 action）→ ACTION_CONFIRMED → IDLE
          ├─ 確認型（別 action） → PENDING_CONFIRM(新 action) （上書き）
          └─ 宣言型             → PHASE_EVENT → IDLE（pending は破棄）

    AudioThread から process_utterance() を呼ぶ。
    戻り値のリストに含まれる AudioEvent を event_queue に送出すること。
    """

    _state: BufferState
    _pending: Optional[_PendingItem]

    def __init__(self) -> None: ...

    def process_utterance(self, text: str) -> list[AudioEvent]:
        """Whisper 認識テキストを受け取り、確定したイベントのリストを返す。

        戻り値は空リスト（確定なし）または 1 件以上の AudioEvent。
        """
        ...

    def _classify_utterance(self, text: str) -> UtteranceType:
        """テキストを確定型/確認型/宣言型に分類する。

        判定ロジック:
        1. DECLARATORY_KEYWORDS にキーワードが含まれれば DECLARATORY
        2. QUESTION_PATTERNS のいずれかで終わる（または含む）なら PENDING
        3. それ以外は CONFIRMATORY
        """
        ...

    def reset(self) -> None:
        """ストリート遷移時などに状態を IDLE に戻す。"""
        ...

    @property
    def state(self) -> BufferState:
        """現在の状態機械の状態を返す。"""
        ...
