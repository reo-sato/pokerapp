from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from core.constants import ACTION_KEYWORDS, DECLARATORY_KEYWORDS, QUESTION_PATTERNS
from core.events import AudioEvent

logger = logging.getLogger(__name__)


class UtteranceType(Enum):
    """ディーラー発話の分類（spec.md FR-16）。"""
    CONFIRMATORY = "confirmatory"   # 確定型: 即 ACTION_CONFIRMED を発火
    PENDING      = "pending"        # 確認型: PENDING_CONFIRM 状態へ遷移
    DECLARATORY  = "declaratory"    # 宣言型: 即 PHASE_EVENT を発火


class BufferState(Enum):
    """AudioStreamBuffer の状態機械の状態（spec.md FR-15–17）。"""
    IDLE            = "idle"
    PENDING_CONFIRM = "pending_confirm"


@dataclass
class _PendingItem:
    """PENDING_CONFIRM 状態で保持する保留アクション。"""
    action: str
    amount: Optional[int]


def _now_iso() -> str:
    return datetime.datetime.now().isoformat()


def _make_phase_event(text: str) -> Optional[AudioEvent]:
    """declaratory テキストから PHASE_EVENT 用 AudioEvent を生成する。

    parse_action が None を返す場合（"ポット"等）は DECLARATORY_KEYWORDS を
    直接検索して ACTION_KEYWORDS 経由でアクション名を解決する。
    """
    # 遅延インポートで循環依存を回避
    from audio.recognizer import extract_seat, extract_position, parse_action

    ev = parse_action(text)
    if ev is not None:
        return ev

    # ACTION_KEYWORDS にない宣言型キーワード（例: "ポット"）
    text_lower = text.lower()
    for kw in DECLARATORY_KEYWORDS:
        if kw.lower() in text_lower:
            mapped = ACTION_KEYWORDS.get(kw, kw)
            return AudioEvent(
                action=mapped,
                amount=None,
                timestamp=_now_iso(),
                raw_text=text,
                mentioned_seat=extract_seat(text),
                mentioned_position=extract_position(text),
            )
    return None


class AudioStreamBuffer:
    """ストリート単位で発話を蓄積し、確定型/確認型/宣言型に分類する状態機械。

    spec.md FR-15–17 の状態遷移:

        IDLE
          ├─ 確定型 → ACTION_CONFIRMED（即発火） → IDLE
          ├─ 確認型 → PENDING_CONFIRM(action, amount)
          └─ 宣言型 → PHASE_EVENT（即発火）      → IDLE

        PENDING_CONFIRM(action, amount)
          ├─ 確定型 → ACTION_CONFIRMED → IDLE
          ├─ 確認型 → PENDING_CONFIRM(新 action) （上書き）
          └─ 宣言型 → PHASE_EVENT → IDLE（pending は破棄）

    AudioThread から process_utterance() を呼び出し、戻り値の AudioEvent を
    event_queue に送出する。
    """

    def __init__(self) -> None:
        self._state: BufferState = BufferState.IDLE
        self._pending: Optional[_PendingItem] = None

    # ── 公開 API ──────────────────────────────────────────────────────────────

    def process_utterance(self, text: str) -> list[AudioEvent]:
        """Whisper 認識テキストを受け取り、確定したイベントのリストを返す。

        戻り値は空リスト（確定なし）または 1 件以上の AudioEvent。
        """
        from audio.recognizer import extract_seat, extract_position, parse_action

        utype = self._classify_utterance(text)
        logger.debug("process_utterance: state=%s type=%s text=%r",
                     self._state.value, utype.value, text)

        if self._state == BufferState.IDLE:
            return self._handle_idle(text, utype, parse_action, extract_seat, extract_position)
        else:
            return self._handle_pending(text, utype, parse_action, extract_seat, extract_position)

    def _classify_utterance(self, text: str) -> UtteranceType:
        """テキストを確定型/確認型/宣言型に分類する（FR-16）。

        優先順位:
        1. DECLARATORY_KEYWORDS のいずれかが含まれれば DECLARATORY
        2. QUESTION_PATTERNS のいずれかで終わる、または「？」で終われば PENDING
        3. それ以外は CONFIRMATORY
        """
        text_lower = text.lower()

        # 1. 宣言型チェック（最優先）
        for kw in DECLARATORY_KEYWORDS:
            if kw.lower() in text_lower:
                return UtteranceType.DECLARATORY

        # 2. 確認型チェック（末尾パターン）
        stripped = text.rstrip()
        for pattern in QUESTION_PATTERNS:
            if stripped.endswith(pattern):
                return UtteranceType.PENDING
        if stripped.endswith("？") or stripped.endswith("?"):
            return UtteranceType.PENDING

        return UtteranceType.CONFIRMATORY

    def reset(self) -> None:
        """ストリート遷移時などに状態を IDLE に戻す。"""
        self._state = BufferState.IDLE
        self._pending = None
        logger.debug("AudioStreamBuffer reset to IDLE")

    @property
    def state(self) -> BufferState:
        """現在の状態機械の状態を返す。"""
        return self._state

    # ── 状態別ハンドラ（内部） ────────────────────────────────────────────────

    def _handle_idle(
        self, text: str, utype: UtteranceType,
        parse_action, extract_seat, extract_position,
    ) -> list[AudioEvent]:
        if utype == UtteranceType.CONFIRMATORY:
            # 確定型 → 即 ACTION_CONFIRMED
            ev = parse_action(text)
            if ev is None:
                return []
            logger.debug("IDLE + CONFIRMATORY → ACTION_CONFIRMED: %s", ev.action)
            return [ev]

        elif utype == UtteranceType.PENDING:
            # 確認型 → PENDING_CONFIRM に遷移、イベントなし
            ev = parse_action(text)
            self._pending = _PendingItem(
                action=ev.action if ev is not None else "",
                amount=ev.amount if ev is not None else None,
            )
            self._state = BufferState.PENDING_CONFIRM
            logger.debug("IDLE + PENDING → PENDING_CONFIRM(action=%s)", self._pending.action)
            return []

        else:  # DECLARATORY
            ev = _make_phase_event(text)
            if ev is not None:
                logger.debug("IDLE + DECLARATORY → PHASE_EVENT: %s", ev.action)
                return [ev]
            return []

    def _handle_pending(
        self, text: str, utype: UtteranceType,
        parse_action, extract_seat, extract_position,
    ) -> list[AudioEvent]:
        if utype == UtteranceType.CONFIRMATORY:
            # 確定型 → PENDING を確定
            new_ev = parse_action(text)
            if new_ev is not None:
                # 新しいテキストでアクション認識できた場合はそちらを使う
                action_str = new_ev.action
                amount     = new_ev.amount
            elif self._pending is not None:
                # 「はい」など行動キーワードなしの確認 → 保留中のアクションを使う
                action_str = self._pending.action
                amount     = self._pending.amount
            else:
                action_str = ""
                amount     = None

            self._state   = BufferState.IDLE
            self._pending = None
            logger.debug("PENDING_CONFIRM + CONFIRMATORY → ACTION_CONFIRMED: %s", action_str)
            return [AudioEvent(
                action=action_str,
                amount=amount,
                timestamp=_now_iso(),
                raw_text=text,
                mentioned_seat=extract_seat(text),
                mentioned_position=extract_position(text),
            )]

        elif utype == UtteranceType.PENDING:
            # 確認型（別アクション）→ pending を上書き
            ev = parse_action(text)
            self._pending = _PendingItem(
                action=ev.action if ev is not None else "",
                amount=ev.amount if ev is not None else None,
            )
            logger.debug("PENDING_CONFIRM + PENDING → overwrite pending: %s", self._pending.action)
            return []

        else:  # DECLARATORY
            # pending を破棄して PHASE_EVENT を発火
            logger.debug("PENDING_CONFIRM + DECLARATORY → PHASE_EVENT, pending discarded")
            self._state   = BufferState.IDLE
            self._pending = None
            ev = _make_phase_event(text)
            return [ev] if ev is not None else []
