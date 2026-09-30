#!/usr/bin/env python3
"""tools/bench_hands.py — 全部正しいハンドの割合（ハンドの整合の物差し, オーナー 2026-09-30）

発話の読みの当たり（行）ではなく、**ハンドが丸ごと正しいか**（真のアクションの行が全部正しく・余計な行が無く・勝者が
合い・ボードがあればボードも合う = `measure_capture_accuracy.hand_fully_correct`）を数える。推定器を良くしたかどうかは、
この数字で決める。

- 店舗: 真のアクションのある店舗のハンド（`tests/fixtures/store`。09-27・09-29 の一人テストの実卓）
- 台本: 声だけの台本のハンド（`tests/fixtures/script`。正解は台本。台本の画面を押した順に並べる）
- シミュレーション: 台本 + 決まった割合の聞き違い（`tools/simulate.py`。**精度の主張には使わない**）

推定器 v1（`integration/estimator.py`, 既定）は、生の観測の再生（直しの無い = 読み直し）と比べ、要確認の漏れ（誤りの
あるハンドに要確認が付かない）・付きすぎ（正しいハンドに付く）と、誤りの内訳（正解が候補に無い = 直しで表せないか
探しきれない / 候補にあるが点で負けた）を出す（監査 2026-09-30 の 3 分解・区間・対の比較）。`--v0` で推定器 v0。

使い方:

    python tools/bench_hands.py                 # v1: 店舗 + 台本
    python tools/bench_hands.py --sim 2         # v1: シミュレーションも 2 セッション
    python tools/bench_hands.py --v0 --quick    # v0: 3 つとも（シミュレーションは 1 セッション）
    python tools/bench_hands.py --json
"""
from __future__ import annotations

import argparse
import json
import logging
import math
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


# ───────────────────────── 推定器 v1 ─────────────────────────


def wilson(x: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """割合 x/n の 95% 区間（Wilson）。"""
    if n <= 0:
        return 0.0, 1.0
    p = x / n
    center = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return max(0.0, center - half), min(1.0, center + half)


@dataclass
class V1Result:
    name: str
    hands: int = 0
    base_exact: int = 0           # 生の観測の再生（直し無し = 読み直し）
    exact: int = 0                # 推定器 v1 の 1 番
    in_candidates: int = 0        # 正解が候補に入った
    flagged: int = 0              # 要確認が付いた
    better: list[str] = field(default_factory=list)
    worse: list[str] = field(default_factory=list)
    failing: list[str] = field(default_factory=list)
    unflagged_errors: list[str] = field(default_factory=list)   # 誤りがあるのに要確認が付かない（あってはならない）
    flagged_correct: list[str] = field(default_factory=list)    # 正しいのに要確認（直しを使った・差が小さい など）
    not_found: list[str] = field(default_factory=list)          # 正解が候補に無い（直しで表せない or 探しきれない）
    outscored: list[str] = field(default_factory=list)          # 正解は候補にあるが点で負けた（採点の誤り）
    edits: dict[str, int] = field(default_factory=dict)         # 1 番の直しの数 → ハンド数
    seconds: float = 0.0
    session_seconds: list[float] = field(default_factory=list)   # セッションごとの秒数（1 セッション 1 分以内の条件）
    best: dict[str, str] = field(default_factory=dict)          # ハンド → 1 番の記録（探索の確かめで比べる）
    correct: list[str] = field(default_factory=list)
    reasons: list[int] = field(default_factory=list)            # ハンドごとの要確認の理由の数（1 ハンド 2 件以下の目安）
    # ライブの記録（店舗のログのみ）: 切り替えの条件の「同じハンドで比べて悪くなったハンド 0」
    live_hands: int = 0
    live_exact: int = 0
    better_than_live: list[str] = field(default_factory=list)
    worse_than_live: list[str] = field(default_factory=list)

    def add_hand(self, key: str, truth: dict, result, live=None) -> None:
        from integration.estimator import record_key
        from tools.measure_capture_accuracy import hand_fully_correct

        base = next((c for c in result.candidates if not c.edits), None)
        ok_base = hand_fully_correct(truth, base.hand if base else None)
        ok = hand_fully_correct(truth, result.best.hand)
        found = any(hand_fully_correct(truth, c.hand) for c in result.candidates)
        flagged = bool(result.reasons)
        self.best[key] = repr(record_key(result.best.hand))
        self.reasons.append(len(result.reasons))
        if ok:
            self.correct.append(key)
        if live is not _NO_LIVE:
            ok_live = hand_fully_correct(truth, live) if isinstance(live, dict) else False
            self.live_hands += 1
            self.live_exact += ok_live
            if ok and not ok_live:
                self.better_than_live.append(key)
            if ok_live and not ok:
                self.worse_than_live.append(key)
        self.hands += 1
        self.base_exact += ok_base
        self.exact += ok
        self.in_candidates += found
        self.flagged += flagged
        n_edits = str(len(result.best.edits))
        self.edits[n_edits] = self.edits.get(n_edits, 0) + 1
        if ok and not ok_base:
            self.better.append(key)
        if ok_base and not ok:
            self.worse.append(key)
        if not ok:
            self.failing.append(key)
            (self.outscored if found else self.not_found).append(key)
            if not flagged:
                self.unflagged_errors.append(key)
        elif flagged:
            self.flagged_correct.append(key)


def v1_inputs(store: bool = True, script: bool = True, sim_sessions: int = 0, sim_hands: int = 30,
              logs: Optional[list[Path]] = None):
    """(名前, [(SessionInput, 席の札の在否の履歴 or None)]) の並び。"""
    from integration.world_replay import PresenceTimeline

    def presence_of(inp: SessionInput):
        if inp.folder is None:
            return None
        path = inp.folder / "presence.jsonl"
        if path.exists():
            return PresenceTimeline.from_rows(
                json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
        state = inp.folder / f"{inp.session_id}.table_state.jsonl"       # 店舗のログ（卓状態の履歴）
        if state.exists():
            return PresenceTimeline.from_table_state(
                [json.loads(line) for line in state.read_text(encoding="utf-8").splitlines() if line.strip()])
        return None

    out = []
    if logs:
        from tools.estimate import inputs_from_logs

        inputs, _tmp = inputs_from_logs(logs)
        out.append(("店舗のログ", [(inp, presence_of(inp)) for inp in inputs if inp.truth.get("hands")]))
    if store:
        out.append(("店舗（実卓）", [(inp, presence_of(inp)) for inp in inputs_from_fixtures()
                                  if inp.transcripts and presence_of(inp) is not None]))
    if script:
        out.append(("台本（声だけ）", [(inp, None) for inp in inputs_from_script_fixtures()]))
    if sim_sessions > 0:
        from tools.simulate import Noise, simulate_session

        out.append(("シミュレーション", [(simulate_session(seed, sim_hands, noise=Noise()), None)
                                      for seed in range(1, sim_sessions + 1)]))
    return out


def run_bench_v1(*, store: bool = True, script: bool = True, sim_sessions: int = 0,
                 params: Optional[dict] = None, logs: Optional[list[Path]] = None) -> list[V1Result]:
    from integration.estimator import PARAMS as V1_PARAMS
    from integration.estimator import SessionEstimator

    from tools.estimate_logs import _live_hand

    out = []
    for name, sessions in v1_inputs(store, script, sim_sessions, logs=logs):
        res = V1Result(name)
        for inp, presence in sessions:
            started = time.time()
            est = SessionEstimator(inp.events, inp.transcripts, presence, inp.setup, inp.flags, inp.session_id,
                                   params or V1_PARAMS)
            truth = {int(h["hand_id"]): h for h in inp.truth.get("hands") or []}
            live_hands = _live_record(inp)
            for w in est.windows():
                # 店舗のログはライブの記録のハンド番号で真のアクションが付く（始まりの時刻で合わせる）
                live = _live_hand(live_hands, w.base.get("started_at")) if live_hands else None
                hid = int(live["hand_id"]) if live is not None else w.hand_id
                if hid in truth:
                    res.add_hand(f"{short_id(inp.session_id)}#{hid}", truth[hid], est.estimate_hand(w),
                                 live=live if live_hands else _NO_LIVE)
            res.session_seconds.append(round(time.time() - started, 1))
            res.seconds += time.time() - started
        out.append(res)
    return out


_NO_LIVE = object()      # ライブの記録が無い（fixture・台本）= ライブとは比べない


def _live_record(inp: SessionInput) -> list[dict]:
    """店舗のログのライブの記録（`<sid>.json` のハンド）。fixture・台本には無い。"""
    path = inp.folder / f"{inp.session_id}.json" if inp.folder is not None else None
    if path is None or not path.exists():
        return []
    try:
        return list((json.loads(path.read_text(encoding="utf-8")) or {}).get("hands") or [])
    except (OSError, ValueError):
        return []


def wide_params(params: Optional[dict] = None) -> dict:
    """探索を広げた値（ビーム幅・広げる直しを 2 倍）。1 番が変わる率 = 探索の誤りの目安（監査 2026-09-30）。"""
    from integration.estimator import PARAMS as V1_PARAMS

    base = dict(params or V1_PARAMS)
    return dict(base, beam=int(base["beam"]) * 2, expand=int(base["expand"]) * 2)


def format_search_check(normal: list[V1Result], wide: list[V1Result]) -> list[str]:
    lines = []
    for a, b in zip(normal, wide):
        changed = [k for k in a.best if k in b.best and a.best[k] != b.best[k]]
        fixed = [k for k in changed if k in b.correct and k not in a.correct]
        broken = [k for k in changed if k in a.correct and k not in b.correct]
        lines.append(f"{a.name}: 探索を 2 倍に広げると 1 番が変わる {len(changed)}/{a.hands}"
                     f"（正しくなる {len(fixed)}・正しくなくなる {len(broken)}）"
                     f" ／ 全部正しいハンド {a.exact} → {b.exact}（{a.seconds:.0f} 秒 → {b.seconds:.0f} 秒）")
        if changed:
            lines.append(f"    1 番が変わる: {' '.join(changed)}")
    return lines


def format_v1(r: V1Result) -> list[str]:
    lo, hi = wilson(r.exact, r.hands)
    lines = [
        f"{r.name}: 全部正しいハンド 読み直し {r.base_exact}/{r.hands}（{_pct(r.base_exact, r.hands)}）"
        f" → 推定 {r.exact}/{r.hands}（{_pct(r.exact, r.hands)}, 95% 区間 {lo:.0%}〜{hi:.0%}）"
        f" ／ 良くなった {len(r.better)}・悪くなった {len(r.worse)}"
        f" ／ 要確認 {r.flagged}（誤りに付かない {len(r.unflagged_errors)}・正しいのに付く {len(r.flagged_correct)}）"
        f"（{r.seconds:.0f} 秒）",
        f"    誤り {len(r.failing)} の内訳: 正解が候補に無い {len(r.not_found)}・候補にあるが点で負けた {len(r.outscored)}"
        f" ／ 1 番の直しの数 {dict(sorted(r.edits.items()))}",
        f"    要確認の理由 1 ハンド平均 {sum(r.reasons) / max(1, len(r.reasons)):.1f}・最多 {max(r.reasons, default=0)}"
        f" ／ 1 セッション最長 {max(r.session_seconds, default=0.0):.0f} 秒",
    ]
    if r.live_hands:
        lines.append(f"    ライブの記録 {r.live_exact}/{r.live_hands} → 推定 {r.exact}/{r.hands}"
                     f"（ライブより良くなった {len(r.better_than_live)}・悪くなった {len(r.worse_than_live)}）")
    for label, keys in (("良くなった", r.better), ("悪くなった", r.worse), ("誤りに要確認が付かない", r.unflagged_errors),
                        ("正解が候補に無い", r.not_found), ("点で負けた", r.outscored),
                        ("ライブより悪くなった", r.worse_than_live)):
        if keys:
            lines.append(f"    {label}: {' '.join(keys[:20])}{' …' if len(keys) > 20 else ''}")
    return lines


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="全部正しいハンドの割合（店舗・台本・シミュレーション）")
    ap.add_argument("logs", nargs="*", type=Path,
                    help="v1: 店舗のログ（pack_logs の zip かフォルダ）。渡すと、真のアクションのあるセッションだけを測る")
    ap.add_argument("--v0", action="store_true", help="推定器 v0（tools/estimate.py）で測る")
    ap.add_argument("--quick", action="store_true", help="v0: シミュレーションを 1 セッションにする")
    ap.add_argument("--no-sim", action="store_true", help="v0: シミュレーションを回さない")
    ap.add_argument("--sessions", type=int, default=4, help="v0: シミュレーションのセッション数（1 セッション 30 ハンド）")
    ap.add_argument("--sim", type=int, default=0, help="v1: シミュレーションのセッション数（既定 0 = 回さない）")
    ap.add_argument("--no-script", action="store_true", help="v1: 台本を回さない")
    ap.add_argument("--search-check", action="store_true",
                    help="v1: 探索を 2 倍に広げてもう 1 回回し、1 番が変わる率（探索の誤りの目安）を出す")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    logging.disable(logging.CRITICAL)          # 候補の再生で出るエンジンのログは要らない
    if not args.v0:
        def run(params: Optional[dict] = None) -> list[V1Result]:
            if args.logs:
                return run_bench_v1(store=False, script=False, logs=args.logs, params=params)
            return run_bench_v1(script=not args.no_script, sim_sessions=args.sim, params=params)

        results_v1 = run()
        if args.search_check:
            wide = run(wide_params())
            if not args.json:
                for r in results_v1:
                    print("\n".join(format_v1(r)))
                print("\n".join(format_search_check(results_v1, wide)))
                return 0
        if args.json:
            print(json.dumps([asdict(r) for r in results_v1], ensure_ascii=False, indent=1))
        else:
            for r in results_v1:
                print("\n".join(format_v1(r)))
        return 0
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
