#!/usr/bin/env python3
"""tools/voice_prior_trial.py — ディーラーの声の近さを推定器の証拠に足す実験（台帳 I の I3, 2026-10-28。推定器は変えない）

I1（`tools/dealer_voice.py`）で、Whisper が読めなかったのにディーラーが言っていた発話は、ほかの読めなかった発話より
登録した声に近いと分かった（10/26 の聞き取りの印 46 発話, AUC 0.73）。そこで推定器の採点に 1 項を足して、ハンドが
全部正しくなる数が増えるかを測る。

- 声の近さ（`sims`）: 発話ごとに話者の埋め込み（`audio/dealer_voice.py`, CAM++）と、**その前後 15 分のアクションに読めた
  発話の平均**（その発話自身は除く）とのコサイン類似度。ディーラーの交代に合わせて声が替わる（10/26 の 20:45 の交代 =
  オーナー確認）。読み上げ集の登録が無いセッションでも測れる。
- 項: 仮説が発話を**アクションの宣言として使う**（その発話の語が記録の行に対応する）なら
  clip(k ×（近さ − そのセッションの近さの中央値）, ±1) を足す。使わない発話は 0（仮説どうしで差だけが効く）。
  k は事前に決めて 4（近さが中央値より 0.25 高い = log で +1）。感度として 8 も並べる。
- 推定器の値・規則は変えない（`SessionEstimator.score` を外から包む）。

使い方:

    python tools/voice_prior_trial.py sims <音声の zip…> --out sims.json          # 発話ごとの近さ（音声が要る）
    TZ=Asia/Tokyo python tools/voice_prior_trial.py run --sims sims.json --store --logs <zip…> --workers 4
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import statistics
import sys
import time
from bisect import bisect_left, bisect_right
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Optional

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

WINDOW_SEC = 900.0
AUTO_MIN_SEC = 0.8
PRIMARY_K = 4.0
VARIANTS = (0.0, 4.0, 8.0)      # 0 = いまの推定器
CAP = 1.0


# ───────────────────────── 発話ごとの近さ ─────────────────────────

def local_sims(rows: list[dict], vecs: list[np.ndarray], *, window: float = WINDOW_SEC) -> list[Optional[float]]:
    """各発話と、前後 `window` 秒のアクションに読めた発話（自分は除く）の平均の近さ。"""
    from tools.dealer_voice import ACTION, category

    anchors = [(float(r["utterance_start_ts"]), v) for r, v in zip(rows, vecs)
               if category(r) == ACTION and float(r.get("audio_sec") or 0) >= AUTO_MIN_SEC]
    anchors.sort(key=lambda x: x[0])
    times = [t for t, _v in anchors]
    cum = np.cumsum(np.array([v for _t, v in anchors]), axis=0) if anchors else None
    out: list[Optional[float]] = []
    for r, v in zip(rows, vecs):
        t = float(r["utterance_start_ts"])
        lo, hi = bisect_left(times, t - window), bisect_right(times, t + window)
        if cum is None or hi - lo < 5:
            out.append(None)
            continue
        total = cum[hi - 1] - (cum[lo - 1] if lo > 0 else 0)
        k = bisect_left(times, t)
        if k < len(times) and times[k] == t and lo <= k < hi:
            total = total - anchors[k][1]          # 自分を除く
        norm = float(np.linalg.norm(total))
        out.append(round(float(np.dot(total / norm, v)), 4) if norm > 0 else None)
    return out


def cmd_sims(args) -> int:
    from audio import dealer_voice as dv
    from tools.dealer_voice import ensure_model, embed_file
    from tools.eval_store import open_input
    from tools.rescore_audio import find_sessions, read_jsonl

    path = ensure_model(dv.DEFAULT_MODEL)
    if path is None:
        return 1
    embedder = dv.OnnxEmbedder(path)
    out: dict = json.loads(args.out.read_text(encoding="utf-8")) if args.out.exists() else {}
    root, tmp = open_input(list(args.inputs))
    try:
        for session in find_sessions(root):
            if session.session_id in out:
                continue
            rows, vecs = [], []
            for r in read_jsonl(session.transcripts):
                if not r.get("audio_file") or r.get("no_speech") or r.get("utterance_start_ts") is None:
                    continue
                p = session.audio_path(r["audio_file"])
                v = embed_file(embedder, p) if p is not None else None
                if v is not None:
                    rows.append(r)
                    vecs.append(v)
            if not rows:
                continue
            sims = local_sims(rows, vecs)
            got = {f"{float(r['utterance_start_ts']):.3f}": s for r, s in zip(rows, sims) if s is not None}
            vals = list(got.values())
            out[session.session_id] = {"median": round(statistics.median(vals), 4) if vals else None, "sims": got}
            print(f"  {session.session_id[:8]}: {len(got)} 発話・近さの中央値 {out[session.session_id]['median']}")
            args.out.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)
    return 0


# ───────────────────────── 推定器に足す ─────────────────────────

def voice_estimator(base_cls, sims: dict, k: float):
    """`SessionEstimator` の採点に声の項を足した型。"""
    from integration.estimator import align_words

    class VoiceEstimator(base_cls):
        def score(self, w, hand, records, info, edits):
            score, terms, flags = super().score(w, hand, records, info, edits)
            if hand is None or k <= 0 or not sims.get("sims"):
                return score, terms, flags
            rows, _unused = align_words(hand.get("actions") or [], info["tokens"], info["inserted"])
            used = {r.word.utterance_start_ts for r in rows if r.word is not None and r.word.utterance_start_ts}
            mid = float(sims.get("median") or 0.0)
            extra = 0.0
            for start in used:
                s = sims["sims"].get(f"{float(start):.3f}")
                if s is None:
                    continue
                v = max(-CAP, min(CAP, k * (float(s) - mid)))
                if v:
                    terms.append((f"声の近さ {s:.2f}", round(v, 3)))
                    extra += v
            return score + extra, terms, flags

    return VoiceEstimator


def _run(job) -> list[dict]:
    logging.disable(logging.CRITICAL)
    inp, presence, sims, k = job
    from integration.estimator import PARAMS, SessionEstimator
    from tools.bench_hands import scored_hands
    from tools.measure_capture_accuracy import hand_correct_ignoring_amounts, hand_fully_correct

    cls = voice_estimator(SessionEstimator, sims, k)
    est = cls(inp.events, inp.transcripts, presence, inp.setup, inp.flags, inp.session_id, PARAMS)
    out = []
    for h in scored_hands(inp, est):
        r = est.estimate_hand(h.window)
        best = r.best
        out.append({"key": h.key, "k": k, "correct": hand_fully_correct(h.truth, best.hand),
                    "flow": hand_correct_ignoring_amounts(h.truth, best.hand), "review": bool(r.reasons),
                    "voice_terms": sum(1 for lbl, _v in best.terms if lbl.startswith("声の近さ"))})
    return out


def report(results: list[dict]) -> list[str]:
    by: dict[tuple, dict] = {}
    for d in results:
        by[(d["key"], d["k"])] = d
    keys = sorted({d["key"] for d in results})
    sessions = sorted({k.split("#")[0] for k in keys})
    lines = ["セッション   ハンド  " + "  ".join(f"k={k:g}: 正しい(額を除く)" for k in VARIANTS)]
    for s in [*sessions, None]:
        ks = [k for k in keys if s is None or k.startswith(s + "#")]
        cells = []
        for k in VARIANTS:
            got = [by[(x, k)] for x in ks if (x, k) in by]
            cells.append(f"{sum(d['correct'] for d in got):>3}（{sum(d['flow'] for d in got):>3}）")
        lines.append(f"{(s or '合計'):<10} {len(ks):>5}   " + "             ".join(cells))
    for k in VARIANTS[1:]:
        fixed = [x for x in keys if (x, 0.0) in by and (x, k) in by and not by[(x, 0.0)]["correct"] and by[(x, k)]["correct"]]
        broken = [x for x in keys if (x, 0.0) in by and (x, k) in by and by[(x, 0.0)]["correct"] and not by[(x, k)]["correct"]]
        silent = [x for x in broken if not by[(x, k)]["review"]]
        mark = " ← 事前に決めた設定" if k == PRIMARY_K else ""
        lines.append(f"k={k:g}: 直った {len(fixed)}（{', '.join(fixed) or '—'}）・壊れた {len(broken)}"
                     f"（{', '.join(broken) or '—'}）・うち要確認なし {len(silent)}{mark}")
    return lines


def cmd_run(args) -> int:
    from tools.bench_hands import v1_inputs

    sims_all = json.loads(args.sims.read_text(encoding="utf-8"))
    pairs = []
    if args.store:
        for _n, sessions in v1_inputs(store=True, script=False):
            pairs.extend(sessions)
    if args.logs:
        for _n, sessions in v1_inputs(store=False, script=False, logs=list(args.logs)):
            pairs.extend(sessions)
    seen, picked = set(), []
    for inp, presence in pairs:
        if inp.session_id in sims_all and inp.session_id not in seen:
            seen.add(inp.session_id)
            picked.append((inp, presence))
    print(f"声の近さのあるセッション {len(picked)}: {', '.join(i.session_id[:8] for i, _p in picked)}")
    results: list[dict] = []
    done: set[tuple] = set()
    if args.resume and args.resume.exists():
        for line in args.resume.read_text(encoding="utf-8").splitlines():
            if line.strip():
                d = json.loads(line)
                results.append(d)
                done.add((d["key"].split("#")[0], d["k"]))
    jobs = [(inp, presence, sims_all[inp.session_id], k) for inp, presence in picked for k in VARIANTS
            if (inp.session_id[:8], k) not in done]

    def finished(out: list[dict]) -> None:
        results.extend(out)
        if args.resume:
            with args.resume.open("a", encoding="utf-8") as f:
                for d in out:
                    f.write(json.dumps(d, ensure_ascii=False) + "\n")

    started = time.time()
    if args.workers <= 1:
        for job in jobs:
            finished(_run(job))
    else:
        import multiprocessing
        from concurrent.futures import as_completed

        with ProcessPoolExecutor(max_workers=args.workers, mp_context=multiprocessing.get_context("spawn")) as pool:
            for fut in as_completed([pool.submit(_run, job) for job in jobs]):
                finished(fut.result())
    print(f"（{time.time() - started:.0f} 秒）")
    if args.dump:
        args.dump.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    for line in report(results):
        print(line)
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="ディーラーの声の近さを推定器の証拠に足す実験（台帳 I3）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sims")
    s.add_argument("inputs", nargs="+", type=Path)
    s.add_argument("--out", type=Path, required=True)
    r = sub.add_parser("run")
    r.add_argument("--sims", type=Path, required=True)
    r.add_argument("--store", action="store_true", help="開発データ（声の近さのあるセッションだけ）")
    r.add_argument("--logs", nargs="*", type=Path, help="店舗のログ（text の zip と真のアクションの zip）")
    r.add_argument("--workers", type=int, default=4)
    r.add_argument("--dump", type=Path)
    r.add_argument("--resume", type=Path, help="セッション × k ごとの結果を足す JSONL。あれば済んだ分を飛ばす")
    args = ap.parse_args(argv)
    logging.disable(logging.CRITICAL)
    return cmd_sims(args) if args.cmd == "sims" else cmd_run(args)


if __name__ == "__main__":
    sys.exit(main())
