"""core/stackless.py

持ち点を数えないモード（オーナー 2026-10-24。リングでもトーナメントでも使う）の記録の形。

持ち点は分からないものとして扱い、オールインはオールインとだけ記録する（額は書かない）。ゲーム状態は内部で十分大きい
持ち点を持って回す（`core/poker_engine.PokerkitGameState(stackless=True)`）ので、その内部の値が記録に出ないように、
ハンドの記録（`HandSummary.to_dict`）からここで外す:

- アクション: オールインの額・オールインに向き合ったコールの額（オールインの額の分が分からない）は ``None``。
  持ち点（``stack_after``）はいつも ``None``。ポット（``pot_after``）はそのハンドで最初のオールインから ``None``。
- 席: 始め・終わりの持ち点はいつも ``None``。オールインのあるハンドでは、オールインに関わった席の収支（``result``）は
  ``None`` にして ``allin``（won / lost / unknown）を付ける。最初のオールインより前に降りた席の収支は残す（2026-11-05）。
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


def _winners(data: dict) -> set[int]:
    seats: set[int] = set()
    if isinstance(data.get("winner_seat"), int):
        seats.add(data["winner_seat"])
    awards = data.get("pot_awards")
    if isinstance(awards, dict):                     # 真のアクションの形 {席: 額}
        seats |= {int(s) for s, v in awards.items() if str(s).isdigit() and v != 0}
    for award in awards if isinstance(awards, list) else []:
        if isinstance(award, dict) and isinstance(award.get("seat"), int) and award.get("amount") != 0:
            seats.add(award["seat"])
    return seats


def allin_seats(data: dict) -> set[int]:
    """オールインに関わった席: オールインした席・最初のオールインのあとでチップを出した席（コール・レイズ…）・勝った席。
    この席の収支は分からない。最初のオールインより前に出しただけで降りた席の収支は分かる。オールインの無いハンドは空。"""
    actions = [a for a in data.get("actions") or [] if isinstance(a, dict)]
    first = next((i for i, a in enumerate(actions) if a.get("action") in ("allin", "all_in")), None)
    if first is None:
        return set()
    seats = {a["seat"] for a in actions[first:]
             if isinstance(a.get("seat"), int) and a.get("action") not in ("fold", "check")}
    return seats | _winners(data)


def mark_allin(data: dict) -> dict:
    """オールインに関わった席に `allin` = won（勝った）/ lost / unknown（勝者が決まらない）を付ける（ハンド履歴の画面が
    収支の代わりに「all-in」を勝ち 緑・負け 赤で出す, オーナー 2026-11-05）。収支（`result`）は変えない。冪等。"""
    involved = allin_seats(data)
    if not involved:
        return data
    winners = _winners(data)
    undetermined = data.get("winner_source") == "undetermined"
    for p in data.get("players") or []:
        if isinstance(p, dict) and p.get("seat") in involved:
            p["allin"] = "unknown" if undetermined else "won" if p["seat"] in winners else "lost"
    return data


def strip_hand(data: dict) -> dict:
    """ハンドの記録（dict）から持ち点と分からない額を外す（その dict を書き換えて返す）。

    オールインのあるハンドの収支: オールインに関わった席（`allin_seats`）は分からないので ``None`` にして `allin` の印を
    付ける。最初のオールインより前に出しただけで降りた席は、出した額がすべて分かっているのでそのまま残す（2026-11-05）。
    """
    data["stackless"] = True
    actions = [a for a in data.get("actions") or [] if isinstance(a, dict)]
    flags = unknown_amounts(actions)
    had_allin = any(pot for _, pot in flags)
    involved = allin_seats(data) if had_allin else set()
    mark_allin(data)
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
            if had_allin and (p.get("seat") in involved or not isinstance(p.get("result"), int)):
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
