"""core/settlement.py

Phase 2-A: settlement core 実装。

役割:
1. BettingState の seat 別 contribution と RevealedHand から、main / side pot を含む
   全 pot の決済 (``list[PotSettlement]``) を計算する。
2. hole cards + board から hand rank を pokerkit で評価する。
3. split pot を winning_seats へ均等分配し、odd chip は最低 seat 番号優先で割り当てる。

依存: ``pokerkit.StandardHighHand`` (Texas Hold'em の 7-card best-5 評価)。

このモジュールは pure logic で BettingState を read-only に扱う。HandFinalizer や
engine._finalize_hand への組み込みは Phase 2-B 以降。
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol

from core.hand_log import PotSettlement, RevealedHand

if TYPE_CHECKING:
    from integration.action_inference import BettingState

logger = logging.getLogger(__name__)


class _BettingStateLike(Protocol):
    """compute_pot_settlements が読み取る最小限のインターフェース。

    本物の ``BettingState`` (``integration.action_inference``) と Phase 2-A テストの
    stub の両方を受け入れるための duck-typing 用 Protocol。
    """

    player_contrib_hand: dict[int, int]
    folded_seats: list[int]


# ────────────────────────────────────────────────────────────────────────────
# distribute_split_pot
# ────────────────────────────────────────────────────────────────────────────


def distribute_split_pot(
    pot_amount: int,
    winning_seats: list[int],
    button_seat: int | None = None,
) -> dict[int, int]:
    """split pot を winning_seats へ均等分配し payouts dict を返す。

    odd chip 配分ルール: **最低 seat 番号から順に +1 を付与する** (lowest-seat priority)。
    例: ``distribute_split_pot(1001, [2, 5, 9])`` → ``{2: 334, 5: 334, 9: 333}``。

    ``button_seat`` 引数は将来 (button 隣優先など別ルールに切り替えるための) 拡張ポイント
    として受け入れるが、Phase 2-A では未使用。

    Args:
        pot_amount: 分配する pot の総額 (非負整数)。
        winning_seats: 同 rank の勝者 seat 集合 (1 要素以上)。
        button_seat: ボタン席 (将来の odd chip rule 切り替え用、現状未使用)。

    Returns:
        ``{seat: payout, ...}``。``sum(payouts.values()) == pot_amount`` が必ず成り立つ。
        ``winning_seats`` が空なら空 dict。
    """
    if not winning_seats:
        return {}
    n = len(winning_seats)
    base = pot_amount // n
    remainder = pot_amount % n
    sorted_seats = sorted(winning_seats)
    return {
        seat: base + (1 if i < remainder else 0)
        for i, seat in enumerate(sorted_seats)
    }


# ────────────────────────────────────────────────────────────────────────────
# evaluate_hand_rank
# ────────────────────────────────────────────────────────────────────────────


def evaluate_hand_rank(hole_cards: list[str], board: list[str]) -> int:
    """Texas Hold'em の hole_cards + board から best-5 役の強さを整数で返す。

    高い値ほど強い hand。pokerkit の ``StandardHighHand`` の ``entry.index`` を採用。
    役種 (high card → straight flush) とキッカーまでエンコードされているので、
    数値比較で winner 判定および tie (split pot) 判定が可能。

    **前提とスコープ (重要)**:
      - 本関数は **Texas Hold'em + StandardHighHand (high-hand)** を前提とする。
        「entry.index が大きいほど強い」の単調性は pokerkit の StandardHighHand
        ルックアップテーブルが保証している性質であり、それ以外の variant で
        そのまま使うと正しくない:
          * **Lowball** (2-7 / A-5): 弱い手ほど強い → 単調性の向きが逆
          * **Hi/Lo split** (Omaha Hi/Lo 等): 別途 low hand 評価器が必要
          * **Short deck (6+)**: フラッシュとフルハウスの順位が変わる
        他 variant に拡張するときは別の評価関数 (例: ``evaluate_lowball_hand_rank``)
        を追加し、HandFinalizer 側で variant に応じて使い分ける設計に拡張する。
      - 入力カード枚数は 7 枚 (hole 2 + board 5) が想定形。少ない枚数の hand は
        呼び出し側で finalization 可能性を判断する責務 (HandFinalizer の incomplete
        判定で除外される)。

    Args:
        hole_cards: ホールカード文字列のリスト (例: ``["Ah", "Kd"]``)。
        board: ボードカード文字列のリスト (例: ``["Qh", "Jh", "Th", "2c", "3d"]``)。

    Returns:
        役の強さを表す整数。同じ役 + 同じキッカー列なら同じ値 (split pot 対象)。

    Notes:
        - 重複カード等のバリデーションは pokerkit に委ねる (例外はそのまま伝播)。
    """
    from pokerkit import StandardHighHand

    hole_str = "".join(hole_cards)
    board_str = "".join(board)
    return StandardHighHand.from_game(hole_str, board_str).entry.index


# ────────────────────────────────────────────────────────────────────────────
# compute_pot_settlements
# ────────────────────────────────────────────────────────────────────────────


def compute_pot_settlements(
    betting_state: "_BettingStateLike",
    revealed_hands: list[RevealedHand],
    board: list[str],
) -> list[PotSettlement]:
    """各 seat の累積投入額と revealed hands から main / side pot を構築・決済する。

    用語 (関数内で一貫):
      - ``eligible_seats``: その pot を**勝ちうる** seat 集合 = その層に出資した
        seat のうち fold していないもの。fold 済み seat の chips は pot の原資には
        残るが、彼ら自身は eligible には入らない。
      - ``contenders``: ``eligible_seats`` のうち**現時点で revealed hand があり
        rank 評価できる** seat 集合 = ``eligible_seats ∩ {rh.seat for rh in
        revealed_hands}``。muck した seat や RFID 未観測 seat は eligible だが
        contenders から外れる。
      - winning_seats は contenders の中で最大 rank の seat 群 (split 含む)。
      - Phase 2-B の HandFinalizer は「eligible はいるが contenders が空 / 不足」
        を ``incomplete`` の判定材料として利用する。

    アルゴリズム (Stratified Side Pot Decomposition):
      1. ``betting_state.player_contrib_hand`` の正の値だけを残した contrib dict を作る。
         fold 済み seat の contribution も含む (彼らの金は pot の原資)。
      2. ``contrib`` が空になるまで以下を繰り返し、各イテレーションが 1 つの pot 層を生成:
         a. ``min_pos = min(contrib.values())`` でこの層の最小投入額を求める
         b. ``layer_seats = list(contrib.keys())`` がこの層への出資者全員
         c. ``pot_amount = min_pos * len(layer_seats)`` がこの層の pot サイズ
         d. ``eligible_seats = layer_seats - folded_seats`` (sorted) がこの pot を争う seat 集合
         e. ``contenders = eligible & revealed`` のみ hand rank を評価し最大値の seat 群を winners に
         f. ``distribute_split_pot(pot_amount, winners)`` で payouts を決定
         g. ``PotSettlement`` を append し、最初の層を ``"main"``、それ以降を ``"side"`` とマーク
         h. 全 seat の contrib から ``min_pos`` を差し引き、残額 > 0 の seat だけを次層へ
      3. 結果の ``list[PotSettlement]`` を返す (main → side1 → side2 → … の順)。

    Args:
        betting_state: ``player_contrib_hand`` / ``folded_seats`` を持つオブジェクト
            (本物の ``BettingState`` または duck-typed stub)。
        revealed_hands: showdown で hole cards が明かされた seat 集合。
        board: 公開ボードカード。

    Returns:
        pot 別の決済結果リスト。``len(result) == 1`` なら side pot なし。
    """
    revealed_by_seat: dict[int, list[str]] = {rh.seat: list(rh.cards) for rh in revealed_hands}
    folded = set(betting_state.folded_seats or [])

    # 正の contribution のみコピー
    contrib: dict[int, int] = {
        seat: amount
        for seat, amount in (betting_state.player_contrib_hand or {}).items()
        if amount > 0
    }

    pots: list[PotSettlement] = []
    layer_index = 0

    while contrib:
        min_pos = min(contrib.values())
        layer_seats = list(contrib.keys())
        pot_amount = min_pos * len(layer_seats)
        eligible = sorted(s for s in layer_seats if s not in folded)

        contenders = [s for s in eligible if s in revealed_by_seat]
        if contenders:
            ranks = {
                s: evaluate_hand_rank(revealed_by_seat[s], board)
                for s in contenders
            }
            top = max(ranks.values())
            winning_seats = sorted(s for s, r in ranks.items() if r == top)
        elif eligible:
            # 退化ケース: eligible だが誰も reveal していない。
            # Phase 2-A スコープ外だが防御的に「全 eligible で split」して potを失わない。
            logger.warning(
                "Pot layer with eligible seats %s but no revealed hands; "
                "defaulting to even split (test scenario should avoid this).",
                eligible,
            )
            winning_seats = list(eligible)
        else:
            # 全 eligible が folded (理論上ありえない退化ケース)
            logger.warning(
                "Pot layer with no eligible seats (all folded); "
                "amount %d unassigned.", pot_amount,
            )
            winning_seats = []

        payouts = distribute_split_pot(pot_amount, winning_seats)
        pots.append(PotSettlement(
            amount=pot_amount,
            eligible_seats=eligible,
            winning_seats=winning_seats,
            payouts=payouts,
            pot_type="main" if layer_index == 0 else "side",
        ))
        layer_index += 1

        # 次層: 各 seat から min_pos を引き、残りが 0 の seat は除外
        contrib = {
            seat: amount - min_pos
            for seat, amount in contrib.items()
            if amount - min_pos > 0
        }

    return pots
