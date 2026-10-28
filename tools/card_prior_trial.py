#!/usr/bin/env python3
"""tools/card_prior_trial.py — 手札の後押しの実験（台帳 B, 2026-10-28。オフライン・推定器は変えない）

オーナー（10/28）:「ハンドを見て取りそうなアクションを予想する」→ 記録を手札で**選ぶ**のは危ない（ブラフ・スロープレイが
「ありそうなプレー」に書き換わり、しかも要確認にならない）。そこで**音で決めきれないときだけの弱い後押し**として、
推定器の候補の点に手札の項を外から足して並べ直したら、何ハンド直り・何ハンド壊れるかを数える。

手札の項（推定器・開発データの値は使わない = 方策を開発データで数えない、台帳 B の決定 1）:

- 手札とその時点のボードから、でたらめな相手 1 人に対する勝率 e（モンテカルロ、乱数は手札とボードで固定）。
- 種類の起こりやすさ（一般的な傾向を手で置いた形。弱くするため 25% を一様に混ぜる）:
  - ベットに向き合う: フォールド ∝ (1−e)²・コール ∝ 2e(1−e)+0.15・レイズ ∝ e²
  - 向き合わない: チェック ∝ (1−e)+0.2・ベット ∝ e²+0.1
- 1 アクションの項 = log P(種類 | e) − log P(種類)（e を 0〜1 で平均した P。種類そのものの多さは推定器の側にあるので、
  手札が持つ分だけを足す）。1 アクション ±1 で切り、1 ハンドの合計を ±上限で切る。
- 足すのは 1 番から `窓` 以内の候補だけ（音で決めきれないとき）。手札の読めない席・ショーダウンの行は 0。

採点: 開発データ（物差しと同じハンド）で、手札の項を足す前と後の 1 番が全部正しいか。誤ったハンドはオラクル（真に寄せた探索,
`tools/oracle_estimator.py`）で真の候補を探し、「点で負けた」ハンドで真の候補が手札の項で 1 番を抜くかも数える。

使い方:

    TZ=Asia/Tokyo python tools/card_prior_trial.py --workers 4 --dump card_prior.json
    TZ=Asia/Tokyo python tools/card_prior_trial.py --load card_prior.json      # 数え直しだけ（上限・窓を変えて）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import random
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SAMPLES = 200
MIX = 0.25
ACTION_CLIP = 1.0
# 事前に決めた主な設定（これで「直った・壊れた」を数える）。ほかは感度として並べるだけ
PRIMARY = (1.0, 2.0)          # (1 ハンドの上限, 窓)
VARIANTS = [(0.5, 2.0), (1.0, 2.0), (2.0, 2.0), (1.0, 4.0), (2.0, 99.0)]
KEEP = 30                     # 1 ハンドで残す候補の数

RANKS = "23456789TJQKA"
SUITS = "cdhs"
DECK = [r + s for r in RANKS for s in SUITS]
STREET_BOARD = {"preflop": 0, "flop": 3, "turn": 4, "river": 5}


# ───────────────────────── 勝率 ─────────────────────────

_EQUITY: dict[tuple, float] = {}


def _known(cards) -> bool:
    return bool(cards) and len(cards) == 2 and all(isinstance(c, str) and len(c) == 2 and c in DECK for c in cards)


def equity(hole: tuple[str, str], board: tuple[str, ...]) -> float:
    """でたらめな相手 1 人に対する勝率（引き分けは半分）。"""
    key = (tuple(sorted(hole)), tuple(sorted(board)))
    if key in _EQUITY:
        return _EQUITY[key]
    from pokerkit import StandardHighHand

    rng = random.Random(int(hashlib.sha1(repr(key).encode()).hexdigest()[:12], 16))
    rest = [c for c in DECK if c not in hole and c not in board]
    win = 0.0
    for _ in range(SAMPLES):
        draw = rng.sample(rest, 2 + 5 - len(board))
        opp, full = draw[:2], list(board) + draw[2:]
        mine = StandardHighHand.from_game("".join(hole), "".join(full))
        theirs = StandardHighHand.from_game("".join(opp), "".join(full))
        win += 1.0 if mine > theirs else 0.5 if mine == theirs else 0.0
    _EQUITY[key] = win / SAMPLES
    return _EQUITY[key]


# ───────────────────────── 種類の起こりやすさ ─────────────────────────

def _kinds(facing: bool, e: float) -> dict[str, float]:
    if facing:
        w = {"fold": (1 - e) ** 2, "call": 2 * e * (1 - e) + 0.15, "raise": e ** 2}
    else:
        w = {"check": (1 - e) + 0.2, "bet": e ** 2 + 0.1}
    total = sum(w.values())
    n = len(w)
    return {k: (1 - MIX) * v / total + MIX / n for k, v in w.items()}


_GRID = [i / 40 + 1 / 80 for i in range(40)]
_MARGINAL = {f: {k: sum(_kinds(f, e)[k] for e in _GRID) / len(_GRID) for k in _kinds(f, 0.5)} for f in (True, False)}


def action_term(kind: str, facing: bool, e: float) -> float:
    p = _kinds(facing, e)
    if kind not in p:
        return 0.0
    v = math.log(p[kind]) - math.log(_MARGINAL[facing][kind])
    return max(-ACTION_CLIP, min(ACTION_CLIP, v))


def card_terms(hand: Optional[dict]) -> list[tuple[str, float]]:
    """候補の記録の各アクションの手札の項（ラベル, 値）。"""
    if not hand:
        return []
    holes = {int(p["seat"]): tuple(p["hole_cards"]) for p in hand.get("players") or []
             if _known(p.get("hole_cards"))}
    board = [c for c in hand.get("board") or [] if isinstance(c, str) and c in DECK]
    blinds = hand.get("blinds") or {}
    positions = {str(k): v for k, v in (hand.get("position_map") or {}).items()}
    out = []
    street = None
    top, put = 0, {}
    for a in hand.get("actions") or []:
        s = a.get("street")
        if s not in STREET_BOARD:
            continue
        if s != street:
            street, put = s, {}
            top = 0
            if s == "preflop":
                for seat, pos in positions.items():
                    if pos == "SB" or (pos == "BTN" and len(positions) == 2):     # ヘッズアップはボタンが SB
                        put[int(seat)] = int(blinds.get("sb") or 0)
                    elif pos == "BB":
                        put[int(seat)] = int(blinds.get("bb") or 0)
                top = int(blinds.get("bb") or 0)
        seat, kind, amount = int(a.get("seat") or 0), a.get("action"), int(a.get("amount") or 0)
        facing = top > put.get(seat, 0)
        if kind == "allin":
            kind = "raise" if facing and amount > top else "call" if facing else "bet"
        if kind == "bet" and facing:
            kind = "raise"
        if kind == "raise" and not facing:
            kind = "bet"
        n = STREET_BOARD[s]
        if seat in holes and len(board) >= n:
            e = equity(holes[seat], tuple(board[:n]))
            v = action_term(kind, facing, e)
            if v:
                out.append((f"{s} 席{seat} {kind}（勝率 {e:.2f}）", round(v, 3)))
        if kind in ("bet", "raise"):
            top = max(top, amount)
            put[seat] = top
        elif kind == "call":
            put[seat] = top
    return out


def hand_term(hand: Optional[dict], cap: float) -> float:
    total = sum(v for _l, v in card_terms(hand))
    return max(-cap, min(cap, total))


# ───────────────────────── 1 セッション ─────────────────────────

def _run(job) -> list[dict]:
    logging.disable(logging.CRITICAL)
    inp, presence = job
    from integration.estimator import PARAMS, SessionEstimator
    from tools.bench_hands import scored_hands
    from tools.measure_capture_accuracy import hand_fully_correct
    from tools.oracle_estimator import guided_search

    est = SessionEstimator(inp.events, inp.transcripts, presence, inp.setup, inp.flags, inp.session_id, PARAMS)
    out = []
    for h in scored_hands(inp, est):
        r = est.estimate_hand(h.window)

        def row(c) -> dict:
            return {"score": round(c.score, 3), "correct": hand_fully_correct(h.truth, c.hand),
                    "terms": card_terms(c.hand), "actions": _rows(c.hand)}

        d = {"key": h.key, "candidates": [row(c) for c in r.candidates[:KEEP]], "truth": _rows(h.truth),
             "oracle": None, "category": "correct"}
        if not d["candidates"][0]["correct"]:
            found, _closest, _n = guided_search(est, h.window, h.truth)
            if found is None:
                d["category"] = "unreachable"
            else:
                d["oracle"] = row(found)
                d["category"] = "outscored" if found.score <= r.best.score else "unsearched"
        out.append(d)
    return out


def _rows(hand: Optional[dict]) -> list[str]:
    return [f"{a.get('street')} 席{a.get('seat')} {a.get('action')} {int(a.get('amount') or 0) or ''}".rstrip()
            for a in (hand or {}).get("actions") or [] if a.get("street") != "showdown"]


# ───────────────────────── 数える ─────────────────────────

def _capped(c: dict, cap: float) -> float:
    return max(-cap, min(cap, sum(v for _l, v in c["terms"])))


def rerank(d: dict, cap: float, window: float) -> dict:
    """手札の項を足したあとの 1 番（窓の中の候補だけに足す）。真の候補（オラクル）も候補に入れたときの勝ち負けも。"""
    cands = d["candidates"]
    top = cands[0]["score"]

    def adj(c: dict) -> float:
        return c["score"] + (_capped(c, cap) if c["score"] >= top - window else 0.0)

    best = max(cands, key=adj)
    o = d.get("oracle")
    oracle_wins = None
    if o is not None and d["category"] == "outscored":
        oracle_wins = adj(o) > max(adj(c) for c in cands if not c["correct"])
    return {"correct": best["correct"], "best": best, "oracle_wins": oracle_wins}


def report(data: list[dict]) -> list[str]:
    lines = []
    n = len(data)
    before = sum(d["candidates"][0]["correct"] for d in data)
    cats: dict[str, int] = {}
    for d in data:
        cats[d["category"]] = cats.get(d["category"], 0) + 1
    lines.append(f"開発データ {n} ハンド: 全部正しい {before}・点で負けた {cats.get('outscored', 0)}・"
                 f"探しきれない {cats.get('unsearched', 0)}・表せない {cats.get('unreachable', 0)}")
    lines.append("")
    lines.append("上限  窓    全部正しい  直った  壊れた  点で負けたで真が抜く")
    for cap, window in VARIANTS:
        fixed = broken = wins = 0
        for d in data:
            was = d["candidates"][0]["correct"]
            r = rerank(d, cap, window)
            fixed += (not was) and r["correct"]
            broken += was and not r["correct"]
            wins += bool(r["oracle_wins"])
        mark = " ← 事前に決めた設定" if (cap, window) == PRIMARY else ""
        lines.append(f"{cap:>4.1f}  {window:>4.1f}  {before + fixed - broken:>4}/{n}     {fixed:>3}     {broken:>3}"
                     f"     {wins:>3}/{cats.get('outscored', 0)}{mark}")
    cap, window = PRIMARY
    for title, want in (("直ったハンド", "fixed"), ("壊れたハンド", "broken")):
        rows = []
        for d in data:
            was = d["candidates"][0]["correct"]
            r = rerank(d, cap, window)
            if (want == "fixed" and not was and r["correct"]) or (want == "broken" and was and not r["correct"]):
                rows.append((d, r))
        lines.append("")
        lines.append(f"■ {title}（上限 {cap}・窓 {window}）: {len(rows)}")
        for d, r in rows:
            lines.append(f"  {d['key']}")
            old, new = d["candidates"][0], r["best"]
            diff_old = [x for x in old["actions"] if x not in new["actions"]]
            diff_new = [x for x in new["actions"] if x not in old["actions"]]
            lines.append(f"    前の 1 番だけ: {', '.join(diff_old) or '—'}（手札の項 {_capped(old, cap):+.2f}・点 {old['score']}）")
            lines.append(f"    後の 1 番だけ: {', '.join(diff_new) or '—'}（手札の項 {_capped(new, cap):+.2f}・点 {new['score']}）")
            for label, v in new["terms"]:
                if any(label.startswith(x.rsplit(' ', 1)[0]) for x in diff_new):
                    lines.append(f"      {label} {v:+.2f}")
    lines.append("")
    lines.append(f"■ 点で負けたハンドの真の候補（上限 {cap}・窓 {window}）")
    for d in data:
        if d["category"] != "outscored":
            continue
        o, b = d["oracle"], d["candidates"][0]
        r = rerank(d, cap, window)
        lines.append(f"  {d['key']}: 点差 {o['score'] - b['score']:+.2f}・手札の項 真 {_capped(o, cap):+.2f} / 1 番 "
                     f"{_capped(b, cap):+.2f} → {'真が抜く' if r['oracle_wins'] else '抜かない'}")
    return lines


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="手札の後押しの実験（台帳 B）")
    ap.add_argument("logs", nargs="*", type=Path, help="店舗のログ（省略 = 開発データ）")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--dump", type=Path)
    ap.add_argument("--load", type=Path)
    args = ap.parse_args(argv)
    logging.disable(logging.CRITICAL)
    if args.load:
        data = json.loads(args.load.read_text(encoding="utf-8"))
    else:
        from tools.bench_hands import v1_inputs

        pairs = []
        for _name, sessions in v1_inputs(store=not args.logs, script=False, logs=args.logs or None):
            pairs.extend(sessions)
        started = time.time()
        data = []
        if args.workers <= 1:
            for job in pairs:
                data.extend(_run(job))
        else:
            import multiprocessing

            with ProcessPoolExecutor(max_workers=args.workers, mp_context=multiprocessing.get_context("spawn")) as pool:
                for out in pool.map(_run, pairs):
                    data.extend(out)
        print(f"（{time.time() - started:.0f} 秒）")
        if args.dump:
            args.dump.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    for line in report(data):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
