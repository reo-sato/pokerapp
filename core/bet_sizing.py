"""core/bet_sizing.py

賭けの額の事前の重み（オーナー 2026-10-01:「現在のポットサイズに対してリーズナブルな額（多くはポットサイズ以下、
多くても 3 倍）に重み付けをすると良いかもしれません」）。

聞き取った額に候補が 2 つ以上あるとき（第 2 の耳の額ごとの点数・音の近さ）、その場面で使える額（最小ベット・レイズ〜
オールイン）のうち、音の点数にこの重みを足して選ぶ（`integration/engine.py:_choose_amount`）。音がはっきり違えば
音が勝ち、音が近いときだけ重みが効く（弱い事前）。

賭けの大きさ = 上乗せ分 ÷ コールしたあとのポット（ポットの何倍か。ベットはベット ÷ ポット）。真のアクションの賭け
92（店舗 09-27〜10-01）では 0.5 倍以下 30・0.5〜1 倍 39・1〜1.5 倍 11・1.5〜2 倍 7・2〜3 倍 4・3 倍より大きい 1
（ヘッズアップのプリフロップの 1800 = 4 倍）。データの傾きはこれより急だが、一人で打ったデータの賭け方を
そのまま事前にしない（監査）ので、オーナーの言う形（ポット以下は同じ・その先はゆるく）を弱めに置いた。
オールイン（持ち点の全部）はポットとの比によらず重みを付けない。
"""
from __future__ import annotations

from typing import Optional

from core.engine_types import LegalContext

# ポットの何倍まで重みを付けないか / そこから 3 倍までの 1 倍あたりの重み（log）/ 3 倍より先の 1 倍あたり / 下限。
# 3 倍より先は 2 → 1、下限は −8 → −4（監査 3 回目, 2026-10-03: 本当の大きな賭け（真のアクションの 92 のうち 1 = 4 倍）に
# −4 が付き、第 2 の耳の表の別の額（−2 + 0.4 × 差）に上書きされうる。いまは 4 倍で −3、どれだけ大きくても −4 まで）
POT_FREE = 1.0
POT_SOFT_LIMIT = 3.0
POT_SLOPE = 1.0
POT_STEEP_SLOPE = 1.0
POT_PRIOR_FLOOR = -4.0


def pot_fraction(amount: int, ctx: LegalContext, pot: int) -> Optional[float]:
    """額 `amount`（レイズはその額まで = 合計）の賭けが、コールしたあとのポットの何倍か。賭けられない場面なら None。

    `pot` = いまのポット（このストリートの賭けを含む、ハンドで出た額の合計）。
    """
    if ctx.actor_seat is None:
        return None
    current = ctx.committed + ctx.amount_to_call          # いまのベット（この人がそろえる額）
    after_call = max(1, pot + ctx.amount_to_call)
    return max(0, amount - current) / after_call


def size_prior(ratio: Optional[float]) -> float:
    """ポットの何倍かの賭けの事前の重み（log。0 が最も自然、負ほど不自然）。"""
    if ratio is None or ratio <= POT_FREE:
        return 0.0
    weight = -POT_SLOPE * (min(ratio, POT_SOFT_LIMIT) - POT_FREE)
    if ratio > POT_SOFT_LIMIT:
        weight -= POT_STEEP_SLOPE * (ratio - POT_SOFT_LIMIT)
    return max(POT_PRIOR_FLOOR, weight)


def amount_prior(amount: int, ctx: LegalContext, pot: int) -> float:
    """額 `amount` の事前の重み（`size_prior`）。オールイン（持ち点の全部）は 0。"""
    if ctx.max_raise and amount >= ctx.max_raise:
        return 0.0
    return size_prior(pot_fraction(amount, ctx, pot))


def hand_wager_ratios(actions: list[dict], blinds: dict, position_map: dict) -> dict[int, float]:
    """記録のハンドの行（`actions` の順。コールの額は上乗せ分、ベット・レイズは合計）から、ベット・レイズの行の番号 →
    その賭けがコールしたあとのポットの何倍か。ブラインドの席は `position_map`（SB・BB。ヘッズアップは BTN が SB）。
    オールインの行は数えない（ポットとの比によらず自然）。ブラインドが分からなければ空。"""
    sb, bb = int((blinds or {}).get("sb") or 0), int((blinds or {}).get("bb") or 0)
    pos = {int(k): v for k, v in (position_map or {}).items()}
    sb_seat = next((s for s, v in pos.items() if v == "SB"), None)
    if sb_seat is None and len(pos) == 2:
        sb_seat = next((s for s, v in pos.items() if v == "BTN"), None)
    bb_seat = next((s for s, v in pos.items() if v == "BB"), None)
    if not bb or bb_seat is None:
        return {}
    committed: dict[int, int] = {bb_seat: bb}
    pot, bet, street = bb, bb, "preflop"
    if sb_seat is not None:
        committed[sb_seat] = sb
        pot += sb
    out: dict[int, float] = {}
    for i, a in enumerate(actions):
        st = a.get("street")
        if st not in ("preflop", "flop", "turn", "river"):
            continue
        if st != street:
            street, committed, bet = st, {}, 0
        seat, act, amount = int(a.get("seat") or 0), a.get("action"), int(a.get("amount") or 0)
        c = committed.get(seat, 0)
        if act == "call":
            inc = amount if amount > 0 else max(0, bet - c)
            committed[seat] = c + inc
            pot += inc
        elif act in ("bet", "raise", "allin") and amount > bet:
            if act != "allin":
                out[i] = (amount - bet) / max(1, pot + bet - c)
            pot += amount - c
            committed[seat] = amount
            bet = amount
    return out
