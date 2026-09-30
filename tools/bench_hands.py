#!/usr/bin/env python3
"""tools/bench_hands.py — 全部正しいハンドの割合（ハンドの整合の物差し, オーナー 2026-09-30）

発話の読みの当たり（行）ではなく、**ハンドが丸ごと正しいか**（真のアクションの行が全部正しく・余計な行が無く・勝者が
合い・ボードがあればボードも合う = `measure_capture_accuracy.hand_fully_correct`）を数える。推定器（`tools/estimate.py`）
を良くしたかどうかは、この数字で決める。

- 店舗: 真のアクションのある店舗のハンド（`tests/fixtures/store`。09-27・09-29 の一人テストの実卓）
- 台本: 声だけの台本のハンド（`tests/fixtures/script`。正解は台本。台本の画面を押した順に並べる）
- シミュレーション: 台本 + 決まった割合の聞き違い（`tools/simulate.py`。**精度の主張には使わない**）

使い方:

    python tools/bench_hands.py                 # 3 つとも（シミュレーションは 4 セッション × 30 ハンド）
    python tools/bench_hands.py --quick         # シミュレーションを 1 セッションに
    python tools/bench_hands.py --no-sim --json
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.estimate import (  # noqa: E402
    PARAMS,
    SessionInput,
    compare_with_truth,
    estimate_session,
    inputs_from_fixtures,
    inputs_from_script_fixtures,
)


def short_id(session_id: str) -> str:
    """表示用のセッションの短い名前（台本の「2026-09-30_165030_script_voice」は時刻の部分）。"""
    m = re.match(r"\d{4}-\d{2}-\d{2}_(\d{6})", session_id)
    return m.group(1) if m else session_id[:8]


@dataclass
class SourceResult:
    name: str
    hands: int = 0
    default_exact: int = 0        # 読み直し（いまの読み取りで再生 = 推定器なし）
    estimate_exact: int = 0       # 推定器
    in_nbest: int = 0             # 正解が推定器の候補（N-best）に入った
    default_rows: list[int] = field(default_factory=lambda: [0, 0])
    estimate_rows: list[int] = field(default_factory=lambda: [0, 0])
    better: list[str] = field(default_factory=list)   # 推定で丸ごと正しくなったハンド
    worse: list[str] = field(default_factory=list)    # 推定で丸ごと正しくなくなったハンド
    failing: list[str] = field(default_factory=list)  # 推定でも正しくないハンド
    seconds: float = 0.0

    def add(self, inp: SessionInput, params: dict) -> None:
        started = time.time()
        estimates, _ = estimate_session(inp, params)
        for r in compare_with_truth(estimates, inp.truth):
            key = f"{short_id(inp.session_id)}#{r.hand_id}"
            self.hands += 1
            self.default_exact += r.default_exact
            self.estimate_exact += r.estimate_exact
            self.in_nbest += r.in_nbest
            self.default_rows[0] += r.default[0]
            self.default_rows[1] += r.default[1]
            self.estimate_rows[0] += r.estimate[0]
            self.estimate_rows[1] += r.estimate[1]
            if r.estimate_exact and not r.default_exact:
                self.better.append(key)
            if r.default_exact and not r.estimate_exact:
                self.worse.append(key)
            if not r.estimate_exact:
                self.failing.append(key)
        self.seconds += time.time() - started


def run_bench(*, store: bool = True, script: bool = True, sim_sessions: int = 4, sim_hands: int = 30,
              params: dict = PARAMS) -> list[SourceResult]:
    out: list[SourceResult] = []
    if store:
        res = SourceResult("店舗（実卓）")
        for inp in inputs_from_fixtures():
            res.add(inp, params)
        out.append(res)
    if script:
        res = SourceResult("台本（声だけ）")
        for inp in inputs_from_script_fixtures():
            res.add(inp, params)
        out.append(res)
    if sim_sessions > 0:
        from tools.simulate import Noise, simulate_session

        res = SourceResult("シミュレーション")
        for seed in range(1, sim_sessions + 1):
            res.add(simulate_session(seed, sim_hands, noise=Noise()), params)
        out.append(res)
    return out


def _pct(a: int, b: int) -> str:
    return f"{a / b:.0%}" if b else "—"


def format_result(r: SourceResult) -> list[str]:
    lines = [
        f"{r.name}: 全部正しいハンド 読み直し {r.default_exact}/{r.hands}（{_pct(r.default_exact, r.hands)}）"
        f" → 推定 {r.estimate_exact}/{r.hands}（{_pct(r.estimate_exact, r.hands)}）"
        f" ／ 正解が候補に {r.in_nbest}/{r.hands}"
        f" ／ 行 {_pct(*r.default_rows)} → {_pct(*r.estimate_rows)}（{r.seconds:.0f} 秒）",
    ]
    if r.better:
        lines.append(f"    推定で正しくなった: {' '.join(r.better)}")
    if r.worse:
        lines.append(f"    推定で正しくなくなった: {' '.join(r.worse)}")
    if r.failing:
        lines.append(f"    推定でも正しくない: {' '.join(r.failing[:20])}{' …' if len(r.failing) > 20 else ''}")
    return lines


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="全部正しいハンドの割合（店舗・台本・シミュレーション）")
    ap.add_argument("--quick", action="store_true", help="シミュレーションを 1 セッションにする")
    ap.add_argument("--no-sim", action="store_true", help="シミュレーションを回さない")
    ap.add_argument("--sessions", type=int, default=4, help="シミュレーションのセッション数（1 セッション 30 ハンド）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    logging.disable(logging.CRITICAL)          # 候補の再生で出るエンジンのログは要らない
    sessions = 0 if args.no_sim else (1 if args.quick else args.sessions)
    results = run_bench(sim_sessions=sessions)
    if args.json:
        print(json.dumps([asdict(r) for r in results], ensure_ascii=False, indent=1))
    else:
        for r in results:
            print("\n".join(format_result(r)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
