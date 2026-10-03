#!/usr/bin/env python3
"""tools/compare_versions.py — 前の版と今の版で同じログを推定し、ハンドごとの違いを出す（監査 3 回目の必須 7）

規則づくりに使っていない新しいセッションで、前の版（例: 事前登録の草稿のときの版）と今の版を比べ、悪くなった
ハンドが無いかを確かめる（10/01〜10/03 の変更すべてにとって初めての前向きの確認）。前の版は git の作業ツリー
（一時フォルダ）に取り出し、両方の版の `tools/bench_hands.py --json` を同じ入力で回して、ハンドごとに比べる。

    TZ=Asia/Tokyo python tools/compare_versions.py <前の版のコミット> logs_2026-10-04.zip
    TZ=Asia/Tokyo python tools/compare_versions.py <前の版のコミット>             # 開発データ（店舗 + 台本）
    TZ=Asia/Tokyo python tools/compare_versions.py --base-dir <前の版のフォルダ> logs.zip   # git の無いところ

比べるもの（出所ごと）: 推定の全部正しいハンドと読み直し（直しの無い再生 = いまの読みの規則）の全部正しいハンド、
正しくなった / 正しくなくなった（今の版で要確認が付いたか）ハンド、1 番の記録が変わったハンド、要確認が変わった
ハンド。店舗のログは店の時刻の文字列でハンドを合わせるので `TZ=Asia/Tokyo` で回す。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
_FINGERPRINT = ("from integration.estimator import params_hash; import integration.estimator as m; "
                "print(params_hash(), getattr(m, 'content_hash', lambda: '-')())")


def run_bench(tree: Path, logs: list[Path], workers: int) -> list[dict]:
    """`tree` の版の物差し（`bench_hands.py --json`）を回す。"""
    cmd = [sys.executable, str(tree / "tools" / "bench_hands.py"), "--json", "--workers", str(workers),
           *[str(p) for p in logs]]
    env = dict(os.environ, PYTHONPATH=str(tree))
    done = subprocess.run(cmd, cwd=tree, env=env, capture_output=True, text=True, encoding="utf-8")
    if done.returncode != 0:
        raise RuntimeError(f"{tree} の物差しが失敗しました:\n{done.stderr[-2000:]}")
    return json.loads(done.stdout)


def fingerprint(tree: Path) -> str:
    """その版の値の指紋・内容の指紋（内容の指紋の無い古い版は「-」）。"""
    env = dict(os.environ, PYTHONPATH=str(tree))
    done = subprocess.run([sys.executable, "-c", _FINGERPRINT], cwd=tree, env=env, capture_output=True, text=True,
                          encoding="utf-8")
    return done.stdout.strip() if done.returncode == 0 else "?"


def per_hand(source: dict) -> dict[str, dict]:
    """物差しの結果（出所 1 つ）をハンドごとに: 推定が正しいか・読み直しが正しいか・要確認・1 番の記録。"""
    correct = set(source.get("correct") or [])
    better = set(source.get("better") or [])
    worse = set(source.get("worse") or [])
    review = source.get("review_of") or {}
    out: dict[str, dict] = {}
    for key, best in (source.get("best") or {}).items():
        ok = key in correct
        out[key] = {"ok": ok, "base_ok": (ok and key not in better) or key in worse,
                    "review": review.get(key), "best": best}
    return out


def compare(before: list[dict], after: list[dict]) -> list[str]:
    """出所ごとに、前の版と今の版のハンドごとの違いを文にする。"""
    lines: list[str] = []
    old_by_name = {s.get("name"): s for s in before}
    for src in after:
        name = src.get("name")
        old = old_by_name.get(name)
        if old is None:
            lines.append(f"{name}: 前の版に無い出所")
            continue
        a, b = per_hand(old), per_hand(src)
        keys = [k for k in b if k in a]
        excluded = set(src.get("excluded") or {}) | set(old.get("excluded") or {})    # 数えないハンド（理由は物差し）
        skipped = sorted((set(a) ^ set(b)) & excluded)
        only = sorted((set(a) ^ set(b)) - excluded)
        n = len(keys)
        fixed = [k for k in keys if b[k]["ok"] and not a[k]["ok"]]
        broken = [k for k in keys if a[k]["ok"] and not b[k]["ok"]]
        silent = [k for k in broken if not b[k]["review"]]
        base_fixed = [k for k in keys if b[k]["base_ok"] and not a[k]["base_ok"]]
        base_broken = [k for k in keys if a[k]["base_ok"] and not b[k]["base_ok"]]
        moved = [k for k in keys if a[k]["best"] != b[k]["best"]]
        review = [k for k in keys if bool(a[k]["review"]) != bool(b[k]["review"])]
        lines.append(
            f"{name}: 推定の全部正しいハンド {sum(a[k]['ok'] for k in keys)}/{n} → {sum(b[k]['ok'] for k in keys)}/{n}"
            f"（正しくなった {len(fixed)}・正しくなくなった {len(broken)}、うち要確認なし {len(silent)}）"
            f" ／ 読み直し {sum(a[k]['base_ok'] for k in keys)}/{n} → {sum(b[k]['base_ok'] for k in keys)}/{n}"
            f"（正しくなった {len(base_fixed)}・正しくなくなった {len(base_broken)}）")
        for label, items in (("正しくなった", fixed), ("正しくなくなった", broken), ("要確認なしで正しくなくなった", silent),
                             ("読み直しが正しくなった", base_fixed), ("読み直しが正しくなくなった", base_broken),
                             ("1 番の記録が変わった", moved), ("要確認が変わった", review),
                             ("片方の版だけ数えないハンド（真のアクションの不備など）", skipped),
                             ("片方の版にしか無いハンド", only)):
            if items:
                lines.append(f"    {label}: {' '.join(items[:30])}{' …' if len(items) > 30 else ''}")
    return lines


def checkout(commit: str) -> tuple[Path, Path]:
    """`commit` を一時フォルダの作業ツリーに取り出す。返り値: (作業ツリー, 片付ける一時フォルダ)。"""
    tmp = Path(tempfile.mkdtemp(prefix="pokerapp_base_"))
    tree = tmp / "tree"
    subprocess.run(["git", "-C", str(ROOT), "worktree", "add", "--detach", str(tree), commit], check=True,
                   capture_output=True, text=True)
    return tree, tmp


def remove_checkout(tree: Path, tmp: Path) -> None:
    subprocess.run(["git", "-C", str(ROOT), "worktree", "remove", "--force", str(tree)], capture_output=True)
    shutil.rmtree(tmp, ignore_errors=True)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="前の版と今の版で同じログを推定し、ハンドごとの違いを出す")
    ap.add_argument("base", nargs="?", help="前の版のコミット（git の作業ツリーに取り出す）")
    ap.add_argument("logs", nargs="*", type=Path, help="店舗のログ（pack_logs の zip かフォルダ）。無ければ開発データ")
    ap.add_argument("--base-dir", type=Path, help="前の版を取り出したフォルダ（git の無いところ。コミットの代わり）")
    ap.add_argument("--workers", type=int, default=3, help="ハンドを並べて推定するプロセスの数（結果は同じ）")
    args = ap.parse_args(argv)
    logs = [p.resolve() for p in args.logs]
    if args.base_dir is not None:
        if args.base is not None:
            logs = [Path(args.base).resolve(), *logs]       # コミットを渡さないときは 1 つ目もログ
        tree, tmp = args.base_dir.resolve(), None
    elif args.base:
        tree, tmp = checkout(args.base)
    else:
        ap.error("前の版のコミットか --base-dir を渡してください")
        return 2
    try:
        print(f"前の版: {args.base or tree}（値の指紋・内容の指紋 {fingerprint(tree)}）")
        print(f"今の版: {ROOT}（{fingerprint(ROOT)}）")
        before = run_bench(tree, logs, args.workers)
        after = run_bench(ROOT, logs, args.workers)
    finally:
        if tmp is not None:
            remove_checkout(tree, tmp)
    print("\n".join(compare(before, after)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
