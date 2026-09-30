#!/usr/bin/env python3
"""tools/sweep_estimator.py — 推定器 v1 の値の感度の掃引（監査 2 回目の推奨 5: 事前登録に添付する表）

値を 2 軸で動かして、全部正しいハンド・悪くなったハンド・誤りに要確認が付かないハンド・正しいのに要確認が付く
ハンドがどれだけ動くかを見る。値は「正誤が動かない区間の中央」に置く。

    python tools/sweep_estimator.py silent_fold_mean=1.5,3,5,8 fold_lag_b=0.25,0.4,0.8 --no-script
    python tools/sweep_estimator.py p_phantom_short=0.01,0.02,0.05 p_restate_same=0.03,0.1,0.3
"""
from __future__ import annotations

import argparse
import itertools
import json
import logging
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from integration.estimator import PARAMS  # noqa: E402
from tools.bench_hands import default_workers, run_bench_v1  # noqa: E402


def parse_axis(text: str) -> tuple[str, list[float]]:
    name, _, values = text.partition("=")
    if name not in PARAMS or not values:
        raise argparse.ArgumentTypeError(f"値の名前=値,値,… の形で（知らない名前: {name!r}）")
    return name, [float(v) for v in values.split(",") if v]


def sweep(axes: list[tuple[str, list[float]]], *, script: bool = True, workers: int = 1) -> list[dict]:
    rows = []
    names = [name for name, _ in axes]
    for combo in itertools.product(*(values for _, values in axes)):
        params = dict(PARAMS, **dict(zip(names, combo)))
        row: dict = {"params": dict(zip(names, combo))}
        for r in run_bench_v1(script=script, params=params, workers=workers):
            key = "store" if r.name.startswith("店舗") else "script"
            row[key] = {"hands": r.hands, "exact": r.exact, "worse": len(r.worse), "better": len(r.better),
                        "unflagged_errors": len(r.unflagged_errors), "flagged_correct": len(r.flagged_correct)}
            row[f"{key}_best"] = r.best
        rows.append(row)
        print(json.dumps({k: v for k, v in row.items() if not k.endswith("_best")}, ensure_ascii=False), flush=True)
    return rows


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="推定器 v1 の値の感度の掃引")
    ap.add_argument("axes", nargs="+", type=parse_axis, help="値の名前=値,値,…（2 つまで）")
    ap.add_argument("--no-script", action="store_true", help="台本を回さない（札の離脱の値は店舗だけで効く）")
    ap.add_argument("--workers", type=int, default=default_workers())
    ap.add_argument("--out", type=Path, help="結果を JSON で書く")
    args = ap.parse_args(argv)
    logging.disable(logging.CRITICAL)
    rows = sweep(args.axes, script=not args.no_script, workers=args.workers)
    if args.out:
        args.out.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
