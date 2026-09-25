"""core/showdown.py

ショーダウンの勝者を **RFID で読んだ手札とボード**から決める（ADR-0062）。

pokerkit の役判定（`StandardHighHand`）を使う純粋関数で、ゲーム状態は持たない。

- `evaluate_hands`: 席ごとの最強の 5 枚と役名。
- `award_pots`: main / side pot ごとに、その pot に参加できる席のうち一番強い手へ配る。
  同じ強さは等分し、割り切れない端数は **手番の順で先の人**（ボタンの次の席から）に 1 枚ずつ配る。

手札で決めてよいのは「残った全員が手札を見せた」ときだけ。見せずにマックした人は手札が強くても
ポットを失うので、マックの扱いは呼び出し側（`integration/engine.py`）が決める。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# pokerkit の役名 → 日本語（CLI と記録の表示用）
HAND_NAMES_JA: dict[str, str] = {
    "High card": "ハイカード",
    "One pair": "ワンペア",
    "Two pair": "ツーペア",
    "Three of a kind": "スリーカード",
    "Straight": "ストレート",
    "Flush": "フラッシュ",
    "Full house": "フルハウス",
    "Four of a kind": "フォーカード",
    "Straight flush": "ストレートフラッシュ",
}


@dataclass(frozen=True)
class ShowdownHand:
    """1 席の判定結果。`value` は pokerkit の役（大小比較できる）。"""

    seat: int
    hole: tuple[str, ...]
    name: str                    # 役名（pokerkit の英語表記。例 "Two pair"）
    best: tuple[str, ...]        # 役を作る 5 枚
    value: Any = field(compare=False, repr=False)

    @property
    def name_ja(self) -> str:
        return HAND_NAMES_JA.get(self.name, self.name)

    def to_dict(self) -> dict:
        return {
            "seat": self.seat,
            "hole_cards": list(self.hole),
            "hand": self.name,
            "best": list(self.best),
        }


def evaluate_hands(hole_cards: dict[int, list[str]], board: list[str]) -> dict[int, ShowdownHand]:
    """席ごとに、手札 2 枚とボード 5 枚から最強の役を求める。"""
    from pokerkit import StandardHighHand   # rules-aware backend と同じく遅延 import

    board_text = "".join(board)
    hands: dict[int, ShowdownHand] = {}
    for seat, hole in hole_cards.items():
        hand = StandardHighHand.from_game("".join(hole), board_text)
        hands[seat] = ShowdownHand(
            seat=seat,
            hole=tuple(hole),
            name=hand.entry.label.value,
            best=tuple(repr(card) for card in hand.cards),
            value=hand,
        )
    return hands


def award_pots(
    pots: list[dict], hands: dict[int, ShowdownHand], order: list[int],
) -> tuple[dict[int, int], list[list[int]]]:
    """pot ごとに勝者へ配る額を決める。

    Args:
        pots: `[{"amount": int, "eligible_seats": [int, ...]}, ...]`（main pot が先頭）。
        hands: 手札を見せた席の判定結果。ここに無い席（マックした席など）は pot を受け取れない。
        order: 手番の順（ボタンの次の席が先頭）。同じ強さの端数をどちらに配るかに使う。

    Returns:
        (席 → 受け取る額, pot ごとの勝者の席)。

    Raises:
        ValueError: 判定できる手が 1 つも無い pot がある。
    """
    rank = {seat: i for i, seat in enumerate(order)}
    awards: dict[int, int] = {}
    winners_by_pot: list[list[int]] = []
    for pot in pots:
        amount = int(pot.get("amount", 0))
        contenders = [s for s in pot.get("eligible_seats", []) if s in hands]
        if amount <= 0:
            continue
        if not contenders:
            raise ValueError(f"pot {pot} に判定できる手がありません")
        best = max(hands[s].value for s in contenders)
        winners = sorted(
            (s for s in contenders if hands[s].value == best),
            key=lambda s: (rank.get(s, len(rank)), s),
        )
        share, remainder = divmod(amount, len(winners))
        for i, seat in enumerate(winners):
            awards[seat] = awards.get(seat, 0) + share + (1 if i < remainder else 0)
        winners_by_pot.append(winners)
    return awards, winners_by_pot
