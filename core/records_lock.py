"""core/records_lock.py — `sessions.json` / `players.json` を書くプロセスをまたぐロック（ADR-0073 D5）。

ロガー（`--cli` / GUI）と店舗サーバー（`--serve`）は、どちらもこの 2 つのファイルを丸ごと書き換える。前はロガーが起動時に
読んだ内容に席の割当を足して書いていたので、プレー中に iPad で行った変更（公開範囲・別の卓・席の割当・お客さんの作成）が、
次のハンドで消えた。書き込みはこのロックのもとで「読み直し → 検査 → 変更 → 書く」にする。

- ロックは記録のファイルではなく、同じフォルダの別のファイル `records.lock` にかける（記録のファイルは `os.replace` で
  入れ替わる。Windows では開いたままのファイルを置き換えられない）。
- セッションとお客さんの記録で 1 つのロックを共有する（読み直しが連鎖するので、2 つに分けると取る順でデッドロックし得る）。
- 同じプロセスの中では入れ子で取れる（スレッドごとの深さ。OS のロックは一番外で 1 回だけ）。
- 待ちには上限がある（既定 5 秒）。取れなければ警告を出して、ロックなしで進める（前の動き。記録を止めない）。
- OS のロックなので、プロセスが落ちれば OS が外す（ファイルが残っても次を妨げない）。
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator, Optional

logger = logging.getLogger(__name__)

LOCK_NAME = "records.lock"
DEFAULT_TIMEOUT_SEC = 5.0
_POLL_SEC = 0.02


def lock_path_for(record_file: Path | str) -> Path:
    """記録のファイルと同じフォルダの `records.lock`。"""
    return Path(record_file).resolve().parent / LOCK_NAME


class _Entry:
    def __init__(self) -> None:
        self.thread_lock = threading.RLock()     # 同じプロセスのスレッドの順番
        self.depth = 0                           # thread_lock を持つスレッドの入れ子の深さ
        self.fh: Optional[BinaryIO] = None
        self.held = False


_entries: dict[str, _Entry] = {}
_entries_guard = threading.Lock()


def _entry(path: Path) -> _Entry:
    key = os.path.normcase(str(path))
    with _entries_guard:
        if key not in _entries:
            _entries[key] = _Entry()
        return _entries[key]


def _try_os_lock(path: Path, timeout: float) -> Optional[BinaryIO]:
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+b")
    deadline = time.monotonic() + timeout
    while True:
        try:
            _lock(fh)
            return fh
        except OSError:
            if time.monotonic() >= deadline:
                fh.close()
                return None
            time.sleep(_POLL_SEC)


@contextmanager
def records_lock(*record_files: Path | str, timeout: float = DEFAULT_TIMEOUT_SEC) -> Iterator[bool]:
    """記録のファイル（複数可）のフォルダのロックを取る。取れたら True、上限まで待って取れなければ False（進める）。

    フォルダが違えば、それぞれのロックを名前の順に取る（順番を揃えてデッドロックを防ぐ）。
    """
    paths = sorted({lock_path_for(f) for f in record_files}, key=lambda p: os.path.normcase(str(p)))
    entries = [(_entry(p), p) for p in paths]
    acquired: list[_Entry] = []
    ok = True
    try:
        for entry, path in entries:
            entry.thread_lock.acquire()
            acquired.append(entry)
            if entry.depth == 0:
                entry.fh = _try_os_lock(path, timeout)
                entry.held = entry.fh is not None
                if not entry.held:
                    logger.warning("記録のロック %s を %.0f 秒で取れません。ロックなしで書きます（別のプロセスが"
                                   "持ったまま止まっている？）", path, timeout)
            entry.depth += 1
            ok = ok and entry.held
        yield ok
    finally:
        for entry in reversed(acquired):
            entry.depth -= 1
            if entry.depth == 0 and entry.fh is not None:
                try:
                    _unlock(entry.fh)
                except OSError:
                    pass
                entry.fh.close()
                entry.fh = None
                entry.held = False
            entry.thread_lock.release()


if sys.platform == "win32":
    import msvcrt

    def _lock(fh: BinaryIO) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(fh: BinaryIO) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock(fh: BinaryIO) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fh: BinaryIO) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
