#!/usr/bin/env python3
"""tools/param_flips.py — 値を差し替えたとき、どのハンドの順位がひっくり返るかの試算（台帳 F の F3a）

物差しで値を 1 つずつ測る（`tools/sweep_estimator.py`、1 組 数十分）前に、試す値を絞るための安い試算。今の値で 1 回だけ探し、
残した記録の直しの列を、差し替えた値で採点し直す（探し直さない）:

- 誤ったハンド: 真に寄せた探索（`tools/oracle_estimator.py:guided_search`）で真に届いた直しの列と、推定器の候補（1 番から
  `CANDIDATES` 個）。差し替えた値で真が候補のどれよりも高くなれば「直る見込み」。
- 正しいハンド: 推定器の候補。差し替えた値で真でない候補が真（1 番）を抜けば「壊れる見込み」。

探し直さないので、差し替えた値で新しく見つかる候補は見ない（物差しで確かめる = F3b）。表せないハンド（真に届かない）は
数えない。推定器からは import しない（推定器の内容の指紋に入らない）。

    TZ=Asia/Tokyo python tools/param_flips.py --workers 4 --dump flips_dev.json
    TZ=Asia/Tokyo python tools/param_flips.py <10/19 のログ> --workers 4 --dump flips_1019.json
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CANDIDATES = 6        # 推定器の候補の上から何個を採点し直すか
HANDS_PER_JOB = 4     # 1 つの仕事に入れるハンドの数（長いセッションが 1 つのプロセスに偏らないように）

# 差し替える値の組（名前, 推定器の値, 発話の読みの値）。数えた値は `docs/estimator-params/measured.md`
# （開発データ = 2〜4 人・プレイヤーなし / 10/19 = 8 人卓まで・プレイヤーあり）。離脱の幅はラプラスの b = MAD / ln 2、
# 遅い側の幅は今の比（0.55 / 0.4）のまま。読みの値は出所ごとの当たりの割合と今の見込みの比（log）から
SETS: list[tuple[str, dict, dict]] = [
    ("p_miss_fold 0.10（開発）", {"p_miss_fold": 0.10}, {}),
    ("p_miss_fold 0.15（10/19）", {"p_miss_fold": 0.15}, {}),
    ("p_miss_last_fold 0.07（開発）", {"p_miss_last_fold": 0.07}, {}),
    ("p_miss_last_fold 0.17（10/19）", {"p_miss_last_fold": 0.17}, {}),
    ("p_miss_repeat 0.007（開発）", {"p_miss_repeat": 0.007}, {}),
    ("p_miss_repeat 0.125（10/19）", {"p_miss_repeat": 0.125}, {}),
    ("p_miss_wager 0.02（開発）", {"p_miss_wager": 0.02}, {}),
    ("p_miss_wager 0.067（10/19）", {"p_miss_wager": 0.067}, {}),
    ("p_miss_check・call 0.23・0.34（10/19）", {"p_miss_check": 0.23, "p_miss_call": 0.34}, {}),
    ("p_phantom 0.08（開発）", {"p_phantom": 0.08}, {}),
    ("p_phantom 0.30（10/19）", {"p_phantom": 0.30}, {}),
    ("p_nodepart 0.003（開発）", {"p_nodepart": 0.003}, {}),
    ("p_nodepart 0.025（10/19）", {"p_nodepart": 0.025}, {}),
    ("lift_sec 3405（開発）", {"lift_sec": 3405.0}, {}),
    ("lift_sec 481（10/19）", {"lift_sec": 481.0}, {}),
    ("離脱の幅 0.30・0.41（開発）", {"fold_lag_b": 0.30, "fold_lag_b_late": 0.41}, {}),
    ("離脱の幅 1.27・1.75（10/19）", {"fold_lag_b": 1.27, "fold_lag_b_late": 1.75}, {}),
    ("p_sub 0.002（開発）", {"p_sub": 0.002}, {}),
    ("p_sub 0.013（10/19）", {"p_sub": 0.013}, {}),
    ("p_amount 0.003（開発）", {"p_amount": 0.003}, {}),
    ("p_amount 0.066（10/19）", {"p_amount": 0.066}, {}),
    ("短い語 short_none_penalty −0.3", {}, {"short_none_penalty": -0.3}),
    ("短い語 short_cap 2.5", {}, {"short_cap": 2.5}),
    ("短い語 −0.3 + cap 2.5", {}, {"short_none_penalty": -0.3, "short_cap": 2.5}),
    ("耳の候補 ear_base −2.9（開発）", {}, {"ear_base": -2.9}),
    ("耳の候補 ear_base −2.7（10/19）", {}, {"ear_base": -2.7}),
    ("耳の額 ear_amount_top 0", {}, {"ear_amount_top": 0}),
]


@contextmanager
def reading_overrides(values: dict):
    """発話の読みの値を道具の中だけで置き換える（推定器は `reading_params()` を読むたびに `READING_OVERRIDES` を見る）。"""
    import integration.estimator as E

    saved = dict(E.READING_OVERRIDES)
    E.READING_OVERRIDES.update(values)
    try:
        yield
    finally:
        E.READING_OVERRIDES.clear()
        E.READING_OVERRIDES.update(saved)


def _estimator(inp, presence, params: dict):
    from integration.estimator import SessionEstimator

    return SessionEstimator(inp.events, inp.transcripts, presence, inp.setup, inp.flags, inp.session_id, params)


def rebind(est, edits: tuple) -> Optional[tuple]:
    """読みの直し（`Edit.logp` に読みの確からしさを持つ）を、差し替えた値で作り直した選択肢の確からしさに付け替える。
    同じ文の読みが選択肢から消えたら None（その記録は作れない）。"""
    from dataclasses import replace

    out = []
    for e in edits:
        if e.kind == "read":
            opt = next((o for o in est._options(e.at) if o.text == e.value), None)   # noqa: SLF001
            if opt is None:
                return None
            e = replace(e, logp=opt.logp)
        out.append(e)
    return tuple(out)


def flip_hand(est, h, sets: list[tuple[str, dict, dict]], make) -> Optional[dict]:
    """1 ハンド: 今の値で記録を残し、組ごとに採点し直す。表せないハンドは None。"""
    from tools.measure_capture_accuracy import hand_fully_correct
    from tools.oracle_estimator import guided_search

    w = h.window
    result = est.estimate_hand(w)
    correct = hand_fully_correct(h.truth, result.best.hand)
    pool = list(result.candidates[:CANDIDATES])
    if correct:
        truth_edits = result.best.edits
    else:
        found, _closest, _n = guided_search(est, w, h.truth)
        if found is None:
            return None
        truth_edits = found.edits
    others = [c.edits for c in pool if not hand_fully_correct(h.truth, c.hand)]
    if not others:
        return None
    out = {"key": h.key, "correct": correct, "sets": {}}

    def score(e2, edits) -> float:
        edits = rebind(e2, edits)
        return float("-inf") if edits is None else e2.evaluate_edits(w, edits).score

    def margin(e2) -> float:
        best_other = max(score(e2, o) for o in others)
        t = score(e2, truth_edits)
        if best_other == float("-inf"):
            return 99.0
        return -99.0 if t == float("-inf") else round(t - best_other, 3)

    out["now"] = margin(est)
    for name, params, reading in sets:
        with reading_overrides(reading):
            out["sets"][name] = margin(make(params))
    return out


def _run_job(job) -> list[dict]:
    from integration.estimator import PARAMS
    from tools.bench_hands import scored_hands

    logging.disable(logging.CRITICAL)
    inp, presence, keys, sets = job
    est = _estimator(inp, presence, PARAMS)

    def make(params: dict):
        # 読みの値を変えた組は選択肢の作り直しが要る（推定器が選択肢を覚えている）ので毎回作る
        return _estimator(inp, presence, dict(PARAMS, **params))

    out = []
    for h in scored_hands(inp, est):
        if h.key in keys:
            r = flip_hand(est, h, sets, make)
            if r is not None:
                out.append(r)
    return out


def summarize(results: list[dict], sets: list[tuple[str, dict, dict]]) -> list[str]:
    wrong = [r for r in results if not r["correct"]]
    right = [r for r in results if r["correct"]]
    lines = [f"採点し直したハンド: 誤り（真に届いた）{len(wrong)}・正しい {len(right)}",
             f"今の値で: 真が勝つ誤り {sum(1 for r in wrong if r['now'] > 0)}（探しきれない）・"
             f"真が負ける正しいハンド {sum(1 for r in right if r['now'] <= 0)}", "",
             "| 値の組 | 直る見込み | 壊れる見込み | 差 | 直る | 壊れる |", "|---|---|---|---|---|---|"]
    for name, _p, _r in sets:
        fixed = [r["key"] for r in wrong if r["now"] <= 0 < r["sets"][name]]
        broken = [r["key"] for r in right if r["now"] > 0 >= r["sets"][name]]
        lines.append(f"| {name} | {len(fixed)} | {len(broken)} | {len(fixed) - len(broken):+d} | "
                     f"{' '.join(fixed) or '—'} | {' '.join(broken) or '—'} |")
    return lines


def main(argv: Optional[list[str]] = None) -> int:
    from integration.estimator import PARAMS, SessionEstimator, content_hash, params_hash
    from tools.bench_hands import default_workers, scored_hands, v1_inputs

    ap = argparse.ArgumentParser(description="値を差し替えたときの順位のひっくり返りの試算（台帳 F の F3a）")
    ap.add_argument("logs", nargs="*", type=Path, help="店舗のログ（zip かフォルダ）。省略 = 開発データ")
    ap.add_argument("--hands", default="", help="このハンドだけ（「15cad555#3,027e4b15#1」）")
    ap.add_argument("--workers", type=int, default=default_workers())
    ap.add_argument("--dump", type=Path, help="ハンドごとの差（真 − 候補の最高）を JSON に書く")
    ap.add_argument("--sets", type=Path, help="値の組を JSON で（[[名前, 推定器の値, 発話の読みの値], …]）。省略 = `SETS`")
    args = ap.parse_args(argv)
    sets = [tuple(x) for x in json.loads(args.sets.read_text(encoding="utf-8"))] if args.sets else SETS
    logging.disable(logging.CRITICAL)
    only = {k.strip() for k in args.hands.split(",") if k.strip()} or None
    jobs = []
    for _name, sessions in v1_inputs(store=not args.logs, script=False, logs=args.logs or None):
        for inp, presence in sessions:
            est = SessionEstimator(inp.events, inp.transcripts, presence, inp.setup, inp.flags, inp.session_id, PARAMS)
            keys = [h.key for h in scored_hands(inp, est) if only is None or h.key in only]
            for i in range(0, len(keys), HANDS_PER_JOB):
                jobs.append((inp, presence, set(keys[i:i + HANDS_PER_JOB]), sets))
    started = time.time()
    results: list[dict] = []
    if args.workers <= 1 or len(jobs) < 2:
        for job in jobs:
            results += _run_job(job)
    else:
        import multiprocessing

        with ProcessPoolExecutor(max_workers=min(args.workers, len(jobs)),
                                 mp_context=multiprocessing.get_context("spawn")) as pool:
            for out in pool.map(_run_job, jobs):
                results += out
    print(f"推定器 v1: 値の指紋 {params_hash(PARAMS)}・内容の指紋 {content_hash()}（{time.time() - started:.0f} 秒）")
    print("\n".join(summarize(results, sets)))
    if args.dump:
        args.dump.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
