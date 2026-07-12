"""tests/test_shared_ui_sync.py

ADR-0044 D1: 共有 RN UI（正本 `shared/`）と mobile / staff 内コピーのバイト同一性を検証する。

drift（正本だけ編集してコピー忘れ / コピー先を直接編集）を CI で落とす。
正規手順: 正本を編集 → `python scripts/sync_shared_ui.py`。
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from sync_shared_ui import SHARED_TARGETS, check, iter_source_files  # noqa: E402


def test_shared_ui_sources_exist():
    for source_dir, dest_dirs in SHARED_TARGETS:
        assert source_dir.is_dir(), f"正本ディレクトリがありません: {source_dir}"
        assert iter_source_files(source_dir), f"正本が空です: {source_dir}"
        for dest_dir in dest_dirs:
            assert dest_dir.is_dir(), (
                f"コピー先がありません: {dest_dir}（python scripts/sync_shared_ui.py を実行）"
            )


def test_shared_ui_copies_are_in_sync():
    problems = check()
    assert problems == [], (
        "共有 UI が drift しています（正本 shared/ を編集して "
        "`python scripts/sync_shared_ui.py` で再配布してください）:\n" + "\n".join(problems)
    )
