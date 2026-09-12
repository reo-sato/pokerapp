"""core/backup.py

データ永続ファイルのバックアップ（B2 / v1.0 ローンチレビュー）。

会計の source of truth は単一 JSON 群（`ledger.json` / `sessions.json` / `players.json` 等）で、
ディスク事故・誤削除・部分破損で復旧不能になるのが最大の運用リスク。本モジュールは存在する
データファイルをタイムスタンプ付きディレクトリへコピーし、世代を keep 件まで剪定する純関数を提供する
（`tools/backup_data.py` の CLI と `main.py` の起動時バックアップが利用）。
"""
from __future__ import annotations

import logging
import shutil
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# バックアップ対象（プロジェクト直下の永続データ。存在するものだけコピーする）。
_ROOT = Path(__file__).parent.parent
DEFAULT_BACKUP_SOURCES: list[Path] = [
    _ROOT / "players.json",
    _ROOT / "sessions.json",
    _ROOT / "ledger.json",
    _ROOT / "order_requests.json",
    _ROOT / "player_credentials.json",
    _ROOT / "auth_identity.json",
    _ROOT / "rfid_cards.json",
]


def backup_data_files(
    sources: "list[Path] | None" = None,
    dest_dir: "str | Path" = _ROOT / "backups",
    *,
    keep: int = 30,
    now: "datetime | None" = None,
) -> "Path | None":
    """存在する `sources` を `dest_dir/<YYYYmmdd-HHMMSS>/` にコピーし、古い世代を keep 件に剪定する。

    コピー対象が 1 つも無ければ何もせず None を返す。失敗してもクラッシュさせない（best-effort、
    会計運用を止めない）。返り値はバックアップ先ディレクトリ。
    """
    src_list = sources if sources is not None else DEFAULT_BACKUP_SOURCES
    existing = [Path(p) for p in src_list if Path(p).is_file()]
    if not existing:
        return None

    dest_root = Path(dest_dir)
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    target = dest_root / stamp
    try:
        target.mkdir(parents=True, exist_ok=True)
        for src in existing:
            shutil.copy2(src, target / src.name)
    except OSError:
        logger.exception("Backup failed (dest=%s)", target)
        return None

    _prune(dest_root, keep)
    logger.info("Backed up %d file(s) to %s", len(existing), target)
    return target


def _prune(dest_root: Path, keep: int) -> None:
    """`dest_root` 配下のタイムスタンプ世代を新しい順に keep 件残して削除する。"""
    if keep <= 0:
        return
    try:
        gens = sorted(
            (p for p in dest_root.iterdir() if p.is_dir()),
            key=lambda p: p.name,
            reverse=True,
        )
    except OSError:
        return
    for old in gens[keep:]:
        shutil.rmtree(old, ignore_errors=True)
