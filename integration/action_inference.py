"""integration/action_inference.py

ゲームステートを使って音声コマンドの妥当性判定とアクション補完を行う。

AudioEvent の action が "amount_only" の場合、BettingState から
BET / CALL / RAISE を推定する。

action が明示されている場合はゲームステートと照合し、矛盾があれば
needs_review フラグを立てる。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from core.events import AudioEvent

logger = logging.getLogger(__name__)


@dataclass
class BettingState:
    """現在ストリートのベッティング状態を追跡するデータクラス。"""

    street: str = "preflop"
    current_bet: int = 0
    is_opened: bool = False
    last_raise_to: int = 0
    player_contrib_this_street: dict[int, int] = field(default_factory=dict)
    active_seats: list[int] = field(default_factory=list)
    folded_seats: list[int] = field(default_factory=list)

    def get_contrib(self, seat: int) -> int:
        """指定席の今ストリートの投資額を返す。"""
        return self.player_contrib_this_street.get(seat, 0)

    def reset_for_new_street(self) -> None:
        """新ストリート開始時にベッティング状態をリセットする。"""
        self.current_bet = 0
        self.is_opened = False
        self.last_raise_to = 0
        self.player_contrib_this_street.clear()

    def reset_for_new_hand(self) -> None:
        """新ハンド開始時に全状態をリセットする。"""
        self.street = "preflop"
        self.reset_for_new_street()
        self.folded_seats.clear()

    def update_after_action(self, seat: int, action: str, amount: int) -> None:
        """アクション実行後に状態を更新する。"""
        a = action.lower()
        if a in ("bet", "raise", "allin"):
            self.current_bet = amount
            self.last_raise_to = amount
            self.is_opened = True
            self.player_contrib_this_street[seat] = self.get_contrib(seat) + amount
        elif a == "call":
            self.player_contrib_this_street[seat] = self.current_bet
        elif a == "fold":
            if seat not in self.folded_seats:
                self.folded_seats.append(seat)


@dataclass
class InferredAction:
    """infer_action() の返り値。"""

    seat: Optional[int]
    action: Optional[str]   # lowercase: "bet"/"call"/"raise"/"check"/"fold" / None
    amount: int
    confidence: float       # 推定信頼度 (0.0–1.0)
    needs_review: bool
    reason: str
    raw_text: str
    normalized_text: str


def _infer_from_amount_only(
    amount: int,
    state: BettingState,
    actor_seat: Optional[int],
    raw_text: str,
    normalized_text: str,
) -> InferredAction:
    """アクション不明・金額のみの場合にゲームステートから推定する。

    Case A: is_opened=False                         → BET(amount)
    Case B: amount == current_bet, contrib == 0     → CALL(amount)
    Case C: amount >  current_bet, contrib == 0     → RAISE(amount)
    Case E: amount >  current_bet, contrib >  0     → RAISE(amount)  (re-raise)
    Case D: amount <  current_bet                   → needs_review
    """
    contrib = state.get_contrib(actor_seat) if actor_seat is not None else 0
    actor_label = f"seat{actor_seat}" if actor_seat is not None else "unknown"

    if amount == 0:
        logger.debug(
            "Audio infer: raw=%r actor=%s amount=0 => needs_review (amount_only_no_amount)",
            raw_text, actor_label,
        )
        return InferredAction(
            seat=actor_seat, action=None, amount=0,
            confidence=0.0, needs_review=True,
            reason="amount_only_no_amount",
            raw_text=raw_text, normalized_text=normalized_text,
        )

    if not state.is_opened:
        inferred, reason, confidence = "bet", "amount_only_opening_bet", 0.75
    elif amount == state.current_bet and contrib == 0:
        inferred, reason, confidence = "call", "amount_only_exact_call", 0.80
    elif amount > state.current_bet and contrib == 0:
        inferred, reason, confidence = "raise", "amount_only_above_call", 0.70
    elif amount > state.current_bet and contrib > 0:
        inferred, reason, confidence = "raise", "amount_only_reraise", 0.70
    else:
        # Case D: amount < current_bet
        logger.warning(
            "Audio infer: raw=%r normalized=%r actor=%s "
            "state(opened=%s,current_bet=%d,contrib=%d) "
            "amount=%d < current_bet => needs_review",
            raw_text, normalized_text, actor_label,
            state.is_opened, state.current_bet, contrib, amount,
        )
        return InferredAction(
            seat=actor_seat, action=None, amount=amount,
            confidence=0.30, needs_review=True,
            reason="amount_only_below_call",
            raw_text=raw_text, normalized_text=normalized_text,
        )

    logger.info(
        "Audio infer: raw=%r normalized=%r actor=%s "
        "state(opened=%s,current_bet=%d,contrib=%d) => %s %d reason=%s",
        raw_text, normalized_text, actor_label,
        state.is_opened, state.current_bet, contrib,
        inferred.upper(), amount, reason,
    )
    return InferredAction(
        seat=actor_seat, action=inferred, amount=amount,
        confidence=confidence, needs_review=False,
        reason=reason,
        raw_text=raw_text, normalized_text=normalized_text,
    )


def _validate_provided_action(
    action: str,
    amount: int,
    state: BettingState,
    actor_seat: Optional[int],
    raw_text: str,
    normalized_text: str,
) -> InferredAction:
    """明示されたアクションをゲームステートと照合して妥当性を検証する。

    ベッティングラウンドの区切りを音声イベントだけで確実に検出することは
    難しいため、ここでは「論理的に不可能なアクション」のみ needs_review を立てる。
    具体的には check（ベット済み状態で宣言）のみ。
    """
    a = action.lower()
    needs_review = False
    reason = "action_accepted"

    if a == "check" and state.is_opened:
        needs_review = True
        reason = "check_when_bet_open"
        logger.warning(
            "Audio infer: raw=%r action=check but current_bet=%d => needs_review",
            raw_text, state.current_bet,
        )
    else:
        logger.debug(
            "Audio infer: raw=%r action=%s amount=%d "
            "state(opened=%s,current_bet=%d) => accepted",
            raw_text, a, amount, state.is_opened, state.current_bet,
        )

    return InferredAction(
        seat=actor_seat, action=a, amount=amount,
        confidence=1.0, needs_review=needs_review,
        reason=reason,
        raw_text=raw_text, normalized_text=normalized_text,
    )


def infer_action(
    event: AudioEvent,
    state: BettingState,
    actor_seat: Optional[int],
) -> InferredAction:
    """AudioEvent とゲームステートからアクションを推定・検証して返す。

    event.action == "amount_only" の場合: BettingState から BET/CALL/RAISE を推定。
    event.action が通常のアクション: ゲームステートとの整合性を検証。
    """
    raw_text = event.raw_text or ""
    normalized_text = raw_text
    amount = event.amount or 0

    if event.action in (None, "amount_only"):
        return _infer_from_amount_only(amount, state, actor_seat, raw_text, normalized_text)

    return _validate_provided_action(
        event.action, amount, state, actor_seat, raw_text, normalized_text,
    )
