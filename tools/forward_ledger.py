#!/usr/bin/env python3
"""tools/forward_ledger.py — 前向きの成績表（テスト方針 2026-10-08）

開発データ（`tests/fixtures/store`）のセッションごとに、**そのセッションを初めて見たときの版**（見て直す前の版 =
`tests/fixtures/store/first_look.json`）で、いまの真のアクションを採点する。開発データの物差し（`bench_hands.py`）は
直しに使ったハンドで測るので標本内の数になる。新しいセッションでの成績はこの表で見る。

- 初めて見たときの版を git の作業ツリー（一時フォルダ）に取り出し、そのフォルダの開発データを**いまの** fixture
  （いまの真のアクション）に差し替えて、その版の `bench_hands.py --json --no-script` を回す（`compare_versions.py` と同じ）。
- 数えるハンドは今の版の物差しが決める（評価から外す・真のアクションの不備などの理由は今の版の規則と真のアクション）。
  初めて見たときの版がそのハンドを数えなかったら「初見で数えられない」に出し、合計から外す。
- 真のアクションの指紋（数えるハンドの真のアクションの sha256 の先頭 8 桁）を出す。真のアクションを直すと変わるので、
  点の変化が真のアクションの直しから来たか、コードから来たかを分けられる。

    TZ=Asia/Tokyo python tools/forward_ledger.py              # 表（markdown）
    TZ=Asia/Tokyo python tools/forward_ledger.py --json       # 行の JSON
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.compare_versions import checkout, fingerprint, per_hand, remove_checkout, run_bench  # noqa: E402

STORE = ROOT / "tests" / "fixtures" / "store"
FIRST_LOOK = STORE / "first_look.json"
SOURCE = "店舗（実卓）"


def load_first_look(path: Path = FIRST_LOOK) -> dict[str, dict]:
    """fixture のフォルダ名 → {commit, batch}（`_` で始まるキーは説明）。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if not k.startswith("_")}


def by_commit(first_look: dict[str, dict]) -> dict[str, list[str]]:
    """初めて見たときの版 → フォルダ（版の無いセッションは前向きに数えない）。"""
    out: dict[str, list[str]] = {}
    for folder, entry in first_look.items():
        if entry.get("commit"):
            out.setdefault(entry["commit"], []).append(folder)
    return out


def replace_fixtures(tree: Path, folders: list[str], store: Path = STORE) -> None:
    """取り出した版の開発データを、いまの fixture（`folders` だけ）に差し替える。"""
    target = tree / "tests" / "fixtures" / "store"
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True)
    for name in folders:
        shutil.copytree(store / name, target / name)


def _session(folder: Path) -> tuple[str, dict]:
    data = json.loads((folder / "expected.json").read_text(encoding="utf-8"))
    return str(data["session_id"])[:8], data


def gt_fingerprint(expected: dict, hand_ids: list[int]) -> str:
    """数えるハンドの真のアクションの指紋（物差しの前の記録と同じ式）。"""
    from tools.bench_hands import gt_fingerprint as fingerprint_of

    return fingerprint_of([h for h in expected.get("hands") or [] if h["hand_id"] in set(hand_ids)])


def _source(results: list[dict]) -> dict:
    return next((s for s in results if s.get("name") == SOURCE), {})


def ledger_rows(first_look: dict[str, dict], now: list[dict], first: dict[str, list[dict]],
                store: Path = STORE) -> list[dict]:
    """セッションごとの行。`now` = 今の版の物差し、`first` = 初めて見たときの版 → その版の物差し。"""
    cur = per_hand(_source(now))
    rows = []
    for folder, entry in first_look.items():
        if not (store / folder / "expected.json").exists():
            continue
        sid8, expected = _session(store / folder)
        keys = sorted((k for k in cur if k.split("#")[0] == sid8), key=lambda k: int(k.split("#")[1]))
        old = per_hand(_source(first.get(entry.get("commit") or "", [])))
        both = [k for k in keys if k in old]
        row = {
            "batch": entry.get("batch"), "folder": folder, "session": sid8, "commit": entry.get("commit"),
            "seats": len((expected.get("setup") or {}).get("players") or []),
            "hands": len(keys),
            "gt": gt_fingerprint(expected, [int(k.split("#")[1]) for k in keys]),
            "now_base": sum(cur[k]["base_ok"] for k in keys),
            "now_est": sum(cur[k]["ok"] for k in keys),
            "now_unflagged": sum(1 for k in keys if not cur[k]["ok"] and not cur[k]["review"]),
        }
        if entry.get("commit"):
            row.update({
                "first_hands": len(both),
                "first_missing": [k for k in keys if k not in old],
                "first_base": sum(old[k]["base_ok"] for k in both),
                "first_est": sum(old[k]["ok"] for k in both),
                "first_unflagged": sum(1 for k in both if not old[k]["ok"] and not old[k]["review"]),
            })
        rows.append(row)
    return rows


def _pct(a: int, b: int) -> str:
    return f"{a}/{b}（{a / b:.0%}）" if b else "-"


def format_table(rows: list[dict]) -> list[str]:
    """markdown の表（前向きのセッション）と合計・標本内だけのセッション。"""
    lines = ["| 店舗ログ | セッション | 席 | 初めて見た版 | 数える | 初見 読み直し | 初見 推定 | 初見 要確認なしの誤り"
             " | 今の版 推定 | 真のアクションの指紋 |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    fwd = [r for r in rows if r.get("commit")]
    for r in fwd:
        missing = f"（初見で数えられない {len(r['first_missing'])}）" if r["first_missing"] else ""
        lines.append(f"| {r['batch']} | {r['session']} | {r['seats']} | {r['commit']} | {r['first_hands']}{missing}"
                     f" | {r['first_base']} | {r['first_est']} | {r['first_unflagged']} | {r['now_est']}/{r['hands']}"
                     f" | {r['gt']} |")
    n = sum(r["first_hands"] for r in fwd)
    lines.append(f"| **合計** | {len(fwd)} セッション | | | {n} | {_pct(sum(r['first_base'] for r in fwd), n)}"
                 f" | **{_pct(sum(r['first_est'] for r in fwd), n)}** | {sum(r['first_unflagged'] for r in fwd)}"
                 f" | {_pct(sum(r['now_est'] for r in fwd), sum(r['hands'] for r in fwd))} | |")
    inside = [r for r in rows if not r.get("commit")]
    if inside:
        hands = sum(r["hands"] for r in inside)
        lines.append("")
        lines.append(f"標本内だけ（推定器 v1 より前に入れたセッション {len(inside)} 個）: 今の版 推定 "
                     f"{_pct(sum(r['now_est'] for r in inside), hands)}")
    return lines


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="前向きの成績表: 各セッションを初めて見たときの版で、いまの真のアクションを採点する")
    ap.add_argument("--workers", type=int, default=3, help="ハンドを並べて推定するプロセスの数（結果は同じ）")
    ap.add_argument("--json", action="store_true", help="行を JSON で出す")
    args = ap.parse_args(argv)
    first_look = load_first_look()
    now = run_bench(ROOT, [], args.workers, ("--no-script",))
    first: dict[str, list[dict]] = {}
    prints = {}
    for commit, folders in by_commit(first_look).items():
        tree, tmp = checkout(commit)
        try:
            replace_fixtures(tree, folders)
            prints[commit] = fingerprint(tree)
            first[commit] = run_bench(tree, [], args.workers, ("--no-script",))
        finally:
            remove_checkout(tree, tmp)
    rows = ledger_rows(first_look, now, first)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return 0
    print(f"今の版: 値の指紋・内容の指紋 {fingerprint(ROOT)}")
    for commit, fp in prints.items():
        print(f"初めて見た版 {commit}: {fp}")
    print("\n".join(format_table(rows)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
