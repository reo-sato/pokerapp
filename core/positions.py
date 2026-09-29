"""core/positions.py

ディーラーボタンと**ポジション名**（BTN / SB / BB / UTG …）の純粋ロジック（ISSUE-0032 / 仕様 FR-05b〜h）。

なぜ要るか: 仕様 FR-26 はアクター推定の **最優先の証拠**を「ターン順（`current_turn_seat`）」と
規定し、§7 はディーラーに「BTN、コール」のような**ポジション名の言及**を推奨している。
ボタンが回らないとターン順そのものが毎ハンド同じになり、2 人超の卓では (n-1)/n のハンドで
prior が誤る。また `position_map` が無いとポジション名を席に解決できない。

本モジュールは I/O もゲーム状態も持たない（座席リストとボタン席だけを受け取って並びと名前を返す）。
"""
from __future__ import annotations

import re
from typing import Optional

# SB/BB と BTN の**間**に入るポジション名（席数 - 3 個）。最後は常に CO（BTN の直前）。
# 一般的な呼び方に合わせた固定表（9 max まで）。
_MIDDLE_POSITIONS: dict[int, list[str]] = {
    0: [],
    1: ["UTG"],
    2: ["UTG", "CO"],
    3: ["UTG", "HJ", "CO"],
    4: ["UTG", "MP", "HJ", "CO"],
    5: ["UTG", "UTG+1", "MP", "HJ", "CO"],
    6: ["UTG", "UTG+1", "UTG+2", "MP", "HJ", "CO"],
}

# 発話で使われうる表記 → 正準ポジション名。キーはすべて **小文字**（照合前に lower する）。
# `parse_position` / `seat_for_position` が参照する。
POSITION_ALIASES: dict[str, str] = {
    "btn": "BTN", "button": "BTN", "ボタン": "BTN",
    "sb": "SB", "smallblind": "SB", "スモールブラインド": "SB", "スモール": "SB",
    "bb": "BB", "bigblind": "BB", "ビッグブラインド": "BB", "ビッグ": "BB",
    "utg": "UTG", "アンダーザガン": "UTG",
    "utg+1": "UTG+1", "utg1": "UTG+1",
    "utg+2": "UTG+2", "utg2": "UTG+2",
    "mp": "MP", "ミドルポジション": "MP",
    "hj": "HJ", "hijack": "HJ", "ハイジャック": "HJ",
    "co": "CO", "cutoff": "CO", "カットオフ": "CO",
}


def _build_alias_pattern() -> re.Pattern[str]:
    """`POSITION_ALIASES` を最長一致優先の 1 本の正規表現にする。

    英字の別名だけ前後を英数字で挟まれない条件にする（"co" が "code" に、"bb" が "abbr" に
    当たるような偽陽性を防ぐ）。カタカナ別名は語中に紛れないので境界条件を付けない。
    """
    parts: list[str] = []
    for alias in sorted(POSITION_ALIASES, key=len, reverse=True):
        esc = re.escape(alias)
        if alias.isascii():
            esc = rf"(?<![0-9a-z]){esc}(?![0-9a-z])"
        parts.append(esc)
    return re.compile("|".join(parts), re.IGNORECASE)


_ALIAS_PATTERN = _build_alias_pattern()


def parse_position(text: str) -> Optional[str]:
    """発話テキストから **ポジション名の言及**を 1 つ取り出す（仕様 §7 / FR-26）。

    ディーラーは「BTN、コール」「ビッグブラインド チェック」のように席番号ではなく
    ポジション名で読み上げることがある。これを actor 推定の証拠として使うため、正準名
    （BTN/SB/BB/UTG/UTG+1/UTG+2/MP/HJ/CO）に正規化して返す。見つからなければ None。

    最左の言及を採り、同位置なら最長別名を優先する（"utg+1" が "utg" に負けない）。
    席への解決は `seat_for_position` / `position_map`（= ボタンを知っている側）が行う。
    """
    m = _ALIAS_PATTERN.search(text)
    if not m:
        return None
    return POSITION_ALIASES[m.group(0).lower()]


def seat_order_from_button(seats: list[int], button_seat: int) -> list[int]:
    """ボタンの**次の席から**一周した並びを返す（= index 0 が SB, 末尾が BTN）。

    pokerkit の `create_state` はブラインドを index 0/1 に置き、ボタンは最終 index になる。
    したがって「ボタンの次の席を先頭にした並び」をそのまま渡せばボタン位置が反映される。
    `button_seat` が `seats` に無ければ末尾の席をボタンとみなす（= 並びは `seats` のまま）。
    """
    ordered = sorted(seats)
    if not ordered:
        return []
    if button_seat not in ordered:
        button_seat = ordered[-1]
    cut = ordered.index(button_seat) + 1
    return ordered[cut:] + ordered[:cut]


def next_button(seats: list[int], button_seat: Optional[int]) -> int:
    """次のハンドのボタン席（1 つ進める）。

    `button_seat` が None（初回）なら **最大の席番号**を返す。こうすると初回の並びが
    `sorted(seats)` と一致し、ボタン導入前の挙動（および golden fixtures）が保たれる。
    """
    ordered = sorted(seats)
    if not ordered:
        raise ValueError("seats must not be empty")
    if button_seat is None or button_seat not in ordered:
        return ordered[-1]
    return ordered[(ordered.index(button_seat) + 1) % len(ordered)]


def position_names(num_seats: int) -> list[str]:
    """SB 起点のポジション名（`seat_order_from_button` と同じ並び）。

    - 2 人（heads-up）は `["BB", "BTN"]`。ヘッズアップは **ボタンが SB を出す**規則で、
      pokerkit も index 0 に BB・index 1（= 末尾 = ボタン）に SB を post する。実測で確認済み。
    - 3 人以上は `["SB", "BB", …中間…, "BTN"]`。中間は `_MIDDLE_POSITIONS`。
    - 10 人以上は表に無いので中間を `MP` で埋める（運用上は 9 max）。
    """
    if num_seats <= 0:
        return []
    if num_seats == 1:
        return ["BTN"]
    if num_seats == 2:
        return ["BB", "BTN"]
    middle_count = num_seats - 3
    middle = _MIDDLE_POSITIONS.get(middle_count)
    if middle is None:
        middle = ["UTG"] + ["MP"] * (middle_count - 2) + ["CO"]
    return ["SB", "BB"] + middle + ["BTN"]


def position_map(seats: list[int], button_seat: int) -> dict[int, str]:
    """seat → ポジション名（仕様 §6.1 の `position_map`）。"""
    order = seat_order_from_button(seats, button_seat)
    return dict(zip(order, position_names(len(order))))


def seat_for_position(seats: list[int], button_seat: int, position: str) -> Optional[int]:
    """ポジション名 → seat（無ければ None）。発話「BTN、コール」の解決に使う。"""
    canonical = POSITION_ALIASES.get(position.strip().lower(), position.strip().upper())
    for seat, name in position_map(seats, button_seat).items():
        if name == canonical:
            return seat
    return None


# ――― 配った順から見るボタン（ディーラーがボタンを動かし忘れたときの救済, 店舗 2026-09-29 9d1d8536 ハンド 4）―――
# ディーラーはボタンの次の席から 1 枚ずつ 2 周配る（heads-up はボタンでない方から）。手札を最初に読んだ時刻の
# 並びが、どの席をボタンとした配る順に合うかを見る。

# 読んだ時刻の差がこれより小さい 2 枚は、どちらが先か分からないとみて比べない（全リーダーを 1 周読むのに
# 約 0.3 秒かかる = ISSUE-0021 の実測。同じ周に読んだ 2 枚は置いた順と逆に記録されうる）。
DEAL_ORDER_TIE_SEC = 0.3


def deal_order_fit(
    seats: list[int], times: dict[int, list[float]], button_seat: int, tie_sec: float = DEAL_ORDER_TIE_SEC,
) -> tuple[int, int]:
    """手札を読んだ時刻の並びが、ボタン `button_seat` での配る順とどれだけ合うか。

    Args:
        seats: ハンドに配られた席。
        times: 席 → その席の手札を最初に読んだ時刻（1〜2 枚。読めなかった札は無くてよい）。
        button_seat: 仮のボタン。

    Returns:
        (配る順と合った組の数, 比べた組の数)。比べるのは違う席の札の組で、時刻の差が `tie_sec` 以上のもの
        （比べる組はボタンによらない）。同じ席の 2 枚はどのボタンでも同じなので数えない。
    """
    order = seat_order_from_button(seats, button_seat)
    rank = {seat: i for i, seat in enumerate(order)}
    cards = [
        ((k, rank[seat]), seat, t)
        for seat in order for k, t in enumerate(sorted(times.get(seat, ()))[:2])
    ]
    agree = total = 0
    for i, (dealt_a, seat_a, t_a) in enumerate(cards):
        for dealt_b, seat_b, t_b in cards[i + 1:]:
            if seat_a == seat_b or abs(t_a - t_b) < tie_sec:
                continue
            total += 1
            if (dealt_a < dealt_b) == (t_a < t_b):
                agree += 1
    return agree, total


def button_from_deal(
    seats: list[int], times: dict[int, list[float]], current: int, tie_sec: float = DEAL_ORDER_TIE_SEC,
) -> Optional[int]:
    """配った順から見たボタンの席（いまのボタン `current` と違うときだけ。それ以外は None）。

    読み取りの時刻はプレイヤーの手の動き（札を置き直す・持ち上げる）にも左右されるので、次がすべてそろった
    ときだけ判断する（オーナー 2026-09-29: 1 人で 3 人を演じたテストの読み取りの時刻は当てにならない）:

    - 比べられる組が `2 × 席数` 以上ある（証拠が足りなければ判断しない）。
    - 配る順に 1 か所の食い違いもなく合うボタンが 1 つだけ。
    - いまのボタンと食い違う組が 2 つ以上（1 枚の読み遅れで変えない）。
    """
    seats = sorted(seats)
    if len(seats) < 2 or current not in seats:
        return None
    fits = {b: deal_order_fit(seats, times, b, tie_sec) for b in seats}
    total = fits[current][1]
    perfect = [b for b, (agree, n) in fits.items() if n and agree == n]
    if len(perfect) != 1 or perfect[0] == current or total < 2 * len(seats):
        return None
    if total - fits[current][0] < 2:
        return None
    return perfect[0]
