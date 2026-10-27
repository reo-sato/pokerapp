"""core/stackless.py

持ち点を数えないモード（オーナー 2026-10-24。リングでもトーナメントでも使う）の記録の形。

持ち点は分からないものとして扱い、オールインはオールインとだけ記録する（額は書かない）。ゲーム状態は内部で十分大きい
持ち点を持って回す（`core/poker_engine.PokerkitGameState(stackless=True)`）ので、その内部の値が記録に出ないように、
ハンドの記録（`HandSummary.to_dict`）からここで外す:

- アクション: オールインの額・オールインに向き合ったコールの額（オールインの額の分が分からない）は ``None``。
  持ち点（``stack_after``）はいつも ``None``。ポット（``pot_after``）はそのハンドで最初のオールインから ``None``。
- 席: 始め・終わりの持ち点はいつも ``None``。収支（``result``）はオールインの無いハンドだけ残す。
- ポット（``pot_total`` / ``pots`` / ``pot_awards`` の額）: オールインの無いハンドだけ残す。
- ハンドに ``"stackless": true`` を付ける（読む側が「額が無い」ことを知るため）。

オールインの無いハンドは、出した額（ベット・レイズ・コール）とポットがそのまま正しい。
"""
from __future__ import annotations

from typing import Any

_AGGRESSIVE = frozenset({"bet", "raise"})


def _get(a: Any, key: str) -> Any:
    return a.get(key) if isinstance(a, dict) else getattr(a, key, None)


def unknown_amounts(actions: list) -> list[tuple[bool, bool]]:
    """各アクションの (額が分からない, ポットが分からない)。dict でも ActionRecord でもよい（記録の順）。

    額が分からない = オールイン・オールインに向き合ったコール（ストリートの最後の賭けがオールイン）。
    ポットが分からない = そのハンドで最初のオールインから。
    """
    out: list[tuple[bool, bool]] = []
    had_allin = False
    unknown_price = False                 # いまのストリートの賭けの額にオールイン（額が分からない）が入っている
    street: Any = object()
    for a in actions:
        if _get(a, "street") != street:
            street = _get(a, "street")
            unknown_price = False
        action = _get(a, "action")
        unknown = False
        if action in ("allin", "all_in"):
            had_allin = unknown_price = unknown = True
        elif action in _AGGRESSIVE:
            unknown_price = False
        elif action == "call" and unknown_price:
            unknown = True
        out.append((unknown, had_allin))
    return out


def strip_hand(data: dict) -> dict:
    """ハンドの記録（dict）から持ち点と分からない額を外す（その dict を書き換えて返す）。"""
    data["stackless"] = True
    actions = [a for a in data.get("actions") or [] if isinstance(a, dict)]
    flags = unknown_amounts(actions)
    had_allin = any(pot for _, pot in flags)
    for a, (unknown, pot_unknown) in zip(actions, flags):
        if unknown:
            a["amount"] = None
        a["stack_after"] = None
        if pot_unknown:
            a["pot_after"] = None
    for p in data.get("players") or []:
        if isinstance(p, dict):
            p["stack_start"] = None
            p["stack_end"] = None
            if had_allin:
                p["result"] = None
    if had_allin:
        data["pot_total"] = None
        data["pots"] = []
        awards = data.get("pot_awards")
        if isinstance(awards, dict):                 # 真のアクションの形 {席: 額}
            data["pot_awards"] = {seat: None for seat in awards}
        for award in awards if isinstance(awards, list) else []:
            if isinstance(award, dict):
                award["amount"] = None
    return data
