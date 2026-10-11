"""core/store_lock.py — 会計の書き手を 1 プロセスに限るロック（ADR-0065）。

会計（`ledger.json`）と注文（`order_requests.json`）は、メモリの内容をファイルごと書き換える。2 つのプロセスが
同時に書くと、あとから書いた方が相手の変更を消す（単一書き手, ADR-0018/0020）。書き手になり得るのは
店舗サーバー（`main.py --serve`, 自動起動）と会計画面（`main.py --ledger`）の 2 つなので、どちらも起動時に
このロックを取り、取れなければ起動しない。

ロックは OS のファイルロック（Windows = `msvcrt.locking`、それ以外 = `fcntl.flock`）で、プロセスが落ちれば
OS が外す（古いロックファイルが残っても次の起動を妨げない）。誰が持っているかは同じファイルの先頭に書く
（Windows はロックした範囲を他のプロセスが読めないので、ロックは先頭から離れた 1 バイトにかける）。
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import BinaryIO, Optional

DEFAULT_LOCK_PATH = Path(__file__).resolve().parent.parent / "store_writer.lock"

# 持ち主の情報（JSON 1 行）より後ろの 1 バイトだけをロックする。
_LOCK_OFFSET = 4096


class StoreWriterLock:
    """会計の書き手のロック。`acquire()` が True なら、このプロセスが唯一の書き手。"""

    def __init__(self, path: Path | str = DEFAULT_LOCK_PATH) -> None:
        self.path = Path(path)
        self._fh: Optional[BinaryIO] = None

    def acquire(self, mode: str) -> bool:
        """ロックを取る（待たない）。取れたら持ち主の情報（`mode` = serve / ledger と pid）を書く。"""
        if self._fh is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")
        try:
            _lock(fh)
        except OSError:
            fh.close()
            return False
        fh.seek(0)
        fh.truncate()
        fh.write((json.dumps({
            "mode": mode, "pid": os.getpid(),
            "since": datetime.now().isoformat(timespec="seconds"),
        }, ensure_ascii=False) + "\n").encode("utf-8"))
        fh.flush()
        self._fh = fh
        return True

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            _unlock(self._fh)
        finally:
            self._fh.close()
            self._fh = None

    def holder(self) -> Optional[dict]:
        """いまの持ち主の情報（無い・読めなければ None）。ロックを持っていなくても読める。"""
        try:
            first = self.path.read_text(encoding="utf-8").splitlines()[0]
            info = json.loads(first)
        except (OSError, IndexError, ValueError):
            return None
        return info if isinstance(info, dict) else None

    def __enter__(self) -> "StoreWriterLock":
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def describe_holder(info: Optional[dict]) -> str:
    """持ち主を人が読める言葉にする（起動を断るときの案内に使う）。"""
    if not info:
        return "別のプロセス"
    name = {"serve": "店舗サーバー（自動起動）", "ledger": "会計画面"}.get(str(info.get("mode")), "別のプロセス")
    since = info.get("since")
    return f"{name}（pid {info.get('pid')}{f', {since} から' if since else ''}）"


if sys.platform == "win32":
    import msvcrt

    def _lock(fh: BinaryIO) -> None:
        fh.seek(_LOCK_OFFSET)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(fh: BinaryIO) -> None:
        fh.seek(_LOCK_OFFSET)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock(fh: BinaryIO) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fh: BinaryIO) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
