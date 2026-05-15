"""integration/action_order.py

ディーラーボタン位置を起点とした SB / BB / first actor / 次手番の
算出ヘルパ。pure functions のみを置き、状態は持たない。

active_seats は座席番号 (1..9) の集合。順序は seat 番号を昇順に並べた
円環として扱う (1→2→...→9→1)。これは物理的卓配置の clockwise に
相当する (右隣 = 次の番号)。

Note:
    ・スタック 0 や着席していない seat は呼び出し側で active_seats から
      除外しておくこと。
    ・post-flop の actor 算出時は folded / all-in 済みも除外したものを
      live_seats として渡す。
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
    """start_seat の左隣 (= seat 番号で次に大きい active seat、9 を超えたら 1 へ折返す) を返す。

    inclusive=True の場合は start_seat 自身が active なら start_seat を返す。
    active な席が 0 件なら None を返す。
    """
    seats = _normalize_seats(active_seats)
    if not seats:
        return None
    if inclusive and start_seat in seats:
        return start_seat
    n = len(seats)
    # start_seat が active_seats に含まれていなくても、円環上で「次の seat 番号」を探す
    # 例: start_seat=8, active=[1,2,3] → 1 を返す
    for offset in range(1, n + 1):
        # start_seat より大きい最小の seat、または折返しで最小の seat
        candidates = [s for s in seats if s > start_seat]
        if candidates:
            return candidates[0]
        return seats[0]
    return None


def compute_blinds(
    button_seat: int,
    active_seats: Iterable[int],
) -> tuple[Optional[int], Optional[int]]:
    """button 位置から (sb_seat, bb_seat) を返す。

    通常: sb = button の左隣 active、 bb = sb の左隣 active
    heads-up (active 2 人): button が SB、もう一方が BB
    active が 2 未満: (None, None)
    """
    seats = _normalize_seats(active_seats)
    if len(seats) < 2 or button_seat not in seats:
        return (None, None)
    if len(seats) == 2:
        # heads-up: BTN == SB
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
        return button_seat  # heads-up: BTN/SB が最初
    sb, bb = compute_blinds(button_seat, seats)
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
        # heads-up: BB が最初
        _, bb = compute_blinds(button_seat, seats)
        if bb is not None and bb in live:
            return bb
        # BB が all-in/folded で除外された場合は唯一の live を返す
        return live[0]
    # button の左隣から最初の live を探す
    return _next_in(button_seat, live)


def advance_button(
    current_button: int,
    active_seats: Iterable[int],
) -> Optional[int]:
    """次ハンドの button 席を返す。current_button の左隣 (= 次に大きい active seat、
    末尾なら先頭へ wrap) を取る。

    advance_actor の特殊形 (fold/all-in 概念なし) として薄くラップした
    名前付き API。
    """
    return advance_actor(current_button, active_seats)


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
