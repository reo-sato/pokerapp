"""scripts/sync_shared_ui.py

共有 RN UI ソース（正本 `shared/`）を mobile / staff 両アプリへコピーする（ADR-0044 D1）。

両アプリ（`mobile/` / `staff/`）は独立した npm プロジェクトのため、monorepo 化せずに
「正本 → バイト同一コピー」で単一ソース性を保つ。drift は `tests/test_shared_ui_sync.py`
（pytest = CI）が検知する。

使い方:
    python scripts/sync_shared_ui.py            # 正本 → 両アプリへコピー
    python scripts/sync_shared_ui.py --check    # コピーのみ検証（差分があれば exit 1）
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# (正本ディレクトリ, コピー先ディレクトリ...) の対応。追加の共有 UI はここに足す。
SHARED_TARGETS: list[tuple[Path, list[Path]]] = [
    (
        REPO_ROOT / "shared" / "hand_replay",
        [
            REPO_ROOT / "mobile" / "src" / "shared" / "hand_replay",
            REPO_ROOT / "staff" / "src" / "shared" / "hand_replay",
        ],
    ),
]


def iter_source_files(source_dir: Path) -> list[Path]:
    """正本ディレクトリ内の配布対象ファイル（決定的な順序）。"""
    return sorted(p for p in source_dir.rglob("*") if p.is_file())


def check() -> list[str]:
    """正本とコピーの差分を返す（空 = 同期済み）。"""
    problems: list[str] = []
    for source_dir, dest_dirs in SHARED_TARGETS:
        files = iter_source_files(source_dir)
        if not files:
            problems.append(f"正本が空です: {source_dir}")
            continue
        for dest_dir in dest_dirs:
            for src in files:
                dest = dest_dir / src.relative_to(source_dir)
                if not dest.exists():
                    problems.append(f"未配布: {dest.relative_to(REPO_ROOT)}")
                elif dest.read_bytes() != src.read_bytes():
                    problems.append(f"drift: {dest.relative_to(REPO_ROOT)}")
            # コピー先にだけあるファイル（正本から消した後の残骸）も drift。
            if dest_dir.exists():
                for extra in sorted(p for p in dest_dir.rglob("*") if p.is_file()):
                    rel = extra.relative_to(dest_dir)
                    if not (source_dir / rel).exists():
                        problems.append(f"正本に無いコピー: {extra.relative_to(REPO_ROOT)}")
    return problems


def sync() -> None:
    for source_dir, dest_dirs in SHARED_TARGETS:
        for dest_dir in dest_dirs:
            if dest_dir.exists():
                shutil.rmtree(dest_dir)
            shutil.copytree(source_dir, dest_dir)
            print(f"synced: {source_dir.relative_to(REPO_ROOT)} -> "
                  f"{dest_dir.relative_to(REPO_ROOT)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="コピーせず検証のみ（差分があれば exit 1）")
    args = parser.parse_args()
    if args.check:
        problems = check()
        for p in problems:
            print(p, file=sys.stderr)
        return 1 if problems else 0
    sync()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
