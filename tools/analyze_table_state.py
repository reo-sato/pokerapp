"""tools/analyze_table_state.py

卓状態の履歴（`logs/{session}.table_state.jsonl`）から、実プレイ環境の**数字**を出す。

ADR-0045 D7「数値を決める前に計測する」の入力になる:

- **反映遅延** — RFID が観測してから卓状態を書くまで（`updated_at - observed_at`）。
  UI の表示遅延はこれ + モニタのポーリング間隔（1 秒）。
- **不在時間の分布** — 席から札が離れていた時間を「**戻ってきた**（持ち上げて見ただけ）」と
  「**戻らなかった**（マックした）」に分ける。`likely_folded` のしきい値（既定 20 秒）は
  **戻ってきた側の最大値より十分大きく**なければ誤検出する。これを決めるための数字。
- **ストリート遷移** — RFID から見たストリートが変わった時刻と、その間隔。

使い方:

    python tools/analyze_table_state.py                 # 最新セッション
    python tools/analyze_table_state.py --session 2026-09-12_140022_session1
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

# リポジトリ直下を import path に入れる（他の tools/ と同じ規約）。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))
    return ordered[idx]


def _fmt(values: list[float], unit: str = "秒") -> str:
    if not values:
        return "（データなし）"
    return (f"n={len(values)}  最小 {min(values):.1f}{unit}  中央 {_pct(values, 0.5):.1f}{unit}  "
            f"p95 {_pct(values, 0.95):.1f}{unit}  最大 {max(values):.1f}{unit}")


def load_history(path: Path) -> list[dict]:
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue          # 書き込み途中の末尾行は捨てる
    return rows


def find_history(log_dir: Path, session: Optional[str]) -> Optional[Path]:
    if session:
        p = log_dir / f"{session}.table_state.jsonl"
        return p if p.exists() else None
    candidates = sorted(
        log_dir.glob("*.table_state.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    return candidates[0] if candidates else None


def _epoch(row: dict) -> Optional[float]:
    try:
        return datetime.fromisoformat(row["updated_at"]).timestamp()
    except (KeyError, ValueError):
        return None


def analyze(rows: list[dict]) -> dict:
    """履歴から遅延・不在時間・ストリート遷移を集計する。"""
    lags: list[float] = []
    returned: list[float] = []      # 戻ってきた不在（= 持ち上げて見ただけ）
    final: list[float] = []         # 戻らなかった不在（= マック / ハンド終了）
    streets: list[tuple[float, int, str]] = []
    hands: set = set()

    absent_since: dict[tuple[int, int], float] = {}   # (hand_id, seat) → 不在になった時刻
    prev_street: Optional[tuple[int, str]] = None

    for row in rows:
        t = _epoch(row)
        if t is None:
            continue
        hand_id = row.get("hand_id")
        hands.add(hand_id)

        observed = row.get("observed_at")
        if observed is not None:
            lags.append(max(0.0, t - observed))

        street = row.get("rfid_street", "")
        if prev_street != (hand_id, street):
            streets.append((t, hand_id, street))
            prev_street = (hand_id, street)

        for seat in row.get("seats", []):
            key = (hand_id, seat["seat"])
            if not seat.get("dealt_in"):
                continue
            if seat.get("present"):
                start = absent_since.pop(key, None)
                if start is not None:
                    returned.append(t - start)       # 戻ってきた
            else:
                absent_since.setdefault(key, t)

    # 最後まで戻らなかったぶん（履歴の末尾時刻までを不在とみなす）
    end = _epoch(rows[-1]) if rows else None
    if end is not None:
        final.extend(end - start for start in absent_since.values())

    return {
        "rows": len(rows),
        "hands": sorted(h for h in hands if h is not None),
        "lags": lags,
        "returned": returned,
        "final": final,
        "streets": streets,
    }


def report(result: dict, fold_hint_sec: float) -> str:
    lines = [
        f"履歴 {result['rows']} 行 / ハンド {result['hands'] or '—'}",
        "",
        "■ 反映遅延（RFID 観測 → 卓状態の書き出し）",
        f"  {_fmt(result['lags'])}",
        "  ※ UI の表示遅延はこれ + モニタのポーリング間隔（1 秒）",
        "",
        "■ 席の不在時間",
        f"  戻ってきた（持ち上げて見ただけ）: {_fmt(result['returned'])}",
        f"  戻らなかった（マック / ハンド終了）: {_fmt(result['final'])}",
    ]
    returned = result["returned"]
    if returned:
        worst = max(returned)
        if worst >= fold_hint_sec:
            lines.append(
                f"  ⚠ 戻ってきた不在の最大 {worst:.1f} 秒が しきい値 {fold_hint_sec:.0f} 秒 以上です"
                " — このままだと持ち上げただけで「fold らしい」と表示されます。"
                f" しきい値を {worst * 1.5:.0f} 秒以上に上げてください"
            )
        else:
            lines.append(
                f"  ✓ 戻ってきた不在の最大 {worst:.1f} 秒 < しきい値 {fold_hint_sec:.0f} 秒"
                "（誤検出なし）"
            )
    lines += ["", "■ ストリート遷移（RFID 由来）"]
    prev_t = None
    for t, hand_id, street in result["streets"]:
        gap = f"  (+{t - prev_t:.1f}s)" if prev_t is not None else ""
        lines.append(
            f"  {datetime.fromtimestamp(t).strftime('%H:%M:%S')}  ハンド {hand_id}  {street}{gap}"
        )
        prev_t = t
    if not result["streets"]:
        lines.append("  （データなし）")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    from core.table_state import DEFAULT_FOLD_HINT_SEC

    ap = argparse.ArgumentParser(description="卓状態の履歴から遅延・不在時間・ストリート遷移を集計")
    ap.add_argument("--log-dir", default="./logs")
    ap.add_argument("--session", help="セッション ID（省略時は最新）")
    ap.add_argument("--fold-hint-sec", type=float, default=DEFAULT_FOLD_HINT_SEC,
                    help="「fold らしい」と表示するしきい値（既定の妥当性を判定する）")
    args = ap.parse_args(argv)

    path = find_history(Path(args.log_dir), args.session)
    if path is None:
        print(f"{args.log_dir} に卓状態の履歴（*.table_state.jsonl）がありません。")
        return 1
    print(f"== {path.name} ==")
    print(report(analyze(load_history(path)), args.fold_hint_sec))
    return 0


if __name__ == "__main__":
    sys.exit(main())
