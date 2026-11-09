"""core/audio_retention.py — 発話の音声を 90 日で消す（ADR-0072, 2026-11-09）

店舗 PC は発話の音声を `logs/audio/<sid>/` に残す（`audio.save_audio`。書き起こしの送り直し・聞き直し・評価のため）。
卓の会話も入るので、紙で知らせているとおり 90 日（設定 `audio.keep_days`）より古いセッションの音声フォルダを消す。
書き起こしの文・記録・推定は消さない。店舗サーバー（`--serve`）が 1 日 1 回見回る。
"""
from __future__ import annotations

import logging
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

DEFAULT_KEEP_DAYS = 90
CHECK_EVERY_SEC = 24 * 3600.0


def keep_days(cfg: dict) -> int:
    """設定 `audio.keep_days`（既定 90）。0 以下 = 消さない。"""
    try:
        return int((cfg.get("audio") or {}).get("keep_days", DEFAULT_KEEP_DAYS))
    except (TypeError, ValueError):
        return DEFAULT_KEEP_DAYS


def _recorded_at(folder: Path) -> Optional[float]:
    """セッションの音声を録った時刻: フォルダの中でいちばん新しいファイル（無ければフォルダ自身）。"""
    try:
        times = [p.stat().st_mtime for p in folder.iterdir() if p.is_file()]
        return max(times) if times else folder.stat().st_mtime
    except OSError:
        return None


def prune_audio(log_dir: Path, days: int, clock: Callable[[], float] = time.time) -> list[str]:
    """`days` 日より古いセッションの音声フォルダを消す。消したセッションの ID。"""
    if days <= 0:
        return []
    root = Path(log_dir) / "audio"
    if not root.is_dir():
        return []
    cutoff = clock() - days * 86400.0
    removed = []
    for folder in sorted(p for p in root.iterdir() if p.is_dir()):
        at = _recorded_at(folder)
        if at is None or at >= cutoff:
            continue
        try:
            shutil.rmtree(folder)
        except OSError:
            logger.warning("古い音声を消せませんでした: %s", folder, exc_info=True)
            continue
        removed.append(folder.name)
        logger.info("%d 日より古い音声を消しました: %s（%s に録音）", days, folder.name,
                    datetime.fromtimestamp(at).date().isoformat())
    return removed


def run_forever(log_dir: Path, days: int, stop: threading.Event, every: float = CHECK_EVERY_SEC) -> None:
    """起動したときと、そのあと 1 日 1 回。"""
    while True:
        prune_audio(log_dir, days)
        if stop.wait(every):
            return
