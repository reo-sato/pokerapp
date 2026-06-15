"""core/atomic_io.py

アトミック + 耐障害な JSON 書き込み（B2 / v1.0 ローンチレビュー）。

temp ファイルへ書き込み → flush + fsync（電源断でも内容ロスしない）→ os.replace でアトミックに
差し替える。各 repository / writer の永続化が共通で使い、書き込み耐性を一元化する。

- fsync は **ファイル内容に対してのみ**行う（ディレクトリ fsync は Windows 非対応のため行わない）。
- 失敗時は中途半端な temp を掃除して OSError を再送出する（呼び出し側が except でログ + 継続する）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


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
        os.replace(tmp, p)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
