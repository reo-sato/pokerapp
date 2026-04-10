from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.game_state import GameState


class ActionType(str, Enum):
    """ポーカーアクション種別（spec.md FR-22）。"""
    CHECK  = "check"
    BET    = "bet"
    CALL   = "call"
    RAISE  = "raise"
    FOLD   = "fold"
    ALL_IN = "allin"


def legal_actions(seat: int, state: GameState) -> frozenset[ActionType]:
    """spec.md FR-22 のロジックに従い、seat が取り得るアクション集合を返す。

    算出ロジック:
        call_diff = state.call_amount - state.invested.get(seat, 0)

        folded / all-in → frozenset()（空集合）
        call_diff == 0  → {CHECK, BET}
        stack <= call_diff → {ALL_IN, FOLD}
        call_diff > 0   → {CALL, RAISE, FOLD}
    """
    if seat in state.folded_seats or seat in state.all_in_seats:
        return frozenset()

    call_diff = state.call_amount - state.invested.get(seat, 0)

    if call_diff == 0:
        return frozenset({ActionType.CHECK, ActionType.BET})

    if state.get_stack(seat) <= call_diff:
        return frozenset({ActionType.ALL_IN, ActionType.FOLD})

    # call_diff > 0 and stack covers the call
    return frozenset({ActionType.CALL, ActionType.RAISE, ActionType.FOLD})


def validate_action(
    seat: int,
    action: ActionType,
    state: GameState,
) -> tuple[bool, str]:
    """legal_actions に含まれない場合 (False, 理由文字列) を返す。

    含まれる場合は (True, "") を返す。
    spec.md FR-23 の矛盾検知に使用する。
    """
    allowed = legal_actions(seat, state)
    if action in allowed:
        return (True, "")
    allowed_names = [a.value for a in allowed]
    return (False, f"action {action.value!r} is not legal for seat {seat} (allowed: {allowed_names})")


def detect_street_overflow(street_action_count: int, active_count: int) -> bool:
    """spec.md FR-24: ストリート内アクション数超過を検知する。

    street_action_count > active_count の場合 True を返す。
    """
    return street_action_count > active_count


def detect_fold_contradiction(
    seat: int,
    audio_action: str,
    state: GameState,
) -> bool:
    """spec.md FR-25: RFID フォールドと音声フォールドの矛盾を検知する。

    例: フォールド済みの席に再度ターンが回ってきた場合に True を返す。
    """
    return seat in state.folded_seats
