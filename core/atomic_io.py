"""core/atomic_io.py

アトミック + 耐障害な JSON 書き込み（B2 / v1.0 ローンチレビュー）。

temp ファイルへ書き込み → flush + fsync（電源断でも内容ロスしない）→ os.replace でアトミックに
差し替える。各 repository / writer の永続化が共通で使い、書き込み耐性を一元化する。

- fsync は **ファイル内容に対してのみ**行う（ディレクトリ fsync は Windows 非対応のため行わない）。
- 失敗時は中途半端な temp を掃除して OSError を再送出する（呼び出し側が except でログ + 継続する）。
- Windows では別プロセスが読んでいる間はファイルを置き換えられず（共有違反）、置き換えの瞬間は
  読めないことがある。hand logger と viewer API が同じファイルを使うので、どちらも短く再試行する。
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Windows の共有違反（別プロセスが開いている）を待つ回数と間隔。合計 0.5 秒。
_RETRIES = 10
_RETRY_SEC = 0.05


def file_stat(path: "str | Path") -> "tuple[int, int] | None":
    """更新判定用の (更新時刻 ns, サイズ)。無い・読めないなら None。"""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def read_json_file(path: "str | Path", *, quarantine: bool = True) -> "dict | None":
    """JSON を読む（B7）。存在しない → None。

    **破損（JSONDecodeError）時は、そのファイルを脇に退避（quarantine）してから None を返す**
    （次の書き込みで唯一のコピーを上書き消失させないため。B2 バックアップと併せて手復旧可能にする）。
    退避先は `<name>.corrupt-<timestamp>`。OSError（一時的 IO 等）は短く再試行し、それでも
    読めなければ退避せず None。
    """
    p = Path(path)
    for attempt in range(_RETRIES):
        if not p.exists():
            return None
        try:
            with p.open(encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            if quarantine:
                _quarantine(p)
            logger.error("Corrupt JSON could not be parsed: %s（空で起動します。手復旧してください）", p)
            return None
        except OSError as e:
            if attempt == _RETRIES - 1:
                logger.warning("Could not read %s (%s)", p, e)
                return None
            time.sleep(_RETRY_SEC)
    return None


def _quarantine(p: Path) -> None:
    try:
        dest = p.with_name(f"{p.name}.corrupt-{datetime.now():%Y%m%d-%H%M%S}")
        os.replace(p, dest)
        logger.error("破損ファイルを退避しました: %s → %s", p, dest)
    except OSError:
        logger.exception("Failed to quarantine corrupt file %s", p)


def atomic_write_json(path: "str | Path", data: Any, *, indent: int = 2) -> None:
    """`data` を `path` に atomic + fsync で書き込む。失敗時は OSError を送出。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=indent)
            f.flush()
            os.fsync(f.fileno())
        _replace_with_retry(tmp, p)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _replace_with_retry(src: Path, dst: Path) -> None:
    for attempt in range(_RETRIES):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == _RETRIES - 1:
                raise
            time.sleep(_RETRY_SEC)
