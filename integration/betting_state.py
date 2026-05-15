"""integration/betting_state.py

ハンド単位のベッティング状態を表現する dataclass と、状態遷移ヘルパ。

GameStateManager がスタック/ポット/hand_id を持つのに対し、BettingState は
「今だれの番か」「current_bet はいくらか」「is_opened か」など、音声入力の
state-aware 解釈に必要な情報を保持する。

両者は IntegrationThread から協調的に更新される。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from integration.action_order import (
    advance_actor,
    compute_blinds,
    compute_first_actor_postflop,
    compute_first_actor_preflop,
)

logger = logging.getLogger(__name__)


# Action ラベル (大文字で統一)。speech_normalizer / action_inference と共通。
A_BET = "BET"
A_CALL = "CALL"
A_RAISE = "RAISE"
A_CHECK = "CHECK"
A_FOLD = "FOLD"
A_ALLIN = "ALLIN"
A_SB_POST = "SB_POST"
A_BB_POST = "BB_POST"


@dataclass
class BettingState:
    """1 ハンドの現在のベッティング状態。

    全フィールドは IntegrationThread / HandStateMachine がライフサイクル管理する。
    """

    # ハンド単位
    button_seat: Optional[int] = None
    sb_seat: Optional[int] = None
    bb_seat: Optional[int] = None
    sb_amount: int = 0
    bb_amount: int = 0
    active_seats: list[int] = field(default_factory=list)
    folded_seats: set[int] = field(default_factory=set)
    all_in_seats: set[int] = field(default_factory=set)
    player_contrib_hand: dict[int, int] = field(default_factory=dict)
    action_history: list[dict] = field(default_factory=list)

    # ストリート単位
    street: str = "preflop"
    actor_seat: Optional[int] = None
    current_bet: int = 0
    last_raise_to: int = 0
    last_aggressor: Optional[int] = None
    is_opened: bool = False
    player_contrib_this_street: dict[int, int] = field(default_factory=dict)

    # 完了フラグ
    is_initialized: bool = False  # button_seat + blinds 計算が完了したか
    hand_over: bool = False

    # ---- ハンドライフサイクル ----

    def start_hand(
        self,
        *,
        button_seat: int,
        active_seats: list[int],
        sb_amount: int,
        bb_amount: int,
    ) -> None:
        """ハンド開始時に呼び出す。button から SB/BB を算出し、blind を post する。"""
        self.button_seat = button_seat
        self.active_seats = sorted(set(active_seats))
        self.folded_seats = set()
        self.all_in_seats = set()
        self.player_contrib_hand = {s: 0 for s in self.active_seats}
        self.action_history = []
        self.street = "preflop"
        self.sb_amount = sb_amount
        self.bb_amount = bb_amount
        self.last_aggressor = None
        self.hand_over = False

        sb, bb = compute_blinds(button_seat, self.active_seats)
        self.sb_seat = sb
        self.bb_seat = bb
        if sb is None or bb is None:
            self.is_initialized = False
            self.current_bet = 0
            self.is_opened = False
            self.actor_seat = None
            self.player_contrib_this_street = {s: 0 for s in self.active_seats}
            logger.warning(
                "Hand start: blinds could not be computed (button=%s active=%s) — "
                "BettingState not initialized",
                button_seat, self.active_seats,
            )
            return

        # blind を自動 post
        self.player_contrib_this_street = {s: 0 for s in self.active_seats}
        self._record_post(sb, A_SB_POST, sb_amount)
        self._record_post(bb, A_BB_POST, bb_amount)
        self.current_bet = bb_amount
        self.last_raise_to = bb_amount
        self.is_opened = True  # BB により preflop は call target が存在する
        self.actor_seat = compute_first_actor_preflop(button_seat, self.active_seats)
        self.is_initialized = True

        logger.info(
            "Hand start: button=%d active=%s sb=%d bb=%d first_actor=%s blinds=(%d/%d)",
            button_seat, self.active_seats, sb, bb,
            self.actor_seat, sb_amount, bb_amount,
        )
        logger.info("Auto post: seat=%d action=SB_POST amount=%d", sb, sb_amount)
        logger.info("Auto post: seat=%d action=BB_POST amount=%d", bb, bb_amount)

    def _record_post(self, seat: int, action: str, amount: int) -> None:
        self.player_contrib_this_street[seat] = (
            self.player_contrib_this_street.get(seat, 0) + amount
        )
        self.player_contrib_hand[seat] = (
            self.player_contrib_hand.get(seat, 0) + amount
        )
        self.action_history.append({"seat": seat, "action": action, "amount": amount})

    def reset_for_new_street(self, street: str) -> None:
        """flop/turn/river 開始時の状態リセット。"""
        self.street = street
        self.current_bet = 0
        self.last_raise_to = 0
        self.last_aggressor = None
        self.is_opened = False
        self.player_contrib_this_street = {s: 0 for s in self.active_seats}
        if self.button_seat is None:
            self.actor_seat = None
        else:
            self.actor_seat = compute_first_actor_postflop(
                self.button_seat,
                self.active_seats,
                folded_seats=self.folded_seats,
                all_in_seats=self.all_in_seats,
            )
        logger.info(
            "Street %s: first_actor=%s current_bet reset to 0",
            street, self.actor_seat,
        )

    # ---- アクション適用 ----

    def apply_action(self, seat: int, action: str, amount: int = 0) -> None:
        """アクションを betting_state に反映する。actor 進行までを行う。

        amount は「投入チップ量」ではなく「raise to / bet to / call to」相当の絶対値。
        - BET / RAISE: target_to を amount に置く (current_bet を amount に更新)
        - CALL: current_bet までを埋める
        - CHECK: 何もしない (contrib 0)
        - FOLD: folded_seats に追加
        - ALLIN: amount 分を投入。current_bet を超えていれば raise 扱いで current_bet 更新
        """
        action_u = action.upper()
        contrib_before = self.player_contrib_this_street.get(seat, 0)

        if action_u == A_FOLD:
            self.folded_seats.add(seat)
            self.action_history.append({"seat": seat, "action": A_FOLD, "amount": 0})
        elif action_u == A_CHECK:
            self.action_history.append({"seat": seat, "action": A_CHECK, "amount": 0})
        elif action_u == A_CALL:
            target = max(self.current_bet, contrib_before)
            delta = target - contrib_before
            self.player_contrib_this_street[seat] = target
            self.player_contrib_hand[seat] = (
                self.player_contrib_hand.get(seat, 0) + delta
            )
            self.action_history.append({"seat": seat, "action": A_CALL, "amount": target})
        elif action_u in (A_BET, A_RAISE):
            target = max(amount, self.current_bet)
            delta = target - contrib_before
            self.player_contrib_this_street[seat] = target
            self.player_contrib_hand[seat] = (
                self.player_contrib_hand.get(seat, 0) + delta
            )
            self.last_raise_to = target
            self.current_bet = target
            self.is_opened = True
            self.last_aggressor = seat
            self.action_history.append({"seat": seat, "action": action_u, "amount": target})
        elif action_u == A_ALLIN:
            target = max(amount, contrib_before)
            delta = target - contrib_before
            self.player_contrib_this_street[seat] = target
            self.player_contrib_hand[seat] = (
                self.player_contrib_hand.get(seat, 0) + delta
            )
            self.all_in_seats.add(seat)
            if target > self.current_bet:
                self.current_bet = target
                self.last_raise_to = target
                self.is_opened = True
                self.last_aggressor = seat
            self.action_history.append({"seat": seat, "action": A_ALLIN, "amount": target})
        else:
            logger.warning("BettingState: unknown action %r ignored", action)
            return

        # actor を進める
        self.actor_seat = advance_actor(
            seat,
            self.active_seats,
            folded_seats=self.folded_seats,
            all_in_seats=self.all_in_seats,
        )

    # ---- 補助 ----

    def call_amount_for(self, seat: int) -> int:
        """seat が CALL するために必要な追加投入額。負にはならない。"""
        contrib = self.player_contrib_this_street.get(seat, 0)
        return max(0, self.current_bet - contrib)

    def is_facing_bet(self, seat: int) -> bool:
        return self.call_amount_for(seat) > 0

    def is_round_complete(self) -> bool:
        """このストリートの bet round が完了したか。

        live (folded/all-in 以外) の全員が current_bet に追いついていて、
        かつ 全員が少なくとも 1 回 action 済みなら完了とみなす近似判定。

        厳密な判定 (BB option など) は呼び出し側で行うこと。
        """
        live = [s for s in self.active_seats
                if s not in self.folded_seats and s not in self.all_in_seats]
        if len(live) <= 1:
            return True
        for s in live:
            if self.player_contrib_this_street.get(s, 0) != self.current_bet:
                return False
        return True

    def snapshot(self) -> dict:
        return {
            "street": self.street,
            "button_seat": self.button_seat,
            "sb_seat": self.sb_seat,
            "bb_seat": self.bb_seat,
            "actor_seat": self.actor_seat,
            "current_bet": self.current_bet,
            "is_opened": self.is_opened,
            "active_seats": list(self.active_seats),
            "folded_seats": sorted(self.folded_seats),
            "all_in_seats": sorted(self.all_in_seats),
            "player_contrib_this_street": dict(self.player_contrib_this_street),
        }
