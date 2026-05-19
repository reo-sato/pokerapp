"""core/hand_finalizer.py

Phase 2-B: BettingState + showdown 観測 + board から ``HandSummary`` を組み立てる。

``integration/engine.py`` の ``_finalize_hand(winner_seat)`` の主経路として
``HandFinalizer.finalize(...)`` を呼ぶ。``resolution_type`` は

  - ``fold_win``: live_seats が 1 人に絞れた
  - ``showdown``: 1 pot で単一 winner
  - ``showdown_split``: 1 pot で同 rank 複数 winner
  - ``sidepot_showdown``: pot が 2 つ以上 (main + side)
  - ``incomplete``: showdown 必須なのに board 不足 / hole cards 不足 / 例外

のいずれかに **昇格** させる。Phase 1 で立てていた ``legacy_winner_finalize`` marker は
本 finalizer 経由では発行されない (engine 経路全体が settlement core ベースに移行)。

``winner_seat`` (HandSummary の compatibility field) は最大 payout の seat。tie 時は
最低 seat 番号。

``winner_seat_hint`` (音声 WINNER 観測) は **補助観測**として受け取り、settlement から
導かれた primary winner と食い違う場合は ``review_required=True`` を立てる。
「Oracle 一発確定」ではなく **異常検知の材料**として扱う。

``resolution_status="incomplete"`` になる条件 (Phase 2-B 時点で _build_incomplete に
入る reason tag を列挙):

  - ``board_under_5``         : live_seats が 2 以上いるのに board が 5 枚未満
  - ``revealed_hands_missing``: showdown で必要な hole cards (live_seats のいずれか)
                                が ``revealed_hands`` に含まれていない
  - ``settlement_exception``  : ``compute_pot_settlements`` 内で例外発生
  - ``empty_pots``            : ``compute_pot_settlements`` が空 list を返した
  - ``no_live_seats``         : 全 seat が folded 等で live_seats が 0 (退化)

``incomplete`` の hand は ``resolution_type=None`` / ``pots=[]`` / ``seat_payouts={}``
が立ち、PHH gate で意図的 skip される。Phase 3+ で ``HandReconstructor`` が
retrospective に再評価して ``incomplete → final`` に昇格させる経路を作る予定。
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from core.hand_log import (
    ActionRecord,
    HandSummary,
    PotSettlement,
    RevealedHand,
    ResolutionStatus,
    ResolutionType,
)
from core.settlement import compute_pot_settlements

if TYPE_CHECKING:
    from integration.action_inference import BettingState

logger = logging.getLogger(__name__)


def _pick_primary_winner(
    seat_payouts: dict[int, int],
    fallback_hint: Optional[int],
    live_seats: list[int],
    active_seats: list[int],
) -> int:
    """``HandSummary.winner_seat`` (compatibility field) に入れる seat を決定する。

    優先順位:
      1. ``seat_payouts`` が空でなければ最大 payout の seat (tie 時は最低 seat 番号)
      2. ``fallback_hint`` (winner 音声観測) が指定されていればそれ
      3. ``live_seats`` の最低 seat
      4. ``active_seats`` の最低 seat
      5. 0 (degenerate fallback、実運用ではここに到達しないはず)
    """
    if seat_payouts:
        # 最大額 → 同額なら最低 seat 番号
        return max(seat_payouts.items(), key=lambda kv: (kv[1], -kv[0]))[0]
    if fallback_hint is not None:
        return int(fallback_hint)
    if live_seats:
        return min(live_seats)
    if active_seats:
        return min(active_seats)
    return 0


class HandFinalizer:
    """BettingState + 観測から HandSummary を組み立てる。

    settlement core (`core/settlement.py`) を呼び、resolution_type を canonical へ
    昇格させる pure logic コンポーネント。GameStateManager への stack 反映は
    呼び出し側 (engine._apply_payouts_to_gamestate) の責任。
    """

    def finalize(
        self,
        betting_state: "BettingState",
        board: list[str],
        revealed_hands: list[RevealedHand],
        pot_total: int,
        players_info: list[dict],
        *,
        hand_id: int,
        session_id: str,
        started_at: str,
        ended_at: str,
        blinds: dict,
        actions: list[ActionRecord],
        board_source: str = "",
        winner_seat_hint: Optional[int] = None,
    ) -> HandSummary:
        """すべての観測情報から HandSummary を組み立てて返す。

        Args:
            betting_state: ``active_seats`` / ``folded_seats`` / ``all_in_seats`` /
                ``player_contrib_hand`` を持つオブジェクト。
            board: 公開ボードカード (空 〜 5 枚)。
            revealed_hands: showdown で明かされた hole cards の集合。
            pot_total: 現時点の総 pot 額。
            players_info: ``HandSummary.players`` へ入る per-seat dict のリスト。
                ``stack_end`` は finalizer の責務ではないので、呼び出し側で post-payout
                時点に更新すること。
            hand_id, session_id, started_at, ended_at, blinds, actions:
                ``HandSummary`` の他の必須フィールド。
            board_source: ボード情報のソース ("rfid" / "manual" / 等)。
            winner_seat_hint: 音声 WINNER 観測など。**補助観測** で、settlement と
                食い違う場合は ``review_required=True`` のトリガとして使う。
        """
        active_seats = sorted(set(betting_state.active_seats or []))
        folded_seats = sorted(set(betting_state.folded_seats or []))
        all_in_seats = sorted(set(betting_state.all_in_seats or []))
        live_seats = [s for s in active_seats if s not in set(folded_seats)]

        # 既存 ActionRecord 由来の派生 list (Phase 1 までと同じ集計ルール)
        folded_from_actions = [a.seat for a in actions if a.action == "fold"]
        all_in_from_actions = [a.seat for a in actions if a.action == "allin"]

        # ── 分岐 1: fold win ─────────────────────────────────────────────
        if len(live_seats) == 1:
            return self._build_fold_win(
                betting_state=betting_state,
                live_seat=live_seats[0],
                active_seats=active_seats,
                folded_from_actions=folded_from_actions,
                all_in_from_actions=all_in_from_actions,
                pot_total=pot_total,
                players_info=players_info,
                board=board,
                board_source=board_source,
                hand_id=hand_id,
                session_id=session_id,
                started_at=started_at,
                ended_at=ended_at,
                blinds=blinds,
                actions=actions,
                winner_seat_hint=winner_seat_hint,
            )

        # ── 分岐 2: showdown 候補 (live >= 2) ─────────────────────────────
        if len(live_seats) >= 2:
            # 必要 board / revealed cards のチェック
            if len(board) < 5:
                logger.info(
                    "Hand %d: incomplete (live_seats=%s, board has only %d cards, need 5).",
                    hand_id, live_seats, len(board),
                )
                return self._build_incomplete(
                    reason="board_under_5",
                    betting_state=betting_state,
                    active_seats=active_seats,
                    live_seats=live_seats,
                    folded_from_actions=folded_from_actions,
                    all_in_from_actions=all_in_from_actions,
                    pot_total=pot_total,
                    players_info=players_info,
                    board=board,
                    board_source=board_source,
                    hand_id=hand_id,
                    session_id=session_id,
                    started_at=started_at,
                    ended_at=ended_at,
                    blinds=blinds,
                    actions=actions,
                    winner_seat_hint=winner_seat_hint,
                )

            revealed_set = {rh.seat for rh in revealed_hands}
            missing = [s for s in live_seats if s not in revealed_set]
            if missing:
                logger.info(
                    "Hand %d: incomplete (live_seats=%s, missing revealed hole cards for %s).",
                    hand_id, live_seats, missing,
                )
                return self._build_incomplete(
                    reason="revealed_hands_missing",
                    betting_state=betting_state,
                    active_seats=active_seats,
                    live_seats=live_seats,
                    folded_from_actions=folded_from_actions,
                    all_in_from_actions=all_in_from_actions,
                    pot_total=pot_total,
                    players_info=players_info,
                    board=board,
                    board_source=board_source,
                    hand_id=hand_id,
                    session_id=session_id,
                    started_at=started_at,
                    ended_at=ended_at,
                    blinds=blinds,
                    actions=actions,
                    winner_seat_hint=winner_seat_hint,
                )

            # ── settlement core を呼ぶ ──────────────────────────────────
            try:
                pots = compute_pot_settlements(betting_state, revealed_hands, board)
            except Exception:
                logger.exception(
                    "Hand %d: compute_pot_settlements raised; classifying as incomplete.",
                    hand_id,
                )
                return self._build_incomplete(
                    reason="settlement_exception",
                    betting_state=betting_state,
                    active_seats=active_seats,
                    live_seats=live_seats,
                    folded_from_actions=folded_from_actions,
                    all_in_from_actions=all_in_from_actions,
                    pot_total=pot_total,
                    players_info=players_info,
                    board=board,
                    board_source=board_source,
                    hand_id=hand_id,
                    session_id=session_id,
                    started_at=started_at,
                    ended_at=ended_at,
                    blinds=blinds,
                    actions=actions,
                    winner_seat_hint=winner_seat_hint,
                )

            if not pots:
                logger.info("Hand %d: incomplete (compute_pot_settlements returned []).", hand_id)
                return self._build_incomplete(
                    reason="empty_pots",
                    betting_state=betting_state,
                    active_seats=active_seats,
                    live_seats=live_seats,
                    folded_from_actions=folded_from_actions,
                    all_in_from_actions=all_in_from_actions,
                    pot_total=pot_total,
                    players_info=players_info,
                    board=board,
                    board_source=board_source,
                    hand_id=hand_id,
                    session_id=session_id,
                    started_at=started_at,
                    ended_at=ended_at,
                    blinds=blinds,
                    actions=actions,
                    winner_seat_hint=winner_seat_hint,
                )

            # ── resolution_type の判定 ─────────────────────────────────
            if len(pots) >= 2:
                resolution_type: ResolutionType = "sidepot_showdown"
            elif len(pots[0].winning_seats) > 1:
                resolution_type = "showdown_split"
            else:
                resolution_type = "showdown"

            # ── seat_payouts: 全 pot から seat 別に合算 ─────────────────
            seat_payouts: dict[int, int] = {}
            for pot in pots:
                for seat, amount in pot.payouts.items():
                    seat_payouts[seat] = seat_payouts.get(seat, 0) + amount

            primary_winner = _pick_primary_winner(
                seat_payouts, winner_seat_hint, live_seats, active_seats,
            )

            showdown_revealed = {rh.seat: list(rh.cards) for rh in revealed_hands}

            # winner_hint と settlement の食い違いは review_required に乗せる
            review_required = any(getattr(a, "needs_review", False) for a in actions)
            if winner_seat_hint is not None and seat_payouts:
                if winner_seat_hint not in seat_payouts or seat_payouts.get(winner_seat_hint, 0) == 0:
                    review_required = True
                    logger.warning(
                        "Hand %d: winner_seat_hint=%s not among payout receivers %s; "
                        "marking review_required.",
                        hand_id, winner_seat_hint, sorted(seat_payouts.keys()),
                    )

            return HandSummary(
                hand_id=hand_id,
                session_id=session_id,
                started_at=started_at,
                ended_at=ended_at,
                blinds=dict(blinds),
                board=list(board),
                board_source=board_source,
                players=list(players_info),
                pot_total=int(pot_total),
                winner_seat=primary_winner,
                actions=list(actions),
                review_required=review_required,
                folded_seats=folded_from_actions,
                all_in_seats=all_in_from_actions,
                resolution_status="final",
                resolution_type=resolution_type,
                seat_payouts=seat_payouts,
                showdown_revealed_cards=showdown_revealed,
                pots=list(pots),
            )

        # ── 分岐 3: live_seats が 0 (退化) → incomplete ──────────────────
        return self._build_incomplete(
            reason="no_live_seats",
            betting_state=betting_state,
            active_seats=active_seats,
            live_seats=live_seats,
            folded_from_actions=folded_from_actions,
            all_in_from_actions=all_in_from_actions,
            pot_total=pot_total,
            players_info=players_info,
            board=board,
            board_source=board_source,
            hand_id=hand_id,
            session_id=session_id,
            started_at=started_at,
            ended_at=ended_at,
            blinds=blinds,
            actions=actions,
            winner_seat_hint=winner_seat_hint,
        )

    # ────────────────────────────────────────────────────────────────────
    # builders
    # ────────────────────────────────────────────────────────────────────

    def _build_fold_win(
        self,
        *,
        betting_state: "BettingState",
        live_seat: int,
        active_seats: list[int],
        folded_from_actions: list[int],
        all_in_from_actions: list[int],
        pot_total: int,
        players_info: list[dict],
        board: list[str],
        board_source: str,
        hand_id: int,
        session_id: str,
        started_at: str,
        ended_at: str,
        blinds: dict,
        actions: list[ActionRecord],
        winner_seat_hint: Optional[int],
    ) -> HandSummary:
        amount = int(pot_total)
        pot = PotSettlement(
            amount=amount,
            eligible_seats=list(active_seats),  # fold 含む全 active が pot 原資
            winning_seats=[live_seat],
            payouts={live_seat: amount} if amount > 0 else {},
            pot_type="main",
        )
        seat_payouts = {live_seat: amount} if amount > 0 else {}

        review_required = any(getattr(a, "needs_review", False) for a in actions)
        if winner_seat_hint is not None and winner_seat_hint != live_seat:
            review_required = True
            logger.warning(
                "Hand %d: winner_seat_hint=%s differs from fold_win primary winner=%s; "
                "marking review_required.",
                hand_id, winner_seat_hint, live_seat,
            )

        return HandSummary(
            hand_id=hand_id,
            session_id=session_id,
            started_at=started_at,
            ended_at=ended_at,
            blinds=dict(blinds),
            board=list(board),
            board_source=board_source,
            players=list(players_info),
            pot_total=amount,
            winner_seat=live_seat,
            actions=list(actions),
            review_required=review_required,
            folded_seats=folded_from_actions,
            all_in_seats=all_in_from_actions,
            resolution_status="final",
            resolution_type="fold_win",
            seat_payouts=seat_payouts,
            showdown_revealed_cards={},
            pots=[pot],
        )

    def _build_incomplete(
        self,
        *,
        reason: str,
        betting_state: "BettingState",
        active_seats: list[int],
        live_seats: list[int],
        folded_from_actions: list[int],
        all_in_from_actions: list[int],
        pot_total: int,
        players_info: list[dict],
        board: list[str],
        board_source: str,
        hand_id: int,
        session_id: str,
        started_at: str,
        ended_at: str,
        blinds: dict,
        actions: list[ActionRecord],
        winner_seat_hint: Optional[int],
    ) -> HandSummary:
        # incomplete: settlement 不能。winner_seat (compat) は hint または fallback。
        primary = _pick_primary_winner(
            seat_payouts={},
            fallback_hint=winner_seat_hint,
            live_seats=live_seats,
            active_seats=active_seats,
        )
        # Phase 2-B では incomplete = 自動的に review_required を立てる方針。
        # PHH gate でも skip 対象になる。
        return HandSummary(
            hand_id=hand_id,
            session_id=session_id,
            started_at=started_at,
            ended_at=ended_at,
            blinds=dict(blinds),
            board=list(board),
            board_source=board_source,
            players=list(players_info),
            pot_total=int(pot_total),
            winner_seat=primary,
            actions=list(actions),
            review_required=True,  # incomplete は常に review 対象
            folded_seats=folded_from_actions,
            all_in_seats=all_in_from_actions,
            resolution_status="incomplete",
            resolution_type=None,
            seat_payouts={},
            showdown_revealed_cards={},
            pots=[],
        )
