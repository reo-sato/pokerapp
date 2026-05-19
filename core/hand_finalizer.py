"""core/hand_finalizer.py

Phase 2 予定の skeleton。

BettingState + showdown 観測 + board から ``HandSummary`` を組み立てる責務を持つ。
``integration/engine.py`` の ``_finalize_hand(winner_seat)`` を将来的に置き換える。

Phase 1 ではクラス・主要メソッド signature のみ定義し、本体は NotImplementedError stub。

設計方針:
- ``resolution_type`` を ``"legacy_winner_finalize"`` (= 旧経路マーカー) から
  ``fold_win`` / ``showdown`` / ``showdown_split`` / ``sidepot_showdown`` のいずれかへ昇格させる
- ``HandSummary.pots`` を ``settlement.compute_pot_settlements()`` で埋める
- ``HandSummary.showdown_revealed_cards`` を ``ShowdownTracker.project_to_summary_dict()`` で投影する
- ``winner_seat`` は compatibility field として、可能なら最大 payout の seat を入れる
  (split / sidepot の場合は Phase 2 で扱いを再検討)
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from core.hand_log import HandSummary, RevealedHand

if TYPE_CHECKING:
    from integration.action_inference import BettingState


class HandFinalizer:
    """BettingState + 観測から HandSummary を組み立てる。Phase 2 で実装。"""

    def finalize(
        self,
        betting_state: "BettingState",
        revealed_hands: list[RevealedHand],
        board: list[str],
        pot_total: int,
        players_info: list[dict],
        *,
        hand_id: int,
        session_id: str,
        started_at: str,
        ended_at: str,
        actions: list,
    ) -> HandSummary:
        """すべての観測情報から HandSummary を組み立てて返す。

        Phase 2 実装方針:
          1. live_seats を ``betting_state`` から算出
             (= active_seats - folded_seats)
          2. ``len(live_seats) == 1`` なら fold_win
             - ``resolution_type = "fold_win"``
             - 単一 pot で eligible = winning = [live_seats[0]]
          3. それ以外 (showdown 経路) なら:
             - revealed_hands が live_seats を完全に覆っているか確認
             - 不足なら ``resolution_status = "incomplete"`` を返す
             - ``settlement.compute_pot_settlements()`` で main / side pot を計算
             - 全 pot が単一 winner なら ``resolution_type = "showdown"``
             - いずれかの pot に複数 winner がいるなら ``showdown_split``
             - side pot が存在するなら ``sidepot_showdown``
          4. ``seat_payouts`` は ``pots`` の payouts を seat 別に合算
          5. ``showdown_revealed_cards`` は ShowdownTracker から投影
          6. ``winner_seat`` (compatibility) は最大 payout の seat。tie 時は最若の seat。

        Phase 1 ではこのメソッドは呼ばれない (engine._finalize_hand が legacy 経路で動く)。
        """
        raise NotImplementedError("Phase 2 で実装予定")
