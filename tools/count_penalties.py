#!/usr/bin/env python3
"""tools/count_penalties.py — 推定器の罰の値をデータから数える（台帳 F, `docs/estimator-params/README.md`）

F1 = 真のアクションと観測の対応づけ。真の行を発話の語と札の離脱に組む。**推定器の点では選ばない**（値に依存しない
= 数えた値を今の値と比べられる）。組み方:

- 語: 窓の発話を話し始めの順に、読みの選択肢（`tools/estimate.py:utterance_options`）ごとに語の列にし、真の行と単調に
  組む（動的計画法）。真の行は「語と組む / 言われない」、語は「行と組む / 余計」。組める条件はアクションの群
  （フォールド / チェック・コール / ベット・レイズ・オールイン）が同じで、話し始めがその行のストリートの札の
  `CARD_EARLY_SEC` 秒前より後・次のストリートの札の `CARD_LATE_SEC` 秒後より前。組みの点は種類と額まで合えば 3・額か
  チェック ↔ コールだけ違えば 2（取り違え）。発話ごとに、最も多くの真の行を説明する読みを選ぶ（同点は既定の読み）。
- 札の離脱: 真のフォールドごとに、その席の離脱（推定器と同じ `blip_sec`・`flicker_sec` で数える）を組む。残りの離脱は
  降りたあと（ショーダウン・片付け）か、残っている席の離脱（持ち上げ）。

推定器からは import されない（内容の指紋に入らない）。

    TZ=Asia/Tokyo python tools/count_penalties.py align --hands 15cad555#3 --dump align.json   # 開発データ
    TZ=Asia/Tokyo python tools/count_penalties.py align pokerlogs_*.zip                         # 店舗のログ
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 組むのに使わない読みの出所: 決まり文句の下のアクション（雑談の発話に「アクションを言った」と仮に置く読み = 事前の
# 推し量りで、音の証拠ではない）。使うと「言われた」を多く数える
GUESS_SOURCES = frozenset({"canned"})
# 語の話し始めとストリートの札の時刻の許し（秒）。札が読めるのは宣言から最大 2.5 秒遅れる（engine の
# `BEFORE_CARD_FIX_MARGIN_SEC`・推定器の `before_card_slack_sec` = 3）。次の札のあとに前のストリートの言い直しが来ることもある
CARD_EARLY_SEC = 3.0
CARD_LATE_SEC = 3.0
EXACT, NEAR = 30, 20
# 同じ数の行を説明するなら、音の証拠の強い読みを選ぶ（Whisper > ライブの規則 > 短い語の分類器 > 第 2 の耳の候補など）。
# 開発データ 845da8e6#1: 雑談の読み直し（ライブの規則で 500）より、続けて言った「レイズ 500」（Whisper）
SOURCE_BONUS = {"whisper": 3, "rescue": 2, "short": 1, "short_amount": 1}
# 額・種類の違う組み（取り違え）を許す読み。第 2 の耳の候補は書き起こしの無い発話（雑音）にも付くので、違う額で組むと
# 「言われたが聞き違えた」を多く数える（開発データ 9db1e032#4: 雑音の「bet 100」と真のベット 2100）
NEAR_SOURCES = frozenset({"whisper", "rescue", "short", "short_amount"})
END_SLACK_SEC = 2.0
# フロップの 3 枚はいっしょに置く（推定器の `flop_spread_sec` = 8 秒・店舗 89 ハンドのうち 88 が 5.5 秒以内）
FLOP_SPREAD_SEC = 8.0
BETTING = ("fold", "check", "call", "bet", "raise", "allin")
_FAMILY = {"fold": "fold", "check": "passive", "call": "passive", "bet": "wager", "raise": "wager", "allin": "wager"}
_STREETS = ("preflop", "flop", "turn", "river")


def _epoch(iso: Optional[str]) -> Optional[float]:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return None


@dataclass
class Token:
    utterance: float          # 発話の話し始め
    index: int                # 発話の中の何番目の語
    action: str
    amount: int


@dataclass
class RowMatch:
    row: int
    street: str
    seat: int
    action: str
    amount: int
    word: Optional[dict] = None          # 組んだ語 {utterance, index, action, amount, source, kind}
    departure: Optional[float] = None    # フォールドの行: 組んだ札の離脱の時刻


@dataclass
class HandAlignment:
    key: str
    session_id: str
    hand_id: int
    seats: int                           # 配られた人数
    rows: list[RowMatch] = field(default_factory=list)
    extra: list[dict] = field(default_factory=list)          # 真の行に使わなかった賭けの語
    utterances: list[dict] = field(default_factory=list)     # 発話ごとに選んだ読み {start, source, default_source, keys}
    departures: list[dict] = field(default_factory=list)     # 組まなかった離脱 {seat, t, back, kind}
    street_cards: dict[str, float] = field(default_factory=dict)
    start: float = 0.0
    end: float = 0.0
    truth_button: Optional[int] = None   # 真のアクションに入れたボタン（入れていなければ None）
    base_button: Optional[int] = None    # 直しの無い再生のボタン（ライブが回したボタン）


def street_cards(hand: dict) -> dict[str, float]:
    """ストリートの札の時刻。ボードの位置ではなく読めた時刻の順で決める（位置は読み取りの都合で入れ替わる = 開発データ
    845da8e6#1: 4 枚目の札がフロップの札より先に読めた）: 最初の札から `FLOP_SPREAD_SEC` 秒以内に読めた札がフロップ、
    そのあとの札が順にターン・リバー。読めていないストリートは無い。"""
    times = sorted(t for t in (_epoch(b.get("dealt_at")) for b in hand.get("board_timeline") or []) if t is not None)
    if not times:
        return {}
    out = {"flop": times[0]}
    later = [t for t in times if t > times[0] + FLOP_SPREAD_SEC]
    for street, t in zip(("turn", "river"), later):
        out[street] = t
    return out


def in_street(street: str, t: float, cards: dict[str, float], start: float) -> bool:
    """話し始め `t` がそのストリートのアクションとして言える時刻か（札の時刻が分からないストリートは制限しない）。"""
    i = _STREETS.index(street) if street in _STREETS else 0
    lo = start - 0.5 if i == 0 else cards.get(street)
    if lo is not None and i > 0:
        lo -= CARD_EARLY_SEC
    hi = next((cards[s] for s in _STREETS[i + 1:] if s in cards), None)
    return (lo is None or t >= lo) and (hi is None or t <= hi + CARD_LATE_SEC)


def match_kind(row: dict, tok: Token) -> Optional[str]:
    """語が行を説明するか: "exact"（種類・額まで）/ "amount"（額だけ違う）/ "sub"（チェック ↔ コール）/ None。"""
    ra, ta = str(row.get("action") or ""), tok.action
    ram, tam = int(row.get("amount") or 0), int(tok.amount or 0)
    if _FAMILY.get(ra) != _FAMILY.get(ta):
        return None
    if ra in ("fold", "check", "call") or ta in ("fold", "check", "call"):
        return "exact" if ra == ta else "sub"
    # ベット ↔ レイズは同じ（額だけの語は「ベット」と読み、engine が状態でレイズにする）。オールインは額を比べない
    # （オーナーの答え (b)）。額の無い語（「レイズ」だけ）は額を言わなかっただけ
    if ram == tam or "allin" in (ra, ta) or not tam:
        return "exact"
    return "amount"


def _score(kind: Optional[str], source: str = "") -> int:
    return (EXACT if kind == "exact" else NEAR if kind else -1) + SOURCE_BONUS.get(source, 0)


def align_words(rows: list[dict], utterances: list[tuple[float, list]], cards: dict[str, float],
                start: float) -> tuple[list[Optional[dict]], list[dict], list[dict]]:
    """真の行と発話の語の単調な対応づけ。utterances = [(話し始め, [(source, [Token…]), …既定の読みが先])]。
    返り値: 行ごとの語（無ければ None）・余計な語・発話ごとに選んだ読み。"""
    n = len(rows)
    NEG = -10 ** 9
    dp = [0] * (n + 1)                        # 行を j 個まで使った最高点（言われない行は 0）
    for j in range(1, n + 1):
        dp[j] = max(dp[j], dp[j - 1])
    history = []                              # 発話ごと: (選んだ読み[j], 読みごとの歩み)
    for t, options in utterances:
        best = [NEG] * (n + 1)
        pick = [0] * (n + 1)
        steps_by_option = []
        for oi, (source, tokens) in enumerate(options):
            cur = list(dp)
            steps = []
            for tok in tokens:
                nxt = list(cur)               # 余計な語
                via = ["extra"] * (n + 1)
                if tok.action == "check_around":
                    for j in range(1, n + 1):
                        k = j
                        total = cur[j]
                        while k >= 1 and rows[k - 1].get("action") == "check" \
                                and rows[k - 1].get("street") == rows[j - 1].get("street") \
                                and in_street(rows[k - 1].get("street"), t, cards, start):
                            total = cur[k - 1] + EXACT * (j - k + 1) + SOURCE_BONUS.get(source, 0)
                            if total > nxt[j]:
                                nxt[j], via[j] = total, ("around", k - 1)
                            k -= 1
                elif tok.action in BETTING:
                    for j in range(1, n + 1):
                        row = rows[j - 1]
                        kind = match_kind(row, tok) if in_street(row.get("street"), t, cards, start) else None
                        if kind not in (None, "exact") and source not in NEAR_SOURCES:
                            kind = None   # 第 2 の耳の候補（音の無い発話にも付く）は種類・額まで合うときだけ
                        if kind and cur[j - 1] + _score(kind, source) > nxt[j]:
                            nxt[j], via[j] = cur[j - 1] + _score(kind, source), ("match", kind)
                skip = [False] * (n + 1)
                for j in range(1, n + 1):     # 言われない行
                    if nxt[j - 1] > nxt[j]:
                        nxt[j], skip[j] = nxt[j - 1], True
                steps.append((via, skip))
                cur = nxt
            steps_by_option.append(steps)
            for j in range(n + 1):
                if cur[j] > best[j]:
                    best[j], pick[j] = cur[j], oi
        history.append((pick, steps_by_option))
        dp = best
    # 戻る
    words: list[Optional[dict]] = [None] * n
    extra: list[dict] = []
    chosen: list[dict] = []
    j = n
    for (t, options), (pick, steps_by_option) in zip(reversed(utterances), reversed(history)):
        oi = pick[j]
        source, tokens = options[oi]
        chosen.append({"start": t, "source": source, "default_source": options[0][0],
                       "keys": [(tk.action, tk.amount) for tk in tokens]})
        for tok, (via, skip) in zip(reversed(tokens), reversed(steps_by_option[oi])):
            while j > 0 and skip[j]:
                j -= 1
            v = via[j]
            word = {"utterance": t, "index": tok.index, "action": tok.action, "amount": tok.amount, "source": source}
            if v == "extra":
                if tok.action in BETTING:
                    extra.append(word)
            elif v[0] == "around":
                for r in range(v[1], j):
                    words[r] = dict(word, kind="around")
                j = v[1]
            else:
                words[j - 1] = dict(word, kind=v[1])
                j -= 1
    chosen.reverse()
    extra.reverse()
    return words, extra, chosen


def tokens_of(start: float, keys: tuple) -> list[Token]:
    out = []
    for i, (action, amount, *_rest) in enumerate(keys):
        if action in BETTING or action == "check_around":
            out.append(Token(start, i, action, int(amount or 0)))
    return out


def align_hand(est, h, guesses: bool = False) -> HandAlignment:
    """1 ハンドの対応づけ（`h` = `tools/bench_hands.py:ScoredHand`）。"""
    from tools.measure_capture_accuracy import _without_showdown_rows

    w = h.window
    truth = _without_showdown_rows(h.truth)
    rows = [a for a in truth.get("actions") or [] if a.get("action") in BETTING]
    cards = street_cards(w.base)
    _events, transcript_rows = est._window_inputs(w)        # noqa: SLF001 — 推定器と同じ窓の発話
    utterances = []
    for row in sorted(transcript_rows, key=lambda r: r["utterance_start_ts"]):
        start = row["utterance_start_ts"]
        opts = [(o.source, tokens_of(start, o.keys)) for o in est._options(start)   # noqa: SLF001
                if guesses or o.source not in GUESS_SOURCES]
        if not opts:
            continue
        if not any(toks for _s, toks in opts):
            continue
        utterances.append((start, opts))
    words, extra, chosen = align_words(rows, utterances, cards, w.start)
    seats = len(w.base.get("players") or []) or len({a.get("seat") for a in rows})
    out = HandAlignment(h.key, h.session_id, h.hand_id, seats, street_cards=cards, start=w.start, end=w.end,
                        extra=extra, utterances=chosen, truth_button=h.truth.get("button_seat"),
                        base_button=w.base.get("button_seat"))
    for i, (a, word) in enumerate(zip(rows, words)):
        out.rows.append(RowMatch(i, a.get("street"), int(a.get("seat")), a.get("action"), int(a.get("amount") or 0),
                                 word))
    align_departures(est, w, out)
    return out


def align_departures(est, w, out: HandAlignment) -> None:
    """真のフォールドに、その席の離脱を組む（推定器の `_departure_terms` と同じ窓・同じ数え方）。"""
    if est.presence is None:
        return
    p = est.params
    stop = min(w.end, (_epoch(w.base.get("ended_at")) or w.end) + p["departure_after_end_sec"])
    seats = {r.seat for r in out.rows} | {pl.get("seat") for pl in w.base.get("players") or []}
    deps = [d for d in est.presence.departures(w.start, stop, p["blip_sec"], p["flicker_sec"])
            if d[0] in seats and not (d[2] is not None and d[2] - d[1] < p["flicker_sec"])]
    used: set[int] = set()
    folded_at: dict[int, float] = {}
    for r in out.rows:
        if r.action != "fold":
            continue
        at = r.word["utterance"] if r.word else None
        mine = [k for k, d in enumerate(deps) if d[0] == r.seat and k not in used]
        if not mine:
            continue
        # 戻らなかった離脱を優先（戻った離脱は持ち上げ）、声があればその時刻に近いもの
        stayed = [k for k in mine if deps[k][2] is None or deps[k][2] >= stop] or mine
        k = min(stayed, key=lambda x: abs(deps[x][1] - at) if at is not None else deps[x][1])
        used.add(k)
        r.departure = deps[k][1]
        folded_at[r.seat] = deps[k][1]
    # ベッティングの終わり = 時刻の分かる最後の行（組んだ語・組んだ離脱）。それより後（`END_SLACK_SEC` の手前から）の
    # 離脱はショーダウン・片付け（推定器の `_departure_terms` と同じ見方）
    anchors = [r.word["utterance"] for r in out.rows if r.word] + [r.departure for r in out.rows if r.departure]
    bet_end = max(anchors, default=w.start)
    for k, (seat, t, back) in enumerate(deps):
        if k in used:
            continue
        if seat in folded_at and folded_at[seat] <= t:
            kind = "after_fold"
        elif t >= bet_end - END_SLACK_SEC:
            kind = "end"
        else:
            kind = "lift"
        out.departures.append({"seat": seat, "t": t, "back": back, "kind": kind})


# ───────────────────────── セッション ─────────────────────────


def session_alignments(inp, presence, only: Optional[set[str]] = None, guesses: bool = False) -> list[HandAlignment]:
    from integration.estimator import PARAMS, SessionEstimator
    from tools.bench_hands import scored_hands

    est = SessionEstimator(inp.events, inp.transcripts, presence, inp.setup, inp.flags, inp.session_id, PARAMS)
    return [align_hand(est, h, guesses) for h in scored_hands(inp, est) if only is None or h.key in only]


def all_alignments(logs: Optional[list[Path]] = None, only: Optional[set[str]] = None,
                   guesses: bool = False) -> list[tuple[str, HandAlignment]]:
    """(データの名前, 対応づけ) の並び。logs が無ければ開発データ（物差しと同じセッション）。"""
    from tools.bench_hands import v1_inputs

    out = []
    for name, sessions in v1_inputs(store=not logs, script=False, logs=logs or None):
        for inp, presence in sessions:
            if only and inp.session_id[:8] not in {k.split("#")[0] for k in only}:
                continue
            out += [(name, a) for a in session_alignments(inp, presence, only, guesses)]
    return out


def describe(a: HandAlignment) -> list[str]:
    lines = [f"{a.key}（{a.seats} 人）"]
    for r in a.rows:
        word = (f"「{r.word['action']}{' ' + str(r.word['amount']) if r.word['amount'] else ''}」"
                f"{r.word['kind']}・{r.word['source']}・{r.word['utterance'] - a.start:+.1f}s") if r.word else "言われない"
        dep = f" ／ 離脱 {r.departure - a.start:+.1f}s" if r.departure is not None else ""
        amount = f" {r.amount}" if r.amount else ""
        lines.append(f"  {r.street} 席{r.seat} {r.action}{amount}: {word}{dep}")
    if a.extra:
        lines.append("  余計な語: " + " ／ ".join(f"{x['action']} {x['amount'] or ''}（{x['utterance'] - a.start:+.1f}s）"
                                              for x in a.extra))
    if a.departures:
        lines.append("  組まなかった離脱: " + " ／ ".join(f"席{d['seat']} {d['t'] - a.start:+.1f}s {d['kind']}"
                                                  for d in a.departures))
    return lines


# ───────────────────────── 数え上げ（F2） ─────────────────────────

DATA_NAMES = {"店舗（実卓）": "開発データ", "店舗のログ": "10/19"}


def table_band(seats: int) -> str:
    return "2〜4 人" if seats <= 4 else "5〜6 人" if seats <= 6 else "7〜8 人"


def wilson(x: int, n: int) -> tuple[float, float]:
    from tools.bench_hands import wilson as _w

    return _w(x, n) if n else (0.0, 1.0)


def _quantiles(values: list[float], qs=(0.1, 0.5, 0.9)) -> list[float]:
    v = sorted(values)
    return [v[min(len(v) - 1, int(q * len(v)))] for q in qs] if v else []


def bootstrap(values: list[float], stat, n: int = 1000, seed: int = 7) -> tuple[float, float]:
    """パーセンタイルのブートストラップの 90% 区間。"""
    import random

    if len(values) < 2:
        return (float("nan"), float("nan"))
    rng = random.Random(seed)
    stats = sorted(stat([rng.choice(values) for _ in values]) for _ in range(n))
    return stats[int(0.05 * n)], stats[int(0.95 * n)]


def _median(v: list[float]) -> float:
    s = sorted(v)
    return s[len(s) // 2] if s else float("nan")


def row_events(a: dict) -> list[dict]:
    """行ごとに数えるための印: 言われたか・直前の人と同じか・ハンドを終わらせるフォールドか・賭けに向き合っていたか。"""
    rows = a["rows"]
    seats = {r["seat"] for r in rows}
    alive = set(seats) | set(range(0))
    if a.get("seats") and len(seats) < a["seats"]:
        alive = set(seats)                        # 配られて動かなかった席（BB のウォーク）は行に無い
    out = []
    prev_by_street: dict[str, Optional[str]] = {}
    wagered: dict[str, set[int]] = {}
    for r in rows:
        street = r["street"]
        prev = prev_by_street.get(street)
        facing = street == "preflop" or bool(wagered.get(street, set()) - {r["seat"]})
        ev = {"action": r["action"], "street": street, "voiced": r["word"] is not None,
              "source": (r["word"] or {}).get("source"), "kind": (r["word"] or {}).get("kind"),
              "repeat": prev == r["action"], "facing": facing, "last_fold": False,
              "departure": r.get("departure"), "word_t": (r["word"] or {}).get("utterance")}
        if r["action"] == "fold":
            alive.discard(r["seat"])
            ev["last_fold"] = len(alive) <= 1
        if r["action"] in ("bet", "raise", "allin"):
            wagered.setdefault(street, set()).add(r["seat"])
        prev_by_street[street] = r["action"]
        out.append(ev)
    return out


def count_all(alignments: list[dict]) -> dict:
    """§3 の表の値を層（データ × 卓の人数）ごとに数える。"""
    from collections import defaultdict

    layers: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for a in alignments:
        data = DATA_NAMES.get(a.get("data"), a.get("data") or "?")
        layers[(data, table_band(a["seats"]))].append(a)
        layers[(data, "全部")].append(a)
    out = {}
    for key, hands in sorted(layers.items()):
        res: dict[str, dict] = {}

        def prop(name: str, x: int, n: int, now: Optional[float] = None) -> None:
            lo, hi = wilson(x, n)
            res[name] = {"x": x, "n": n, "p": (x / n if n else None), "lo": lo, "hi": hi, "now": now}

        evs = [e for a in hands for e in row_events(a)]
        from integration.estimator import PARAMS as P

        def missed(sel) -> tuple[int, int]:
            chosen = [e for e in evs if sel(e)]
            return sum(1 for e in chosen if not e["voiced"]), len(chosen)

        prop("p_miss_check", *missed(lambda e: e["action"] == "check" and not e["repeat"]), P["p_miss_check"])
        prop("p_miss_call", *missed(lambda e: e["action"] == "call" and not e["repeat"]), P["p_miss_call"])
        prop("p_miss_repeat", *missed(lambda e: e["action"] in ("check", "call") and e["repeat"]), P["p_miss_repeat"])
        prop("p_miss_fold", *missed(lambda e: e["action"] == "fold" and not e["last_fold"]), P["p_miss_fold"])
        prop("p_miss_last_fold", *missed(lambda e: e["action"] == "fold" and e["last_fold"]), P["p_miss_last_fold"])
        prop("p_miss_wager", *missed(lambda e: e["action"] in ("bet", "raise", "allin")), P["p_miss_wager"])
        # 取り違え: 組んだ語の種類（チェック ↔ コール）・額の違い
        passive = [e for e in evs if e["voiced"] and e["action"] in ("check", "call")]
        prop("p_sub", sum(1 for e in passive if e["kind"] == "sub"), len(passive), P["p_sub"])
        wager = [e for e in evs if e["voiced"] and e["action"] in ("bet", "raise")]
        prop("p_amount", sum(1 for e in wager if e["kind"] == "amount"), len(wager), P["p_amount"])
        # 余計な語（真の行に使わなかった語 ÷ その種類の語）
        for fam, acts in (("賭け", ("bet", "raise", "allin")), ("フォールド", ("fold",)), ("チェック・コール", ("check", "call"))):
            extra = sum(1 for a in hands for x in a["extra"] if x["action"] in acts)
            used = sum(1 for e in evs if e["voiced"] and e["action"] in acts)
            prop(f"余計な語（{fam}）", extra, extra + used, P["p_phantom"] if fam == "賭け" else None)
        # ベットの無いところのフォールド（プリフロップは数えない = BB のオプションが分からない）
        open_rows = [e for e in evs if e["street"] != "preflop" and not e["facing"]]
        prop("p_open_fold", sum(1 for e in open_rows if e["action"] == "fold"), len(open_rows), P["p_open_fold"])
        # 札の離脱
        folds = [e for e in evs if e["action"] == "fold"]
        prop("p_nodepart", sum(1 for e in folds if e["departure"] is None), len(folds), P["p_nodepart"])
        lags = [e["departure"] - e["word_t"] for e in folds if e["departure"] is not None and e["word_t"] is not None]
        res["fold_lag"] = {"n": len(lags), "median": _median(lags), "q": _quantiles(lags),
                           "median_ci": bootstrap(lags, _median),
                           "mad": _median([abs(x - _median(lags)) for x in lags]) if lags else None,
                           "within_1s": (sum(1 for x in lags if abs(x) <= 1.0) / len(lags)) if lags else None,
                           "now": {"mu": P["fold_lag_mu"], "b": P["fold_lag_b"], "b_late": P["fold_lag_b_late"]}}
        lifts = sum(1 for a in hands for d in a["departures"] if d["kind"] == "lift")
        seat_sec = 0.0
        for a in hands:
            anchors = [r["word"]["utterance"] for r in a["rows"] if r["word"]] + \
                      [r["departure"] for r in a["rows"] if r.get("departure")]
            if anchors:
                seat_sec += max(0.0, max(anchors) - a["start"]) * a["seats"]
        res["lift_sec"] = {"lifts": lifts, "seat_sec": round(seat_sec), "sec_per_lift": (seat_sec / lifts) if lifts else None,
                           "now": P["lift_sec"]}
        # ボタン（真に入れたハンドだけ）
        with_button = [a for a in hands if a.get("truth_button") is not None]
        prop("p_button", sum(1 for a in with_button if a["truth_button"] != a.get("base_button")), len(with_button),
             P["p_button"])
        res["hands"] = {"n": len(hands)}
        out[f"{key[0]} / {key[1]}"] = res
    return out


def format_counts(counts: dict) -> list[str]:
    lines = []
    for layer, res in counts.items():
        lines.append(f"## {layer}（ハンド {res['hands']['n']}）")
        lines.append("")
        lines.append("| 値 | 今 | 数えた値 | 95% 区間 | x / n |")
        lines.append("|---|---|---|---|---|")
        for name, v in res.items():
            if name in ("hands", "fold_lag", "lift_sec"):
                continue
            now = "—" if v["now"] is None else f"{v['now']:g}"
            p = "—" if v["p"] is None else f"{v['p']:.3f}"
            mark = "（目安）" if v["n"] < 20 else ""
            lines.append(f"| {name} | {now} | {p}{mark} | {v['lo']:.3f}〜{v['hi']:.3f} | {v['x']} / {v['n']} |")
        lag = res["fold_lag"]
        if lag["n"]:
            q = "・".join(f"{x:+.1f}" for x in lag["q"])
            lines.append(f"| 声のフォールド → 離脱（秒） | 中心 {lag['now']['mu']}・幅 {lag['now']['b']} / {lag['now']['b_late']} | "
                         f"中央 {lag['median']:+.2f}・MAD {lag['mad']:.2f}・1 秒以内 {lag['within_1s']:.0%} | "
                         f"中央の 90% {lag['median_ci'][0]:+.2f}〜{lag['median_ci'][1]:+.2f}・10/50/90% {q} | n {lag['n']} |")
        lift = res["lift_sec"]
        per = "—" if lift["sec_per_lift"] is None else f"{lift['sec_per_lift']:.0f}"
        lines.append(f"| 持ち上げ（秒 / 1 席） | {lift['now']:g} | {per} | — | {lift['lifts']} 回 / {lift['seat_sec']} 席秒 |")
        lines.append("")
    return lines


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="推定器の罰の値をデータから数える（台帳 F）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_align = sub.add_parser("align", help="真のアクションと観測の対応づけ（F1）")
    p_align.add_argument("logs", nargs="*", type=Path, help="店舗のログ（zip かフォルダ）。省略 = 開発データ")
    p_align.add_argument("--hands", default="", help="このハンドだけ（「15cad555#3,027e4b15#1」）")
    p_align.add_argument("--dump", type=Path, help="対応づけを JSON に書く")
    p_align.add_argument("--quiet", action="store_true", help="ハンドごとの表示をしない")
    p_align.add_argument("--guesses", action="store_true", help="決まり文句の下のアクションの読みも組むのに使う")
    p_count = sub.add_parser("count", help="対応づけ（--dump の JSON）から値を数える（F2）")
    p_count.add_argument("dumps", nargs="+", type=Path, help="align --dump の JSON（開発データ・店舗のログ）")
    p_count.add_argument("--md", type=Path, help="表を Markdown に書く")
    p_count.add_argument("--json", type=Path, help="数えた値を JSON に書く")
    args = ap.parse_args(argv)
    logging.disable(logging.CRITICAL)
    if args.cmd == "count":
        alignments = [a for path in args.dumps for a in json.loads(path.read_text(encoding="utf-8"))]
        counts = count_all(alignments)
        lines = format_counts(counts)
        print("\n".join(lines))
        if args.md:
            args.md.write_text("\n".join(lines) + "\n", encoding="utf-8")
        if args.json:
            args.json.write_text(json.dumps(counts, ensure_ascii=False, indent=1), encoding="utf-8")
        return 0
    only = {k.strip() for k in args.hands.split(",") if k.strip()} or None
    results = all_alignments(args.logs or None, only, args.guesses)
    if not args.quiet:
        for _name, a in results:
            print("\n".join(describe(a)))
    rows = [r for _n, a in results for r in a.rows]
    voiced = sum(1 for r in rows if r.word)
    print(f"ハンド {len(results)}・真の行 {len(rows)}・語と組んだ {voiced}・余計な語 {sum(len(a.extra) for _n, a in results)}")
    if args.dump:
        args.dump.write_text(json.dumps([dict(asdict(a), data=n) for n, a in results], ensure_ascii=False, indent=1),
                             encoding="utf-8")
        print(f"対応づけ: {args.dump}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
