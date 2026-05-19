"""core/settlement.py

Phase 2 予定の skeleton。

役割:
1. BettingState の seat 別 contribution と RevealedHand から、main / side pot を含む
   全 pot の決済 (``list[PotSettlement]``) を計算する。
2. hole cards + board から hand rank を評価する (pokerkit 連携)。

Phase 1 では関数 signature のみ NotImplementedError stub で配置する。
Phase 1 の ``HandSummary.pots`` は空のままで、Phase 2 でこの module が埋める入口になる。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from core.hand_log import PotSettlement, RevealedHand

if TYPE_CHECKING:
    from integration.action_inference import BettingState


def compute_pot_settlements(
    betting_state: "BettingState",
    revealed_hands: list[RevealedHand],
    board: list[str],
) -> list[PotSettlement]:
    """各 seat の累積投入額と revealed hands から main / side pot を構築・決済する。

    Phase 2 実装方針:
      1. 各 seat の ``player_contrib_hand`` を all-in 額で stratify し、
         main pot と複数の side pot を生成する
      2. 各 pot ごとに eligible_seats を確定する
         (= その pot に到達するまでに fold しなかった seat 集合)
      3. 各 pot で eligible のうち revealed_hands から ``evaluate_hand_rank`` で
         勝者を選び、payouts を割り振る
      4. split pot の場合は均等分配。odd chip 配分は別 issue (preference: button 隣の
         lowest seat に優先付与 / 保留中)
      5. 結果を ``list[PotSettlement]`` で返す
    """
    raise NotImplementedError("Phase 2 で実装予定")


def evaluate_hand_rank(hole_cards: list[str], board: list[str]) -> int:
    """7-card (hole_cards 2 + board 5) から hand rank の整数値を返す。

    高い値が強い手。比較で順位決定する。

    Phase 2 実装方針:
      - pokerkit の ``Hand`` 評価器 (``StandardHighHand`` 等) を利用する
      - 入力 cards のバリデーション (重複なし、形式チェック) を含める
    """
    raise NotImplementedError("Phase 2 で実装予定")


def distribute_split_pot(
    pot_amount: int,
    winning_seats: list[int],
    button_seat: int | None = None,
) -> dict[int, int]:
    """split pot を winning_seats へ均等分配し payouts dict を返す。

    Phase 2 実装方針:
      - base = pot_amount // len(winning_seats), remainder は odd chips
      - odd chip handling は保留中 (preference: button 隣の lowest seat 優先)
      - 暫定: remainder は seat 番号昇順で 1 chip ずつ加算
    """
    raise NotImplementedError("Phase 2 で実装予定")
