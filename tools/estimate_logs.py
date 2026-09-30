#!/usr/bin/env python3
"""tools/estimate_logs.py — 店舗のログのハンドごとに推定器 v1 を回し、推定のファイルを書く（ADR-0056）

記録を読む側（お客さん向けの画面・スタッフの画面・真のアクションの入力・PHH の書き出し）は、`logs/<sid>.estimate.json`
があれば推定をライブの記録に重ねて**記録の本体**として読む（`core/hand_estimate.py`）。切り替えの条件（ADR-0056
追記 2: 事前登録した版で、新しいセッションの評価に通ってから）を満たすまでは、`--shadow` で読む側が使わない名前
（`<sid>.estimate.shadow.json`）に書く。

    python tools/estimate_logs.py logs_2026-10-01.zip                  # 推定だけ見る（書かない）
    python tools/estimate_logs.py C:\\PokerHandLogger\\logs --shadow   # 影のファイルに書く
    python tools/estimate_logs.py logs --latest --shadow               # いちばん新しいセッションだけ（所要を測る）
    python tools/estimate_logs.py C:\\PokerHandLogger\\logs --write    # 記録の本体にする（評価に通ってから）
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.atomic_io import atomic_write_json  # noqa: E402
from core.hand_estimate import ESTIMATE_SUFFIX  # noqa: E402
from integration.estimator import (  # noqa: E402
    ESTIMATOR_VERSION,
    PARAMS,
    HandResult,
    SessionEstimator,
    params_hash,
    record_key,
)
from integration.world_replay import PresenceTimeline  # noqa: E402

SHADOW_SUFFIX = ".estimate.shadow.json"
MATCH_SEC = 10.0          # 推定のハンドとライブの記録のハンドを、始まりの時刻がこの秒数以内なら同じとみる


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _default_workers() -> int:
    import os

    return max(1, min(4, (os.cpu_count() or 2) - 1))


def presence_for(folder: Optional[Path], session_id: str) -> Optional[PresenceTimeline]:
    """席の札の在否の履歴: fixture の `presence.jsonl`、店舗のログの `<sid>.table_state.jsonl`。"""
    if folder is None:
        return None
    fixture = folder / "presence.jsonl"
    if fixture.exists():
        return PresenceTimeline.from_rows(_jsonl(fixture))
    state = folder / f"{session_id}.table_state.jsonl"
    if state.exists():
        return PresenceTimeline.from_table_state(_jsonl(state))
    return None


def _epoch(iso: Optional[str]) -> Optional[float]:
    try:
        return datetime.fromisoformat(iso).timestamp() if iso else None
    except ValueError:
        return None


def newest_session(paths: list[Path]) -> Optional[str]:
    """ログのフォルダのうち、いちばん新しいセッション（events.jsonl を最後に書いた）の ID。"""
    events = [e for p in paths if p.is_dir() for e in p.rglob("*.events.jsonl")]
    if not events:
        return None
    return max(events, key=lambda e: e.stat().st_mtime).name[: -len(".events.jsonl")]


def _live_hand(live: list[dict], started_at: Optional[str]) -> Optional[dict]:
    """推定のハンドと同じハンド（始まりの時刻がいちばん近く、`MATCH_SEC` 以内）をライブの記録から。"""
    t = _epoch(started_at)
    if t is None:
        return None
    best = min((h for h in live if _epoch(h.get("started_at")) is not None),
               key=lambda h: abs(_epoch(h["started_at"]) - t), default=None)
    if best is None or abs(_epoch(best["started_at"]) - t) > MATCH_SEC:
        return None
    return best


def _summary(hand: dict) -> list[str]:
    return [f"{a.get('street')} 席{a.get('seat')} {a.get('action')}{' ' + str(a['amount']) if a.get('amount') else ''}"
            for a in hand.get("actions") or [] if a.get("street") != "showdown"]


def entry_for(result: HandResult, live: Optional[dict]) -> dict:
    """1 ハンドの推定（`core/hand_estimate.py` が読む形）。hand_id と始まりの時刻はライブの記録のもの。"""
    hand = dict(result.best.hand)
    if live is not None:
        hand["hand_id"] = live.get("hand_id")
    alternatives = [{"posterior": round(p, 3), "score": c.score, "edits": [e.label for e in c.edits],
                     "actions": _summary(c.hand), "winner_seat": c.hand.get("winner_seat")}
                    for c, p in list(zip(result.candidates, result.posteriors))[1:3]]
    return {
        "hand_id": hand.get("hand_id"),
        "started_at": (live or hand).get("started_at"),
        "estimated_at": datetime.now().isoformat(timespec="seconds"),
        "changed": record_key(live) != record_key(result.best.hand) if live is not None else bool(result.best.edits),
        "margin": result.margin,
        "posterior": round(result.posteriors[0], 3) if result.posteriors else None,
        "review": bool(result.reasons),
        "edits": [e.label for e in result.best.edits],
        "notes": list(result.reasons),
        "hand": hand,
        "alternatives": alternatives,
    }


def estimate_session(events: list, transcripts: list[dict], presence: Optional[PresenceTimeline], setup: dict,
                     flags: dict, session_id: str, live_hands: list[dict], params: dict = PARAMS,
                     on_hand: Optional[Callable[[HandResult, Optional[dict]], None]] = None,
                     workers: int = 1) -> dict:
    """セッションの全ハンドの推定（推定のファイルの中身）。`workers` > 1 ならハンドを並べて回す（結果は同じ）。"""
    est = SessionEstimator(events, transcripts, presence, setup, flags, session_id, params)
    hands: dict[str, dict] = {}

    def add(result: HandResult) -> None:
        live = _live_hand(live_hands, result.window.base.get("started_at"))
        entry = entry_for(result, live)
        if entry["hand_id"] is not None:
            hands[str(entry["hand_id"])] = entry
        if on_hand:
            on_hand(result, live)

    est.estimate(on_hand=add, workers=workers)
    return {"tool": "estimator", "estimator_version": ESTIMATOR_VERSION, "params_hash": params_hash(params),
            "session_id": session_id, "hands": hands}


def main(argv: Optional[list[str]] = None) -> int:
    from tools.estimate import inputs_from_logs

    ap = argparse.ArgumentParser(description="店舗のログのハンドごとに推定器 v1 を回す")
    ap.add_argument("paths", nargs="+", type=Path, help="ログのフォルダ・pack_logs の zip（分けた zip は全部）")
    ap.add_argument("--session", action="append", help="このセッション（ID の頭）だけ")
    ap.add_argument("--latest", action="store_true", help="いちばん新しいセッションだけ（ログのフォルダを渡したとき）")
    ap.add_argument("--workers", type=int, default=_default_workers(),
                    help="ハンドを並べて推定するプロセスの数（結果は同じ。既定 = CPU の数 − 1、最大 4）")
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--shadow", action="store_true", help=f"推定を <sid>{SHADOW_SUFFIX} に書く（読む側は使わない）")
    group.add_argument("--write", action="store_true",
                       help=f"推定を <sid>{ESTIMATE_SUFFIX} に書く（読む側が記録の本体として使う。評価に通ってから）")
    args = ap.parse_args(argv)
    logging.disable(logging.CRITICAL)
    only = args.session
    if args.latest:
        newest = newest_session(args.paths)
        if newest is None:
            print("セッションが見つかりません（--latest はログのフォルダを渡してください）")
            return 2
        only = [newest]
    inputs, tmp = inputs_from_logs(args.paths, only)
    if (args.shadow or args.write) and tmp is not None:
        print("zip には書けません（ログのフォルダを渡してください）")
        return 2
    for inp in inputs:
        record_path = inp.folder / f"{inp.session_id}.json" if inp.folder is not None else None
        live = []
        if record_path is not None and record_path.exists():
            live = list((json.loads(record_path.read_text(encoding="utf-8")) or {}).get("hands") or [])
        print(f"=== {inp.session_id[:8]}（{len(live)} ハンド）")

        def show(result: HandResult, live_hand: Optional[dict]) -> None:
            changed = live_hand is not None and record_key(live_hand) != record_key(result.best.hand)
            hid = live_hand.get("hand_id") if live_hand else result.window.hand_id
            mark = "変えた" if changed else "同じ"
            review = f" 要確認: {' / '.join(result.reasons)}" if result.reasons else ""
            print(f"  ハンド {hid}: ライブと{mark}（事後 {result.posteriors[0]:.2f}・次点との差 {result.margin}）{review}")
            if changed:
                print(f"      ライブ: {' | '.join(_summary(live_hand))}")
                print(f"      推定:   {' | '.join(_summary(result.best.hand))}")

        started = time.time()
        data = estimate_session(inp.events, inp.transcripts, presence_for(inp.folder, inp.session_id), inp.setup,
                                inp.flags, inp.session_id, live, on_hand=show, workers=args.workers)
        # 切り替えの条件の「店舗 PC で 1 セッション 1 分以内」（ADR-0056 追記 2）
        print(f"  所要 {time.time() - started:.0f} 秒（{len(data['hands'])} ハンド・{args.workers} プロセス）")
        if args.shadow or args.write:
            out = inp.folder / f"{inp.session_id}{SHADOW_SUFFIX if args.shadow else ESTIMATE_SUFFIX}"
            atomic_write_json(out, data)
            print(f"  → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
