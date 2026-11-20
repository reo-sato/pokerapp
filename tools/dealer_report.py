#!/usr/bin/env python3
"""tools/dealer_report.py — 営業のあとの聞き取りの振り返り（ディーラーごと・1 時間ごと, ADR-0074）

ディーラーの言い方で直せる数を、ディーラー（ハンドの記録の `dealer`。入れていなければ「名前なし」）ごと・1 時間ごとに並べる。
真のアクションが無くても出せる数（毎回）:

- フォールドの声: 書き起こしで「フォールド」と読めた語の数 / 記録のフォールドの数（札の離脱・マックで決めたものも含む）
- 補ったチェック・コール: 言われず、あとの札の動きから補ったチェック / コール（`actor_source=implied`）/ チェック・コールの数
- 額の無いベット・レイズ: 額が聞き取れず最小の額などにしたもの / ベット・レイズの数
- 続けて言った発話: 2 つ以上のアクションを読んだ発話 / アクションを読んだ発話（区切って言えていれば 1 つずつになる）
- 5 秒で切れた発話: 話し続けて途中で切った発話（`audio/recorder.py` の 5 秒）

真のアクション（`<sid>.ground_truth.json`）があれば、配る人が言うアクションのうち読めた割合（`eval_store.listening_stop_metrics`、
種類ごと）も出す。`--brief` はディーラー本人に見せる版（数字 3 つと「次に気をつける 1 つ」。書き起こしの文は出さない）。

    TZ=Asia/Tokyo python tools/dealer_report.py pokerlogs_<日時>_text.zip
    TZ=Asia/Tokyo python tools/dealer_report.py logs --session <sid> --brief
"""
from __future__ import annotations

import argparse
import logging
import shutil
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.eval_store import (  # noqa: E402
    _read_json, _read_jsonl, find_sessions, format_by_kind, hand_windows, is_excluded, listening_stop_metrics,
    open_input, truth_hands,
)

NO_NAME = "（名前なし）"
# 話し続けて途中で切った発話（`audio/recorder.py` の `_MAX_BUFFER_SECONDS` = 5 秒。少し手前から数える）
CUT_SEC = 4.9


@dataclass
class Tally:
    hands: set = field(default_factory=set)
    folds: int = 0
    fold_words: int = 0
    check_call: int = 0
    implied: int = 0
    wagers: int = 0
    no_amount: int = 0
    action_utts: int = 0
    multi_utts: int = 0
    cut_utts: int = 0
    gt_hands: list = field(default_factory=list)

    def add(self, other: "Tally") -> None:
        self.hands |= other.hands
        for k in ("folds", "fold_words", "check_call", "implied", "wagers", "no_amount", "action_utts", "multi_utts",
                  "cut_utts"):
            setattr(self, k, getattr(self, k) + getattr(other, k))
        self.gt_hands += other.gt_hands


def _hour(t: float) -> str:
    return datetime.fromtimestamp(t).strftime("%m/%d %H時")


def _hand_of(t: float, windows: dict[int, tuple[float, float]]) -> Optional[int]:
    return next((hid for hid, (a, b) in windows.items() if a <= t < b), None)


def tally_session(record: dict, transcripts: list[dict], gt: Optional[dict] = None) -> dict[tuple[str, str], Tally]:
    """1 セッションの数を (ディーラー, 時間帯) ごとに。時間帯はハンドの始まりの時刻。"""
    hands = [h for h in record.get("hands") or [] if isinstance(h, dict)]
    windows = hand_windows(hands)
    key_of: dict[int, tuple[str, str]] = {}
    out: dict[tuple[str, str], Tally] = defaultdict(Tally)
    for h in hands:
        hid = h.get("hand_id")
        if hid not in windows:
            continue
        key = (str(h.get("dealer") or NO_NAME), _hour(windows[hid][0]))
        key_of[hid] = key
        t = out[key]
        t.hands.add((record.get("session_id"), hid))
        for a in h.get("actions") or []:
            act = a.get("action")
            if act == "fold" and a.get("street") != "showdown":
                t.folds += 1
            elif act in ("check", "call"):
                t.check_call += 1
                t.implied += a.get("actor_source") == "implied"
            elif act in ("bet", "raise"):
                t.wagers += 1
                t.no_amount += "no_amount" in str(a.get("reason") or "")
    for row in transcripts:
        start = row.get("utterance_start_ts")
        if not isinstance(start, (int, float)):
            continue
        hid = _hand_of(float(start), windows)
        if hid not in key_of:
            continue
        t = out[key_of[hid]]
        events = [e for e in row.get("events") or [] if isinstance(e, dict)]
        actions = [e for e in events if e.get("action") in ("fold", "check", "call", "bet", "raise", "allin")]
        t.fold_words += sum(1 for e in actions if e.get("action") == "fold")
        if actions:
            t.action_utts += 1
            t.multi_utts += len(actions) >= 2
        if float(row.get("audio_sec") or 0) >= CUT_SEC and (row.get("text") or "").strip() and not row.get("noise"):
            t.cut_utts += 1
    for h in (gt or {}).get("hands") or []:
        if h.get("hand_id") in key_of and not is_excluded(h):
            out[key_of[h["hand_id"]]].gt_hands.append((h, hands, transcripts))
    return out


def _rate(a: int, b: int) -> str:
    return f"{a}/{b}（{a / b:.0%}）" if b else "—"


def _truth_line(t: Tally) -> Optional[str]:
    """真のアクションのあるハンドの、配る人が言うアクションのうち読めた割合（セッションをまたいで足す）。"""
    if not t.gt_hands:
        return None
    read = actions = 0
    by_kind: dict[str, list[int]] = {}
    groups: dict[int, tuple[list, list, list]] = {}
    for h, hands, transcripts in t.gt_hands:
        groups.setdefault(id(hands), ([], hands, transcripts))[0].append(h)
    for gt_hands, hands, transcripts in groups.values():
        m = listening_stop_metrics({"hands": gt_hands}, hands, transcripts)
        read += m["read"]
        actions += m["actions"]
        for k, (a, b) in m["by_kind"].items():
            pair = by_kind.setdefault(k, [0, 0])
            pair[0] += a
            pair[1] += b
    return f"読めた {_rate(read, actions)}" + (f"・{format_by_kind(by_kind)}" if by_kind else "")


def format_rows(groups: dict[str, Tally]) -> list[str]:
    lines = []
    for name, t in groups.items():
        lines.append(f"{name}: {len(t.hands)} ハンド")
        lines.append(f"  フォールドの声 {_rate(min(t.fold_words, t.folds), t.folds)}"
                     f"・補ったチェック / コール {_rate(t.implied, t.check_call)}"
                     f"・額の無いベット / レイズ {_rate(t.no_amount, t.wagers)}")
        lines.append(f"  続けて言った発話 {_rate(t.multi_utts, t.action_utts)}・5 秒で切れた発話 {t.cut_utts}")
        truth = _truth_line(t)
        if truth:
            lines.append(f"  真のアクション: {truth}")
    return lines


def advice(t: Tally) -> str:
    """次に気をつける 1 つ: 足りない割合がいちばん大きいもの。"""
    gaps = []
    if t.folds:
        gaps.append((1 - min(t.fold_words, t.folds) / t.folds, "フォールドは札が離れたのを見てから、1 人ずつ「フォールド」と言う"))
    if t.check_call:
        gaps.append((t.implied / t.check_call, "コール・チェックは毎回言う"))
    if t.action_utts:
        gaps.append((t.multi_utts / t.action_utts, "アクションごとに 1 拍（半秒）あけて言う"))
    if t.wagers:
        gaps.append((t.no_amount / t.wagers, "ベット・レイズは額を 1 息で言い切る"))
    if not gaps:
        return "—"
    worst = max(gaps, key=lambda g: g[0])
    return worst[1] if worst[0] > 0.05 else "このままで大丈夫です"


def format_brief(by_dealer: dict[str, Tally]) -> list[str]:
    lines = []
    for name, t in by_dealer.items():
        lines.append(f"{name}（{len(t.hands)} ハンド）")
        lines.append(f"  「フォールド」の声 {_pct(min(t.fold_words, t.folds), t.folds)}"
                     f"・言えたコール / チェック {_pct(t.check_call - t.implied, t.check_call)}"
                     f"・1 つずつ区切れた発話 {_pct(t.action_utts - t.multi_utts, t.action_utts)}")
        lines.append(f"  次に気をつける: {advice(t)}")
    return lines


def _pct(a: int, b: int) -> str:
    return f"{a / b:.0%}" if b else "—"


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="聞き取りの振り返り（ディーラーごと・1 時間ごと）")
    ap.add_argument("path", type=Path, nargs="+", help="pack_logs の zip（分けた zip は全部）/ 展開したフォルダ / logs")
    ap.add_argument("--session", action="append", help="セッション ID（先頭の数文字でよい）")
    ap.add_argument("--brief", action="store_true", help="ディーラー本人に見せる版（数字 3 つと次に気をつける 1 つ）")
    args = ap.parse_args(argv)
    logging.disable(logging.WARNING)
    root, cleanup = open_input(list(args.path))
    try:
        by_dealer: dict[str, Tally] = defaultdict(Tally)
        by_hour: dict[str, Tally] = defaultdict(Tally)
        for files in find_sessions(root, args.session):
            record = _read_json(files.path(".json")) or {}
            if not record.get("hands"):
                continue
            gt = truth_hands(_read_json(files.path(".ground_truth.json")))
            for (dealer, hour), t in tally_session(record, _read_jsonl(files.path(".transcripts.jsonl")), gt).items():
                by_dealer[dealer].add(t)
                by_hour[f"{hour}（{dealer}）"].add(t)
        if not by_dealer:
            print("ハンドの記録のあるセッションがありません", file=sys.stderr)
            return 1
        if args.brief:
            print("\n".join(format_brief(dict(sorted(by_dealer.items())))))
            return 0
        print("■ ディーラーごと")
        print("\n".join(format_rows(dict(sorted(by_dealer.items())))))
        print("\n■ 1 時間ごと")
        print("\n".join(format_rows(dict(sorted(by_hour.items())))))
    finally:
        if cleanup is not None:
            shutil.rmtree(cleanup, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
