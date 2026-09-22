"""core/engine_types.py

game-state 境界（`PokerEngine`）が共有する値型。`core/poker_engine.py` と
`core/game_state.py` の双方が参照するため、循環 import を避ける中立モジュールに置く
（ADR-0009）。`core.poker_engine.LegalContext` として後方互換に再エクスポートされる。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class LegalContext:
    """ある時点の合法手プリオール（推定・訂正が読む, ADR-0009 §2）。

    - `min_raise` / `max_raise` は "to" 総額（raise/bet 不可なら 0）。
    - `amount_to_call` は call に必要な追加額（check 可なら 0）。
    - `bb` はビッグブラインド額（金額 snap の review 閾値・round 寄せに使う, ADR-A S4/V4。
      legacy stub は 0 = 従来挙動）。
    - `committed` は actor の当ストリート既コミット額（レイズ額の to/by 曖昧性検知に使う,
      ADR-A S3。legacy stub は 0）。
    """

    actor_seat: Optional[int]
    legal_actions: frozenset[str]   # subset of {"fold","check","call","bet","raise","allin"}
    amount_to_call: int
    min_raise: int
    max_raise: int
    bb: int = 0
    committed: int = 0
