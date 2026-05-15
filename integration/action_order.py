"""integration/action_order.py

ディーラーボタン位置を起点とした SB / BB / first actor / 次手番の算出ヘルパ。
pure functions のみを置き、状態は持たない。

active_seats は座席番号 (1..9) の集合。順序は seat 番号を昇順に並べた円環として
扱う (1→2→...→9→1)。これは物理的卓配置の clockwise に相当する
(右隣 = 次の番号)。

呼び出し側の責務:
  - スタック 0 / 着席していない seat は active_seats から除外しておく
  - post-flop の first actor 計算では folded / all-in 済みも除外したものを
    live_seats として渡す
"""
from __future__ import annotations

from typing import Iterable, Optional


def _normalize_seats(seats: Iterable[int]) -> list[int]:
    return sorted(set(int(s) for s in seats))


def get_next_active_seat(
    start_seat: int,
    active_seats: Iterable[int],
    *,
    inclusive: bool = False,
) -> Optional[int]:
    """start_seat の左隣 (= seat 番号で次の active seat、9 超えで 1 へ折返し) を返す。

    inclusive=True かつ start_seat が active なら start_seat 自身を返す。
    active な席が 0 件なら None を返す。
    """
    seats = _normalize_seats(active_seats)
    if not seats:
        return None
    if inclusive and start_seat in seats:
        return start_seat
    larger = [s for s in seats if s > start_seat]
    if larger:
        return larger[0]
    return seats[0]


def advance_button(
    current_button: int,
    active_seats: Iterable[int],
) -> Optional[int]:
    """次ハンドの button 席を返す。current_button の左隣を取る。

    現実装は get_next_active_seat と同等だが、用途を明示するための名前付き API。
    """
    return get_next_active_seat(current_button, active_seats)


def compute_blinds(
    button_seat: int,
    active_seats: Iterable[int],
) -> tuple[Optional[int], Optional[int]]:
    """button 位置から (sb_seat, bb_seat) を返す。

    通常 (≥3 人): sb = button の左隣 active、 bb = sb の左隣 active
    heads-up (active 2 人): button が SB、もう一方が BB
    active が 2 未満 / button が active に含まれない: (None, None)
    """
    seats = _normalize_seats(active_seats)
    if len(seats) < 2 or button_seat not in seats:
        return (None, None)
    if len(seats) == 2:
        idx = seats.index(button_seat)
        sb = seats[idx]
        bb = seats[(idx + 1) % 2]
        return (sb, bb)
    sb = get_next_active_seat(button_seat, seats)
    if sb is None:
        return (None, None)
    bb = get_next_active_seat(sb, seats)
    return (sb, bb)


def compute_first_actor_preflop(
    button_seat: int,
    active_seats: Iterable[int],
) -> Optional[int]:
    """preflop で最初に action する seat を返す。

    通常: BB の左隣 (UTG)
    heads-up: BTN (= SB) が最初
    """
    seats = _normalize_seats(active_seats)
    if len(seats) < 2 or button_seat not in seats:
        return None
    if len(seats) == 2:
        return button_seat
    _, bb = compute_blinds(button_seat, seats)
    if bb is None:
        return None
    return get_next_active_seat(bb, seats)


def compute_first_actor_postflop(
    button_seat: int,
    active_seats: Iterable[int],
    folded_seats: Optional[Iterable[int]] = None,
    all_in_seats: Optional[Iterable[int]] = None,
) -> Optional[int]:
    """flop/turn/river で最初に action する seat を返す。

    通常: button の左隣の live seat (SB が live なら SB)
    heads-up: BB が最初
    """
    seats = _normalize_seats(active_seats)
    if not seats or button_seat not in seats:
        return None
    folded = set(folded_seats or ())
    all_in = set(all_in_seats or ())
    live = [s for s in seats if s not in folded and s not in all_in]
    if not live:
        return None
    if len(seats) == 2:
        _, bb = compute_blinds(button_seat, seats)
        if bb is not None and bb in live:
            return bb
        return live[0]
    return _next_in(button_seat, live)


def advance_actor(
    current_actor: int,
    active_seats: Iterable[int],
    folded_seats: Optional[Iterable[int]] = None,
    all_in_seats: Optional[Iterable[int]] = None,
) -> Optional[int]:
    """current_actor の左隣の live seat を返す。folded/all-in はスキップ。"""
    seats = _normalize_seats(active_seats)
    folded = set(folded_seats or ())
    all_in = set(all_in_seats or ())
    live = [s for s in seats if s not in folded and s not in all_in]
    if not live:
        return None
    if len(live) == 1:
        return live[0]
    return _next_in(current_actor, live)


def _next_in(start_seat: int, candidates: list[int]) -> Optional[int]:
    """candidates の昇順円環で start_seat の次の要素を返す (start_seat 自身は除外)。"""
    if not candidates:
        return None
    sorted_c = sorted(candidates)
    larger = [s for s in sorted_c if s > start_seat]
    if larger:
        return larger[0]
    return sorted_c[0]
