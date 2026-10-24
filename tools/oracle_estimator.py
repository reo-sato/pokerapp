#!/usr/bin/env python3
"""tools/oracle_estimator.py — 推定器 v1 のオラクルの採点（台帳 B の ①, 2026-10-24）

物差し（`tools/bench_hands.py`）は誤りを「正解が候補に無い / 点で負けた」としか分けない。前者には (i) いまの直しの
種類では真を表せない・(ii) 表せるが推定器が探しきれない・(iii) 表せるが点が低くてビームから落ちた、が混ざっている
（改善案 `docs/worklog/2026-10-23-estimator-improvement-plan.md` §4.1-2）。

誤ったハンドごとに、推定器と同じ直しの候補（`SessionEstimator.candidate_edits` = いまの直しの語彙）で**真に近い順**に
探し（真に寄せた探索）、真に届いた候補（オラクル）と推定器の 1 番を比べる:

- (iii) 点で負けた: オラクルの点 ≤ 1 番の点。どの項の群で負けたかを出す
- (ii) 探しきれない: オラクルの点 > 1 番の点（推定器の探索の問題）
- (i) 表せない: いまの直しでは届かない。いちばん真に近い候補と真の**最初に違う行**を出す（足りない直しの手がかり）

推定器の値・規則は変えない（測るだけ。推定器からは import されない = 内容の指紋に入らない）。

使い方:

    TZ=Asia/Tokyo python tools/oracle_estimator.py --workers 4          # 開発データ（物差しと同じハンド）
    TZ=Asia/Tokyo python tools/oracle_estimator.py pokerlogs_*.zip      # 店舗のログ（10/19 など）
    TZ=Asia/Tokyo python tools/oracle_estimator.py --hands 027e4b15#1 --dump oracle.json
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 真に寄せた探索の広さ（推定器は幅 4・深さ 3）。真は直しを多く要ることがあるので深く
DEFAULT_BEAM = 8
DEFAULT_DEPTH = 6
DEFAULT_EXPAND = 24          # 1 番以外の候補で試す直しの数（1 手で真に近づいた順）
EXPLAIN_STEPS = 8            # 真に届いたあと、記録を変えずに点を上げる直しを足す回数の上限

# 項の群（`integration/estimator.py:score` のラベル。上から順に当てる）
TERM_GROUPS: list[tuple[str, str]] = [
    ("読み", r"^(読み: |既定の読み)"),
    ("捨てた語", r"^捨てた語"),
    ("余計な語", r"^余計な語"),
    ("言われない", r"^言われない"),
    ("言われた", r"^言われた"),
    ("取り違え", r"^(語の取り違え|額の聞き違い)"),
    ("手の事前", r"^(手の事前|賭けの大きさ|ベットの無いところのフォールド)"),
    ("持ち上げ", r"の離脱（残っているのに）"),
    ("札の離脱", r"(離脱|札が席に載っていた|フォールドの札が離れた)"),
    ("ボタン", r"^ボタン$"),
    ("札の時刻", r"(札より前|次の札よりあと|札が読めていない|全員降りたあとに札が配られた)"),
    ("宣言", r"^(人数の宣言|ショーダウンの声|門)"),
    ("終わり方", r"^(ラウンドが閉じないまま終わった|勝者が決まらない)"),
]
OTHER = "その他"


def term_group(label: str) -> str:
    for name, pattern in TERM_GROUPS:
        if re.search(pattern, label):
            return name
    return OTHER


def group_terms(terms: list[tuple[str, float]]) -> dict[str, float]:
    out: dict[str, float] = {}
    for label, value in terms:
        g = term_group(label)
        out[g] = out.get(g, 0.0) + float(value)
    return out


# ───────────────────────── 真との近さ ─────────────────────────


def _strip(hand: Optional[dict]) -> dict:
    from tools.measure_capture_accuracy import _without_showdown_rows

    return _without_showdown_rows(hand or {})


def closeness(truth: dict, hand: Optional[dict]) -> int:
    """真との近さ（大きいほど近い）: 正しい行 − 正しくない行・余計な行 − 勝者の不一致。全部正しいハンドが最大。"""
    from tools.measure_capture_accuracy import measure_hand

    if not hand:
        return -10_000
    t = _strip(truth)
    m = measure_hand(t, _strip(hand))
    missing = len(t.get("actions") or []) - m.action_correct
    wrong = m.action_total - m.action_correct
    return m.action_correct - max(wrong, missing) - (1 if m.winner_match is False else 0)


def first_difference(truth: dict, hand: Optional[dict]) -> str:
    """真と記録の最初に違う行（ショーダウンのマックの行は見ない）。勝者・ボードだけ違えばそれを書く。"""
    from tools.measure_capture_accuracy import _board_matches, board_entered, row_correct

    t = _strip(truth).get("actions") or []
    h = _strip(hand).get("actions") or []

    def row(a: Optional[dict]) -> str:
        if not a:
            return "（無い）"
        amount = a.get("amount") or 0
        return f"{a.get('street')} 席{a.get('seat')} {a.get('action')}" + (f" {amount}" if amount else "")

    for i in range(max(len(t), len(h))):
        ta = t[i] if i < len(t) else None
        ha = h[i] if i < len(h) else None
        if ta is None or ha is None or not row_correct(ta, ha):
            return f"{i + 1} 行目: 真 {row(ta)} / 得た {row(ha)}"
    if truth.get("winner_seat") is not None and truth.get("winner_seat") != (hand or {}).get("winner_seat"):
        return f"勝者: 真 席{truth.get('winner_seat')} / 得た 席{(hand or {}).get('winner_seat')}"
    if board_entered(truth) and not _board_matches(truth.get("board"), (hand or {}).get("board")):
        return f"ボード: 真 {truth.get('board')} / 得た {(hand or {}).get('board')}"
    return ""


# ───────────────────────── 真に寄せた探索 ─────────────────────────


@dataclass
class OracleHand:
    key: str
    category: str                    # correct / outscored / unsearched / unreachable / no_hand
    best_score: float
    best_edits: list[str]
    oracle_score: Optional[float] = None
    oracle_edits: list[str] = field(default_factory=list)
    gap: Optional[float] = None      # オラクルの点 − 1 番の点（負 = 点で負けた）
    group_diff: dict[str, float] = field(default_factory=dict)   # 群ごとのオラクル − 1 番
    in_candidates: bool = False      # 物差しの「正解が候補に」
    closest_edits: list[str] = field(default_factory=list)
    difference: str = ""             # 表せないとき: いちばん近い候補と真の最初に違う行
    # 真のベット / レイズのうち、その額がハンドの窓のどの発話のどの読み（第 2 の耳の額の候補も）にも無いもの
    # （= 入力に情報が無い。直しの種類を増やしても取り戻せない）
    unheard_wagers: list[str] = field(default_factory=list)
    replays: int = 0
    seconds: float = 0.0

    def lost_groups(self, n: int = 2) -> list[tuple[str, float]]:
        return sorted(((g, d) for g, d in self.group_diff.items() if d < -1e-9), key=lambda x: x[1])[:n]


def guided_search(est, w, truth: dict, *, beam: int = DEFAULT_BEAM, depth: int = DEFAULT_DEPTH,
                  expand: int = DEFAULT_EXPAND) -> tuple[Optional[object], object, int]:
    """推定器と同じ直しの候補で、真に近い順（同じ近さは点の順）に探す。(真に届いた中で点の良い候補 or None,
    いちばん真に近い候補, 再生の回数)。"""
    from integration.estimator import _ordered
    from tools.measure_capture_accuracy import hand_fully_correct

    seen: dict[frozenset, object] = {}

    def evaluate(edits: tuple) -> object:
        k = frozenset(edits)
        if k not in seen:
            seen[k] = est.evaluate_edits(w, _ordered(edits))
        return seen[k]

    def rank(c) -> tuple:
        return (closeness(truth, c.hand), c.score)

    base = evaluate(())
    _hand, _records, info = est.replay(w, ())
    pool = est.candidate_edits(w, info)
    found: Optional[object] = None

    def note(c) -> None:
        nonlocal found
        if c.hand and hand_fully_correct(truth, c.hand) and (found is None or c.score > found.score):
            found = c

    note(base)
    frontier = [base]
    closest = base
    useful: list = list(pool)
    for level in range(depth):
        children = []
        for r, c in enumerate(frontier):
            for e in (pool if r == 0 else useful):
                if e in c.edits or any(e.conflicts(x) for x in c.edits):
                    continue
                child = evaluate((*c.edits, e))
                if not child.hand:
                    continue
                note(child)
                children.append(child)
        if not children:
            break
        if level == 0:
            # 1 手で真に近づいた直しの順（同じなら点の順）に、1 番以外の候補で試す直しを絞る
            ranked = sorted(children, key=rank, reverse=True)
            useful = []
            for c in ranked:
                e = next(x for x in c.edits if x not in base.edits)
                if e not in useful:
                    useful.append(e)
                if len(useful) >= expand:
                    break
        by_record: dict[tuple, object] = {}
        for c in [*frontier, *children]:
            k = c.key()
            if k not in by_record or c.score > by_record[k].score:
                by_record[k] = c
        frontier = sorted(by_record.values(), key=rank, reverse=True)[:beam]
        if rank(frontier[0]) > rank(closest):
            closest = frontier[0]
        if found is not None and closeness(truth, frontier[0].hand) <= closeness(truth, found.hand):
            break                             # 真に届いた（これ以上近い記録は無い）
    if found is not None:
        found = _better_explanation(found, pool, evaluate)
    return found, closest, len(seen)


def _better_explanation(found, pool, evaluate):
    """記録を変えずに点を上げる直し（説明の直し）を、良くなる限り足す（推定器の `keep()` と同じ考え）。"""
    key = found.key()
    for _ in range(EXPLAIN_STEPS):
        best = found
        for e in pool:
            if e in found.edits or any(e.conflicts(x) for x in found.edits):
                continue
            c = evaluate((*found.edits, e))
            if c.hand and c.key() == key and c.score > best.score:
                best = c
        if best is found:
            break
        found = best
    return found


def unheard_wagers(est, w, truth: dict) -> list[str]:
    """真のベット / レイズで、その額を賭けとして読める発話が窓に 1 つも無いもの（どの読みの選択肢・音で読んだ額の候補にも
    無い）。どのストリートの発話かは見ない（= 少なめに数える）。"""
    from integration.world_replay import read_utterance

    _events, rows = est._window_inputs(w)        # noqa: SLF001 — 推定器と同じ窓の発話
    heard: set[int] = set()
    for row in rows:
        start = row.get("utterance_start_ts")
        texts = [None] + [o.text for o in est._options(start)] if start is not None else [None]   # noqa: SLF001
        for text in texts:
            # 額の点数（`amount_scores`）は決まった額の並びをすべて採点したものなので数えない（どの額も「ある」になる）
            for e in read_utterance(row, text):
                if e.action in ("bet", "raise", "allin"):
                    heard.add(int(e.amount or 0))
                    heard.update(int(a) for a in e.amount_options or ())
    out = []
    for a in _strip(truth).get("actions") or []:
        amount = int(a.get("amount") or 0)
        if a.get("action") in ("bet", "raise") and amount and amount not in heard:
            out.append(f"{a.get('street')} 席{a.get('seat')} {a.get('action')} {amount}")
    return out


def oracle_hand(est, h, **search) -> OracleHand:
    """1 ハンド: 推定器の 1 番と、真に寄せた探索の結果を比べて分ける。"""
    from integration.estimator import hand_from_key
    from tools.measure_capture_accuracy import hand_fully_correct

    started = time.time()
    result = est.estimate_hand(h.window)
    best = result.best
    keys = getattr(result, "record_keys", None) or [c.key() for c in result.candidates]
    out = OracleHand(h.key, "correct", round(best.score, 3), [e.label for e in best.edits],
                     in_candidates=any(hand_fully_correct(h.truth, hand_from_key(k)) for k in keys),
                     replays=result.replays)
    if hand_fully_correct(h.truth, best.hand):
        out.seconds = round(time.time() - started, 1)
        return out
    found, closest, replays = guided_search(est, h.window, h.truth, **search)
    out.replays += replays
    if found is None:
        out.category = "unreachable"
        out.closest_edits = [e.label for e in closest.edits]
        out.difference = first_difference(h.truth, closest.hand)
        out.unheard_wagers = unheard_wagers(est, h.window, h.truth)
    else:
        out.oracle_score = round(found.score, 3)
        out.oracle_edits = [e.label for e in found.edits]
        out.gap = round(found.score - best.score, 3)
        out.category = "outscored" if found.score <= best.score else "unsearched"
        mine, theirs = group_terms(found.terms), group_terms(best.terms)
        out.group_diff = {g: round(mine.get(g, 0.0) - theirs.get(g, 0.0), 3)
                          for g in sorted(set(mine) | set(theirs))
                          if abs(mine.get(g, 0.0) - theirs.get(g, 0.0)) > 1e-9}
    out.seconds = round(time.time() - started, 1)
    return out


# ───────────────────────── セッション ─────────────────────────


def run_session(inp, presence, only: Optional[set[str]] = None, **search) -> list[OracleHand]:
    from integration.estimator import PARAMS, SessionEstimator
    from tools.bench_hands import scored_hands

    est = SessionEstimator(inp.events, inp.transcripts, presence, inp.setup, inp.flags, inp.session_id, PARAMS)
    return [oracle_hand(est, h, **search) for h in scored_hands(inp, est) if only is None or h.key in only]


def _run_job(job) -> list[dict]:
    logging.disable(logging.CRITICAL)        # 組み直しの失敗の記録（ライブと同じ）は出さない
    inp, presence, only, search = job
    return [asdict(r) for r in run_session(inp, presence, only, **search)]


SHORT_NAMES = {"outscored": "(iii) 点で負けた", "unsearched": "(ii) 探しきれない", "unreachable": "(i) 表せない"}
CATEGORY_NAMES = {
    "correct": "正しい",
    "outscored": "(iii) 点で負けた（真に届くが点が低い）",
    "unsearched": "(ii) 探しきれない（真の点のほうが高い）",
    "unreachable": "(i) 表せない（いまの直しでは真に届かない）",
}


def summarize(results: list[OracleHand]) -> list[str]:
    from tools.bench_hands import wilson

    n = len(results)
    lines = [f"ハンド {n}"]
    for cat, name in CATEGORY_NAMES.items():
        hit = [r for r in results if r.category == cat]
        lo, hi = wilson(len(hit), n) if n else (0.0, 0.0)
        lines.append(f"  {name}: {len(hit)}（{lo:.0%}〜{hi:.0%}）")
    lost = [r for r in results if r.category == "outscored"]
    if lost:
        totals: dict[str, float] = {}
        counts: dict[str, list[str]] = {}
        for r in lost:
            for g, d in r.group_diff.items():
                totals[g] = totals.get(g, 0.0) + d
            for g, _d in r.lost_groups(1):
                counts.setdefault(g, []).append(r.key)
        lines.append("  点で負けた群（オラクル − 1 番 の合計。負 = その群で負けた）: "
                     + " ／ ".join(f"{g} {v:+.1f}" for g, v in sorted(totals.items(), key=lambda x: x[1])))
        lines.append("  いちばん負けた群ごとのハンド: "
                     + " ／ ".join(f"{g} {len(ks)}（{' '.join(ks)}）"
                                   for g, ks in sorted(counts.items(), key=lambda x: -len(x[1]))))
    unr = [r for r in results if r.category == "unreachable"]
    if unr:
        missing = [r.key for r in unr if r.unheard_wagers]
        lines.append(f"  表せないハンド {len(unr)}: うち真のベット / レイズの額がどの発話にも無い {len(missing)}"
                     f"（入力に情報が無い: {' '.join(missing)}）")
    in_cand = [r for r in results if r.category != "correct" and r.in_candidates]
    not_in = [r for r in results if r.category != "correct" and not r.in_candidates]
    lines.append(f"  物差しの分け方との対応: 候補にあった {len(in_cand)}（"
                 + "・".join(f"{SHORT_NAMES[c]} {sum(1 for r in in_cand if r.category == c)}"
                             for c in ("outscored", "unsearched", "unreachable"))
                 + f"）／ 候補に無かった {len(not_in)}（"
                 + "・".join(f"{SHORT_NAMES[c]} {sum(1 for r in not_in if r.category == c)}"
                             for c in ("outscored", "unsearched", "unreachable")) + "）")
    return lines


def hand_line(r: OracleHand) -> str:
    if r.category in ("outscored", "unsearched"):
        lost = "・".join(f"{g} {d:+.1f}" for g, d in r.lost_groups())
        return f"{r.key}: {SHORT_NAMES[r.category]} 差 {r.gap:+.1f}" + (f"（負けた群 {lost}）" if lost else "")
    unheard = f"（額が発話に無い: {'・'.join(r.unheard_wagers)}）" if r.unheard_wagers else ""
    return f"{r.key}: {SHORT_NAMES[r.category]} — {r.difference}{unheard}"


def main(argv: Optional[list[str]] = None) -> int:
    from integration.estimator import content_hash, params_hash
    from integration.estimator import PARAMS
    from tools.bench_hands import default_workers, v1_inputs

    ap = argparse.ArgumentParser(description="推定器 v1 のオラクルの採点（誤りを 表せない / 探しきれない / 点で負けた に分ける）")
    ap.add_argument("logs", nargs="*", type=Path, help="店舗のログ（pack_logs の zip かフォルダ）。省略 = 開発データ")
    ap.add_argument("--hands", default="", help="このハンドだけ（「15cad555#3,027e4b15#1」）")
    ap.add_argument("--beam", type=int, default=DEFAULT_BEAM)
    ap.add_argument("--depth", type=int, default=DEFAULT_DEPTH)
    ap.add_argument("--expand", type=int, default=DEFAULT_EXPAND)
    ap.add_argument("--workers", type=int, default=default_workers())
    ap.add_argument("--dump", type=Path, help="ハンドごとの詳細（直し・群ごとの差）を JSON に書く")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    logging.disable(logging.CRITICAL)

    only = {k.strip() for k in args.hands.split(",") if k.strip()} or None
    search = {"beam": args.beam, "depth": args.depth, "expand": args.expand}
    pairs = []
    for _name, sessions in v1_inputs(store=not args.logs, script=False, logs=args.logs or None):
        pairs.extend(sessions)
    if only:
        wanted = {k.split("#")[0] for k in only}
        pairs = [(i, p) for i, p in pairs if i.session_id[:8] in wanted]
    started = time.time()
    jobs = [(inp, presence, only, search) for inp, presence in pairs]
    results: list[OracleHand] = []
    if args.workers <= 1 or len(jobs) < 2:
        for job in jobs:
            results += [OracleHand(**d) for d in _run_job(job)]
    else:
        import multiprocessing

        with ProcessPoolExecutor(max_workers=min(args.workers, len(jobs)),
                                 mp_context=multiprocessing.get_context("spawn")) as pool:
            for out in pool.map(_run_job, jobs):
                results += [OracleHand(**d) for d in out]
    print(f"推定器 v1: 値の指紋 {params_hash(PARAMS)}・内容の指紋 {content_hash()}"
          f" ／ 真に寄せた探索 幅 {args.beam}・深さ {args.depth}・広げる直し {args.expand}"
          f"（{time.time() - started:.0f} 秒）")
    for line in summarize(results):
        print(line)
    for r in results:
        if r.category != "correct":
            print("    " + hand_line(r))
    if args.dump:
        args.dump.write_text(json.dumps([asdict(r) for r in results], ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"詳細: {args.dump}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
