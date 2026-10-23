#!/usr/bin/env python3
"""tools/bench_llm.py — LLM の提案を足した推定器の物差し（オフラインの実験）

`tools/llm_candidates.py` がキャッシュした LLM の答えを読み、同じハンドを

1. **今の推定器**（直しの組を持ち込まない）
2. **LLM の提案つき**（解釈ごとの直しの組 = `HandWindow.seeds` を候補に足す）

で推定し、ハンドごとに比べる。数え方は `tools/bench_hands.py` と同じ（`V1Result`: 全部正しい・正解が候補に入った・
要確認・要確認なしの誤り・悪くなった）。加えて **LLM 単独**（解釈の `actions` をそのまま記録とみなす）も数える。

API は呼ばない（キャッシュだけ）。答えの無いハンドは提案なし（= 今の推定器と同じ）で数え、件数を出す。

使い方:

    TZ=Asia/Tokyo python tools/bench_llm.py --split A
    TZ=Asia/Tokyo python tools/bench_llm.py --split B --model claude-haiku-5-5 --json out.json
    TZ=Asia/Tokyo python tools/bench_llm.py <店舗の zip ...> --model claude-opus-5-5
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import sys
import time
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.bench_hands import _NO_LIVE, V1Result, default_workers, format_v1, wilson  # noqa: E402
from tools.llm_candidates import (  # noqa: E402
    DEFAULT_CACHE,
    DEFAULT_MODEL,
    DEFAULT_READ_LOGP,
    cache_path,
    direct_hands,
    interpretation_edits,
    load_cached,
    make_estimator,
    scored_hands,
    seeds_for,
    select_sessions,
)
from tools.llm_hand_prompt import hand_input  # noqa: E402


@dataclasses.dataclass
class LlmResult:
    """LLM の答えそのものの数え方（推定器を通す前）。"""
    hands: int = 0
    answered: int = 0             # 答えがあった（拒否・失敗・未実行を除く）
    seeded: int = 0               # 直しの組が 1 つ以上できた（今の読みと違う解釈があった）
    pick_top: int = 0             # LLM の 1 番目の解釈を再生した記録（合法性は engine）が全部正しい
    direct_top: int = 0           # LLM 単独: 1 番目の解釈が全部正しい
    direct_any: int = 0           # LLM 単独: どれかの解釈が全部正しい
    missing: list[str] = dataclasses.field(default_factory=list)
    direct_correct: list[str] = dataclasses.field(default_factory=list)
    pick_correct: list[str] = dataclasses.field(default_factory=list)


def run(pairs, *, model: str, cache_dir: Path, with_seat: bool, read_logp: float, seed_search: int,
        workers: int, baseline: bool = True) -> tuple[Optional[V1Result], V1Result, LlmResult]:
    from integration.estimator import PARAMS, _ordered
    from tools.measure_capture_accuracy import hand_fully_correct

    base_res = V1Result("今の推定器") if baseline else None
    llm_res = V1Result("LLM の提案つき")
    raw = LlmResult()
    params = dict(PARAMS, seed_search=seed_search) if seed_search else PARAMS
    for inp, presence in pairs:
        est = make_estimator(inp, presence, params)
        hands = scored_hands(inp, est)
        if not hands:
            continue
        seeded_windows = []
        for h in hands:
            raw.hands += 1
            record = load_cached(cache_path(cache_dir, model, h.session_id, h.window.hand_id), hand_input(est, h.window))
            answer = (record or {}).get("answer")
            if answer is None:
                raw.missing.append(h.key)
            else:
                raw.answered += 1
                direct = [hand_fully_correct(h.truth, d) for d in direct_hands(answer, h.window)]
                raw.direct_top += bool(direct and direct[0])
                raw.direct_any += any(direct)
                if any(direct):
                    raw.direct_correct.append(h.key)
                # LLM が選ぶ: 1 番目の解釈を今の再生にかけた記録（選ぶのは LLM、合法性だけ engine）
                first = (interpretation_edits(answer, est, h.window, with_seat=with_seat, read_logp=read_logp)
                         or [()])[0]
                picked = est.evaluate_edits(h.window, _ordered(first))
                if hand_fully_correct(h.truth, picked.hand or None):
                    raw.pick_top += 1
                    raw.pick_correct.append(h.key)
            seeds = seeds_for(answer, est, h.window, with_seat=with_seat, read_logp=read_logp)
            raw.seeded += bool(seeds)
            seeded_windows.append(dataclasses.replace(h.window, seeds=seeds))
        live = [h.live if h.has_live else _NO_LIVE for h in hands]
        started = time.time()
        if base_res is not None:
            for h, lv, r in zip(hands, live, est.estimate(windows=[h.window for h in hands], workers=workers)):
                base_res.add_hand(h.key, h.truth, r, live=lv)
            base_res.seconds += time.time() - started
            started = time.time()
        for h, lv, r in zip(hands, live, est.estimate(windows=seeded_windows, workers=workers)):
            llm_res.add_hand(h.key, h.truth, r, live=lv)
        llm_res.seconds += time.time() - started
    return base_res, llm_res, raw


def compare(base: V1Result, llm: V1Result) -> dict[str, list[str]]:
    """ハンドごとの差（今の推定器 → 提案つき）。"""
    b, a = set(base.correct), set(llm.correct)
    flagged = {k for k, v in llm.review_of.items() if v}
    worse = sorted(b - a)
    return {
        "良くなった": sorted(a - b),
        "悪くなった": worse,
        "悪くなったのに要確認なし": [k for k in worse if k not in flagged],
        "正解が候補に入った（新しく）": sorted(set(base.not_found) - set(llm.not_found)),
    }


def format_raw(raw: LlmResult) -> list[str]:
    lo, hi = wilson(raw.direct_top, raw.hands)
    return [
        f"LLM の答え: {raw.answered}/{raw.hands} ハンド（無し {len(raw.missing)}）・今の読みと違う解釈 {raw.seeded}",
        f"LLM が選ぶ（1 番目の解釈を再生 = 合法性だけ engine）: {raw.pick_top}/{raw.hands}",
        f"LLM 単独（解釈の actions をそのまま記録とみなす）: 1 番目 {raw.direct_top}/{raw.hands}"
        f"（95% 区間 {lo:.0%}〜{hi:.0%}）・どれか {raw.direct_any}/{raw.hands}",
    ]


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="LLM の提案を足した推定器の物差し（キャッシュだけを読む）")
    ap.add_argument("logs", nargs="*", type=Path, help="店舗のログ（zip / フォルダ）。無ければ開発データ")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--split", choices=["A", "B", "a", "b"])
    ap.add_argument("--sessions", nargs="*")
    ap.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    ap.add_argument("--no-seat", action="store_true", help="LLM の言った席を読みの文に入れない")
    ap.add_argument("--read-logp", type=float, default=DEFAULT_READ_LOGP)
    ap.add_argument("--seed-search", type=int, default=0, help="点の良い提案から今の直しでも広げる数")
    ap.add_argument("--no-baseline", action="store_true", help="今の推定器を回さない（提案つきだけ）")
    ap.add_argument("--workers", type=int, default=default_workers())
    ap.add_argument("--json", type=Path, help="結果を JSON に書く")
    args = ap.parse_args(argv)
    logging.disable(logging.CRITICAL)

    pairs = select_sessions(args.logs or None, args.split, args.sessions)
    base, llm, raw = run(pairs, model=args.model, cache_dir=args.cache_dir, with_seat=not args.no_seat,
                         read_logp=args.read_logp, seed_search=args.seed_search, workers=args.workers,
                         baseline=not args.no_baseline)
    print(f"モデル {args.model}・席を入れる {not args.no_seat}・読みの log 確率 {args.read_logp}"
          f"・提案から広げる {args.seed_search}")
    for line in format_raw(raw):
        print(line)
    for r in (base, llm):
        if r is not None:
            for line in format_v1(r):
                print(line)
    diff = compare(base, llm) if base is not None else {}
    for label, keys in diff.items():
        print(f"{label} {len(keys)}: {' '.join(keys)}")
    if args.json:
        args.json.write_text(json.dumps({
            "model": args.model, "with_seat": not args.no_seat, "read_logp": args.read_logp,
            "seed_search": args.seed_search, "raw": dataclasses.asdict(raw),
            "baseline": dataclasses.asdict(base) if base is not None else None,
            "llm": dataclasses.asdict(llm), "diff": diff,
        }, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
