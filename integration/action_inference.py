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
from integration.action_order import (
    advance_actor,
    compute_blinds,
    compute_first_actor_postflop,
    compute_first_actor_preflop,
)

logger = logging.getLogger(__name__)


@dataclass
class BettingState:
    """現在ストリートのベッティング状態 + ハンド単位のボタン/ブラインド情報を保持する。

    Engine から見た「ハンド開始時に self-contained に actor / blind を確定できる」
    一元 state。`start_hand()` を呼ぶと button から SB/BB/first_actor を算出し、
    blind の auto post まで状態に反映する。
    """

    # ── ストリート単位の状態 ────────────────────────────────────────────────
    street: str = "preflop"
    current_bet: int = 0
    is_opened: bool = False
    last_raise_to: int = 0
    player_contrib_this_street: dict[int, int] = field(default_factory=dict)
    active_seats: list[int] = field(default_factory=list)
    folded_seats: list[int] = field(default_factory=list)
    all_in_seats: list[int] = field(default_factory=list)

    # ── ハンド単位 (button / blind / actor) ─────────────────────────────────
    button_seat: Optional[int] = None
    sb_seat: Optional[int] = None
    bb_seat: Optional[int] = None
    sb_amount: int = 0
    bb_amount: int = 0
    actor_seat: Optional[int] = None
    last_aggressor: Optional[int] = None
    player_contrib_hand: dict[int, int] = field(default_factory=dict)
    action_history: list[dict] = field(default_factory=list)
    is_initialized: bool = False  # start_hand が button/blind を確定できたか

    # ── ラウンドクローズ検出用 (Phase 5-K) ─────────────────────────────────
    # 今ストリートで voluntary action を出した seat 集合。SB_POST / BB_POST は
    # 「強制 post であって BB option 行使ではない」ため含めない。これにより
    # preflop で everyone limp 後も BB の option が消化されるまで round close
    # 扱いにならない。
    acted_this_street: set[int] = field(default_factory=set)

    def get_contrib(self, seat: int) -> int:
        """指定席の今ストリートの投資額を返す。"""
        return self.player_contrib_this_street.get(seat, 0)

    def call_amount_for(self, seat: int) -> int:
        """seat が CALL するために必要な追加投入額 (負にはならない)。"""
        return max(0, self.current_bet - self.get_contrib(seat))

    def reset_for_new_street(self) -> None:
        """新ストリート開始時にベッティング状態をリセットする。

        button_seat が既知なら postflop first actor も再計算する。
        """
        self.current_bet = 0
        self.is_opened = False
        self.last_raise_to = 0
        self.last_aggressor = None
        self.player_contrib_this_street.clear()
        self.acted_this_street.clear()
        if self.button_seat is not None and self.active_seats:
            self.actor_seat = compute_first_actor_postflop(
                self.button_seat,
                self.active_seats,
                folded_seats=set(self.folded_seats),
                all_in_seats=set(self.all_in_seats),
            )

    def reset_for_new_hand(self) -> None:
        """新ハンド開始時に全状態をリセットする (button/blind 情報は呼び出し側で再設定)。"""
        self.street = "preflop"
        self.reset_for_new_street()
        self.folded_seats.clear()
        self.all_in_seats.clear()
        self.player_contrib_hand.clear()
        self.action_history.clear()
        self.button_seat = None
        self.sb_seat = None
        self.bb_seat = None
        self.actor_seat = None
        self.last_aggressor = None
        self.is_initialized = False

    def start_hand(
        self,
        *,
        button_seat: int,
        active_seats: list[int],
        sb_amount: int,
        bb_amount: int,
    ) -> None:
        """ハンド開始時に button / SB / BB / first actor を確定し、blind を auto post する。

        この呼び出しの後、action_history には SB_POST と BB_POST が積まれ、
        current_bet=bb_amount, is_opened=True, actor_seat=preflop first actor となる。

        button が active_seats に含まれない / active が 2 人未満なら is_initialized=False。
        """
        self.reset_for_new_hand()
        self.active_seats = sorted(set(active_seats))
        self.sb_amount = sb_amount
        self.bb_amount = bb_amount
        self.button_seat = button_seat
        self.player_contrib_hand = {s: 0 for s in self.active_seats}
        self.player_contrib_this_street = {s: 0 for s in self.active_seats}

        sb, bb = compute_blinds(button_seat, self.active_seats)
        self.sb_seat = sb
        self.bb_seat = bb
        if sb is None or bb is None:
            logger.warning(
                "Hand start: blinds could not be computed (button=%s active=%s) — "
                "BettingState not initialized",
                button_seat, self.active_seats,
            )
            self.is_initialized = False
            return

        # blind 自動 post
        self._record_post(sb, "SB_POST", sb_amount)
        self._record_post(bb, "BB_POST", bb_amount)
        self.current_bet = bb_amount
        self.last_raise_to = bb_amount
        self.is_opened = True
        self.actor_seat = compute_first_actor_preflop(button_seat, self.active_seats)
        self.is_initialized = True

        logger.info(
            "Hand start: button=%d sb=%d bb=%d first_actor=%s blinds=(%d/%d)",
            button_seat, sb, bb, self.actor_seat, sb_amount, bb_amount,
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

    def update_after_action(self, seat: int, action: str, amount: int) -> None:
        """アクション実行後に状態を更新する。actor_seat も次へ進める。

        amount は BET/RAISE では「raise-to の絶対値」、CALL/CHECK/FOLD では
        無視される。ALLIN は target を amount または contrib のいずれか大きい方とする。
        """
        a = action.lower()

        if a in ("bet", "raise"):
            target = max(amount, self.current_bet)
            delta = target - self.get_contrib(seat)
            self.player_contrib_this_street[seat] = target
            self.player_contrib_hand[seat] = (
                self.player_contrib_hand.get(seat, 0) + max(0, delta)
            )
            self.current_bet = target
            self.last_raise_to = target
            self.is_opened = True
            self.last_aggressor = seat
        elif a == "allin":
            target = max(amount, self.get_contrib(seat))
            delta = target - self.get_contrib(seat)
            self.player_contrib_this_street[seat] = target
            self.player_contrib_hand[seat] = (
                self.player_contrib_hand.get(seat, 0) + max(0, delta)
            )
            if seat not in self.all_in_seats:
                self.all_in_seats.append(seat)
            if target > self.current_bet:
                self.current_bet = target
                self.last_raise_to = target
                self.is_opened = True
                self.last_aggressor = seat
        elif a == "call":
            delta = self.current_bet - self.get_contrib(seat)
            self.player_contrib_this_street[seat] = self.current_bet
            self.player_contrib_hand[seat] = (
                self.player_contrib_hand.get(seat, 0) + max(0, delta)
            )
        elif a == "fold":
            if seat not in self.folded_seats:
                self.folded_seats.append(seat)
        # check: contrib も history も増分なし

        self.action_history.append({"seat": seat, "action": a, "amount": amount})

        # voluntary action を出した seat として記録 (Phase 5-K: round close 判定用)
        self.acted_this_street.add(seat)

        # actor を次の live seat へ進める (button_seat が既知のときのみ)
        if self.button_seat is not None and self.active_seats:
            self.actor_seat = advance_actor(
                seat,
                self.active_seats,
                folded_seats=set(self.folded_seats),
                all_in_seats=set(self.all_in_seats),
            )

    def is_round_closed(self) -> bool:
        """ベッティングラウンドが closed か (= 全 live 非 all-in seat が
        contrib==current_bet かつ voluntary action 済) を返す。

        判定条件 (Phase 5-K, all True で closed):
          1. ``is_initialized=True`` (= hand state machine が走っている)
          2. ``live = active_seats - folded_seats`` の人数が 2 以上
             (1 以下は fold_win 待ちなので closed 扱いしない)
          3. ``must_act = live - all_in_seats`` の全 seat について:
             a. ``player_contrib_this_street[seat] == current_bet``
             b. ``seat in acted_this_street`` (= voluntary 行使済み)
          4. ``must_act`` が空 (= 全員 all-in) の場合は (3) を vacuously True
             として round closed 扱い

        SB_POST / BB_POST は ``_record_post`` で acted_this_street に **追加されない**
        ため、preflop everyone-limp ケースでは BB が check するまで closed にならない
        (= BB option 行使を待つ正しい semantics)。
        """
        if not self.is_initialized:
            return False
        live = [s for s in self.active_seats if s not in self.folded_seats]
        if len(live) < 2:
            return False
        must_act = [s for s in live if s not in self.all_in_seats]
        for s in must_act:
            if self.get_contrib(s) != self.current_bet:
                return False
            if s not in self.acted_this_street:
                return False
        return True


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


def _infer_action_core(
    event: AudioEvent,
    state: BettingState,
    actor_seat: Optional[int],
) -> InferredAction:
    """AudioEvent から既存ロジックで InferredAction を計算する (decision-tree primary)。

    event.action == "amount_only" の場合: BettingState から BET/CALL/RAISE を推定。
    event.action が通常のアクション: ゲームステートとの整合性を検証。

    M2 で `infer_action()` から切り出した「primary 仮説生成器」。後方互換のため挙動は不変。
    """
    raw_text = event.raw_text or ""
    normalized_text = raw_text
    amount = event.amount or 0

    if event.action in (None, "amount_only"):
        return _infer_from_amount_only(amount, state, actor_seat, raw_text, normalized_text)

    return _validate_provided_action(
        event.action, amount, state, actor_seat, raw_text, normalized_text,
    )


def infer_action_distribution(
    event: AudioEvent,
    state: BettingState,
    actor_seat: Optional[int],
    prior: Optional["PriorParams"] = None,
) -> list["ActionHypothesis"]:
    """1 観測 → アクション仮説リストを返す (v6.0+ M2)。

    後方互換規約:
      max(result, key=h.log_likelihood) は ``_infer_action_core()`` の出力と
      (action, amount, needs_review, reason) が一致する。

    実装:
      1. ``_infer_action_core()`` で primary 仮説を計算 (legacy)
      2. primary を log_likelihood=0.0 で先頭に置く
      3. legal_actions に該当する他アクションを Bayesian 観測モデルで評価し、
         log_likelihood < 0 で追加 (primary が必ず top-1)
    """
    # 遅延 import: observation_model は本モジュールを TYPE_CHECKING で参照しているため
    from integration.observation_model import (
        ActionHypothesis,
        compute_log_likelihood,
        default_amount_for,
        default_priors,
        evidence_from_audio,
        is_legal,
    )

    legacy = _infer_action_core(event, state, actor_seat)
    primary = ActionHypothesis(
        action=legacy.action,
        amount=legacy.amount,
        log_likelihood=0.0,
        reason=legacy.reason,
        needs_review=legacy.needs_review,
        confidence=legacy.confidence,
        raw_text=legacy.raw_text,
        normalized_text=legacy.normalized_text,
        seat=actor_seat,
    )
    hypotheses: list[ActionHypothesis] = [primary]

    if prior is None:
        prior = default_priors()

    evidence = evidence_from_audio(event)
    legacy_action = (legacy.action or "").lower()

    for alt_action in ("fold", "call", "check", "bet", "raise"):
        if alt_action == legacy_action:
            continue
        alt_amount = default_amount_for(alt_action, state)
        if not is_legal(alt_action, alt_amount, state, actor_seat):
            continue
        log_lik = compute_log_likelihood(
            evidence, alt_action, alt_amount, state, actor_seat, prior,
        )
        # primary の log_likelihood=0 を超えないよう負側にクランプ
        log_lik = min(log_lik, -1e-6)
        hypotheses.append(ActionHypothesis(
            action=alt_action,
            amount=alt_amount,
            log_likelihood=log_lik,
            reason=f"alternative_{alt_action}",
            needs_review=False,
            confidence=0.0,
            raw_text=event.raw_text or "",
            normalized_text="",
            seat=actor_seat,
        ))

    hypotheses.sort(key=lambda h: h.log_likelihood, reverse=True)
    return hypotheses


def infer_action(
    event: AudioEvent,
    state: BettingState,
    actor_seat: Optional[int],
) -> InferredAction:
    """AudioEvent → InferredAction (既存 API)。

    M2 から ``infer_action_distribution()`` を呼び top-1 を返す薄いアダプタ。
    primary 仮説は ``_infer_action_core()`` の結果と一致するよう構築されるので
    既存挙動と完全互換 (243 テスト維持の必要十分条件)。
    """
    hypotheses = infer_action_distribution(event, state, actor_seat)
    top = hypotheses[0]
    return InferredAction(
        seat=actor_seat,
        action=top.action,
        amount=top.amount,
        confidence=top.confidence,
        needs_review=top.needs_review,
        reason=top.reason,
        raw_text=top.raw_text,
        normalized_text=top.normalized_text,
    )
