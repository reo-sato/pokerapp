#!/usr/bin/env python3
"""tools/backup_data.py

データ永続ファイル（players/sessions/ledger/order_requests/credentials/auth_identity/rfid_cards）の
バックアップ CLI（B2 / v1.0 ローンチレビュー）。

店舗運用では cron（Linux/macOS）/ タスクスケジューラ（Windows）で日次実行を推奨。

使い方:
  python tools/backup_data.py                 # ./backups/<timestamp>/ にコピー、最新 30 世代を保持
  python tools/backup_data.py --dest D --keep N
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.backup import backup_data_files  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="poker データのバックアップ")
    ap.add_argument("--dest", default=str(Path(__file__).resolve().parent.parent / "backups"),
                    help="バックアップ先ディレクトリ（既定: ./backups）")
    ap.add_argument("--keep", type=int, default=30, help="保持する世代数（既定: 30）")
    args = ap.parse_args()

    target = backup_data_files(dest_dir=args.dest, keep=args.keep)
    if target is None:
        print("バックアップ対象のデータファイルが見つかりませんでした。")
        return 1
    print(f"バックアップを作成しました: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
