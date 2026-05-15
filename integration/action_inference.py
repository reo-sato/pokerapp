"""integration/action_inference.py

NormalizedSpeech と BettingState を組み合わせて、最終的な action / amount /
needs_review / reason を決定する。

責務:
  1. 金額のみ発話された場合の action 補完 (BET / CALL / RAISE)
  2. action+amount が来た場合の state との整合性チェック
  3. action-only の場合の妥当性検証
  4. 不整合は needs_review=True にし、reason を残す
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from audio.speech_normalizer import NormalizedSpeech
from integration.betting_state import (
    A_ALLIN, A_BET, A_CALL, A_CHECK, A_FOLD, A_RAISE,
    BettingState,
)

logger = logging.getLogger(__name__)


@dataclass
class InferredAction:
    """state-aware に推定された 1 つのアクション。

    needs_review=True の場合、action / amount は best-guess。
    呼び出し側はそのまま実行するか、レビュー UI に上げるかを選ぶ。
    """

    seat: Optional[int]
    action: Optional[str]   # 大文字: BET/CALL/RAISE/CHECK/FOLD/ALLIN or None
    amount: Optional[int]
    confidence: float
    needs_review: bool
    reason: str
    raw_text: str
    normalized_text: str


# ---- 公開 API ----

def infer_action_from_state(
    normalized: NormalizedSpeech,
    state: BettingState,
) -> InferredAction:
    """state-aware に action を推定する。

    順序:
      1. seat の整合性 → mismatch なら review
      2. action あり + amount あり → 整合性チェック
      3. action あり + amount なし → 妥当性チェック
      4. action なし + amount あり → 金額単独推定
      5. どちらもなし → review

    state が未初期化 (button 未設定) の場合は legacy fallback として
    そのまま (action, amount) を返す。
    """
    seat = _resolve_seat(normalized, state)
    seat_mismatch = (
        normalized.seat is not None
        and state.actor_seat is not None
        and normalized.seat != state.actor_seat
    )

    # state が未初期化 (button/blinds 未設定) → legacy fallback
    if not state.is_initialized:
        return _legacy_fallback(normalized, seat, seat_mismatch)

    if seat_mismatch:
        return _review(
            normalized, seat, normalized.action, normalized.amount,
            reason=(
                f"seat_mismatch: speech_seat={normalized.seat} "
                f"actor_seat={state.actor_seat}"
            ),
        )

    a = normalized.action
    amt = normalized.amount

    # ハンド・フェーズ系イベントは state-aware 推定の対象外
    if a in ("NEW_HAND", "SHOWDOWN", "WINNER"):
        return InferredAction(
            seat=seat, action=a, amount=amt or 0,
            confidence=0.9, needs_review=False,
            reason="phase_event_passthrough",
            raw_text=normalized.raw_text,
            normalized_text=normalized.normalized_text,
        )

    if a is not None and amt is not None:
        return _validate_action_with_amount(normalized, state, seat, a, amt)
    if a is not None and amt is None:
        return _validate_action_only(normalized, state, seat, a)
    if a is None and amt is not None:
        return _infer_from_amount_only(normalized, state, seat, amt)

    # action も amount も無い → review
    return _review(
        normalized, seat, None, None,
        reason="empty_speech",
    )


# ---- 内部ヘルパ ----

def _resolve_seat(normalized: NormalizedSpeech, state: BettingState) -> Optional[int]:
    """seat の優先順位: 音声で明示 > actor_seat。"""
    if normalized.seat is not None:
        return normalized.seat
    return state.actor_seat


def _ok(
    normalized: NormalizedSpeech,
    seat: Optional[int],
    action: Optional[str],
    amount: Optional[int],
    *,
    reason: str,
    confidence: float = 0.9,
) -> InferredAction:
    logger.info(
        "Audio infer: raw=%r normalized=%r actor=seat%s "
        "state(opened=%s,current_bet=%s) => %s %s reason=%s",
        normalized.raw_text, normalized.normalized_text, seat,
        getattr(_active_state, "is_opened", "?"),
        getattr(_active_state, "current_bet", "?"),
        action, amount if amount is not None else "",
        reason,
    )
    return InferredAction(
        seat=seat, action=action, amount=amount,
        confidence=confidence, needs_review=False,
        reason=reason,
        raw_text=normalized.raw_text,
        normalized_text=normalized.normalized_text,
    )


def _review(
    normalized: NormalizedSpeech,
    seat: Optional[int],
    action: Optional[str],
    amount: Optional[int],
    *,
    reason: str,
    confidence: float = 0.3,
) -> InferredAction:
    logger.warning(
        "Audio infer: raw=%r normalized=%r actor=seat%s => REVIEW "
        "action=%s amount=%s reason=%s",
        normalized.raw_text, normalized.normalized_text, seat,
        action, amount, reason,
    )
    return InferredAction(
        seat=seat, action=action, amount=amount,
        confidence=confidence, needs_review=True,
        reason=reason,
        raw_text=normalized.raw_text,
        normalized_text=normalized.normalized_text,
    )


# `_ok` 内のログでアクセスするための差し替え用ホルダ。
# (関数引数で渡し続けるよりログ整形が楽なため。スレッド非安全だが
# IntegrationThread 内で逐次処理されるので問題ない)
class _StateRef:
    is_opened = None
    current_bet = None


_active_state = _StateRef()


def _with_state_log(state: BettingState):
    _active_state.is_opened = state.is_opened
    _active_state.current_bet = state.current_bet


# ---- ケース別の推定ロジック ----

def _legacy_fallback(
    normalized: NormalizedSpeech,
    seat: Optional[int],
    seat_mismatch: bool,
) -> InferredAction:
    """BettingState 未初期化時の保守的なフォールバック。"""
    if seat_mismatch:
        return InferredAction(
            seat=seat, action=normalized.action, amount=normalized.amount,
            confidence=0.4, needs_review=True,
            reason="legacy_fallback_seat_mismatch",
            raw_text=normalized.raw_text,
            normalized_text=normalized.normalized_text,
        )
    return InferredAction(
        seat=seat, action=normalized.action, amount=normalized.amount,
        confidence=0.6,
        needs_review=(normalized.action is None and normalized.amount is None),
        reason="legacy_fallback",
        raw_text=normalized.raw_text,
        normalized_text=normalized.normalized_text,
    )


def _infer_from_amount_only(
    normalized: NormalizedSpeech,
    state: BettingState,
    seat: Optional[int],
    amount: int,
) -> InferredAction:
    """action=None かつ amount のみ → state を見て BET/CALL/RAISE を補完。"""
    _with_state_log(state)
    contrib = state.player_contrib_this_street.get(seat, 0) if seat is not None else 0
    call_target = state.current_bet

    if not state.is_opened:
        # opening bet。current_bet は 0 (preflop blind 後でない限り)
        return _ok(
            normalized, seat, A_BET, amount,
            reason="amount_only_opening_bet",
        )

    # is_opened: facing call/raise の場面
    if amount == call_target and call_target > contrib:
        return _ok(
            normalized, seat, A_CALL, call_target,
            reason="amount_only_matches_call",
        )
    if amount > call_target:
        return _ok(
            normalized, seat, A_RAISE, amount,
            reason="amount_only_above_call",
        )
    if amount < call_target and amount != contrib:
        return _review(
            normalized, seat, None, amount,
            reason="amount_only_below_call",
        )
    if call_target == contrib and amount > 0:
        # 既に追いついている。amount > call_target なら RAISE 扱い、
        # amount == call_target なら何もする必要がない CHECK 相当。
        # ここでは review に落とす (情報不足)。
        return _review(
            normalized, seat, None, amount,
            reason="amount_only_already_matched",
        )
    return _review(
        normalized, seat, None, amount,
        reason="amount_only_ambiguous",
    )


def _validate_action_with_amount(
    normalized: NormalizedSpeech,
    state: BettingState,
    seat: Optional[int],
    action: str,
    amount: int,
) -> InferredAction:
    """action + amount が共に提示された場合の検証。"""
    _with_state_log(state)
    contrib = state.player_contrib_this_street.get(seat, 0) if seat is not None else 0

    if action == A_BET:
        if state.is_opened:
            return _review(
                normalized, seat, A_RAISE, amount,
                reason="bet_but_already_opened_suggest_raise",
            )
        return _ok(normalized, seat, A_BET, amount, reason="action_bet_validated")

    if action == A_RAISE:
        if not state.is_opened:
            return _review(
                normalized, seat, A_BET, amount,
                reason="raise_but_not_opened_suggest_bet",
            )
        if amount <= state.current_bet:
            return _review(
                normalized, seat, A_RAISE, amount,
                reason="raise_amount_not_above_current_bet",
            )
        return _ok(normalized, seat, A_RAISE, amount, reason="action_raise_validated")

    if action == A_CALL:
        if not state.is_opened or state.current_bet == 0:
            return _review(
                normalized, seat, A_CALL, amount,
                reason="call_without_facing_bet",
            )
        # amount が call_target と一致するかは緩く検証
        if amount != state.current_bet:
            return _review(
                normalized, seat, A_CALL, state.current_bet,
                reason="call_amount_mismatch",
            )
        return _ok(normalized, seat, A_CALL, state.current_bet, reason="action_call_validated")

    if action == A_CHECK:
        if state.current_bet > contrib:
            return _review(
                normalized, seat, None, None,
                reason="check_while_facing_bet",
            )
        return _ok(normalized, seat, A_CHECK, 0, reason="action_check_validated")

    if action == A_FOLD:
        return _ok(normalized, seat, A_FOLD, 0, reason="action_fold_validated")

    if action == A_ALLIN:
        return _ok(normalized, seat, A_ALLIN, amount, reason="action_allin_validated")

    return _review(
        normalized, seat, action, amount,
        reason=f"unknown_action:{action}",
    )


def _validate_action_only(
    normalized: NormalizedSpeech,
    state: BettingState,
    seat: Optional[int],
    action: str,
) -> InferredAction:
    """action のみ提示された場合 (amount なし) の検証。"""
    _with_state_log(state)
    contrib = state.player_contrib_this_street.get(seat, 0) if seat is not None else 0

    if action == A_CALL:
        if not state.is_opened or state.current_bet == 0:
            return _review(
                normalized, seat, A_CALL, None,
                reason="call_without_facing_bet",
            )
        if state.current_bet == contrib:
            # 既に call target に到達している (BB が preflop に追加 call は不要)
            # → BB は CHECK 可能。CALL は冗長なので review。
            return _review(
                normalized, seat, A_CHECK, 0,
                reason="call_but_already_matched_suggest_check",
            )
        return _ok(
            normalized, seat, A_CALL, state.current_bet,
            reason="action_only_call",
        )

    if action == A_CHECK:
        if state.current_bet > contrib:
            return _review(
                normalized, seat, None, None,
                reason="check_while_facing_bet",
            )
        # preflop で BB 以外からの CHECK は不自然
        if state.street == "preflop" and seat != state.bb_seat:
            return _review(
                normalized, seat, A_CHECK, 0,
                reason="preflop_check_from_non_bb",
            )
        return _ok(normalized, seat, A_CHECK, 0, reason="action_only_check")

    if action == A_FOLD:
        return _ok(normalized, seat, A_FOLD, 0, reason="action_only_fold")

    if action == A_BET:
        if state.is_opened:
            return _review(
                normalized, seat, A_RAISE, None,
                reason="bet_but_already_opened_no_amount",
            )
        return _review(
            normalized, seat, A_BET, None,
            reason="bet_without_amount",
        )

    if action == A_RAISE:
        return _review(
            normalized, seat, A_RAISE, None,
            reason="raise_without_amount",
        )

    if action == A_ALLIN:
        # ALLIN は amount なしでも stack 全部の意味なので OK
        return _ok(normalized, seat, A_ALLIN, 0, reason="action_only_allin")

    return _review(
        normalized, seat, action, None,
        reason=f"unknown_action:{action}",
    )
