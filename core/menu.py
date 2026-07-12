"""core/menu.py

Phase M5 (ADR-0018) + staff 編集 (ADR-0046): 注文メニューマスタ。

`menu.json`（{"items": [{"item_name", "unit_amount", "sold_out"?}]}）のロード・検索・編集。
価格の最終決定権はスタッフ確定時にある（ここは prefill 用の参考値）。

- **thread-safe**: `--ledger` プロセスでは GUI スレッドと API スレッドが同居するため
  公開メソッドを RLock で保護する。
- **reload-on-read**: 別プロセス（単独 `--viewer-api`）が staff 編集に追従できるよう、
  mtime が進んでいたら読み直す。
- **編集は全量置換**（`set_items`）+ atomic write（ADR-0046 D3）。
- ファイル不在・破損は空メニューとして継続する（従来どおり）。
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from core.atomic_io import atomic_write_json

logger = logging.getLogger(__name__)

_DEFAULT_MENU_FILE = Path(__file__).parent.parent / "menu.json"

_MAX_ITEM_NAME = 100


class MenuValidationError(Exception):
    """menu item の validation 違反（error code: invalid_menu）。"""


def _normalize_items(items: list[dict]) -> list[dict]:
    """set_items の入力を検証・正規化する（ADR-0046 D1）。

    - item_name: trim 後 1〜100 文字、重複不可。
    - unit_amount: 0 以上の整数（bool は不可）。
    - sold_out: bool（省略時 false。false は永続形に書かない = 既存ファイル互換）。
    """
    if not isinstance(items, list):
        raise MenuValidationError("items は list が必要です。")
    normalized: list[dict] = []
    seen: set[str] = set()
    for raw in items:
        if not isinstance(raw, dict):
            raise MenuValidationError(f"menu item は object が必要です: {raw!r}")
        name = str(raw.get("item_name", "")).strip()
        if not name or len(name) > _MAX_ITEM_NAME:
            raise MenuValidationError(
                f"item_name は 1〜{_MAX_ITEM_NAME} 文字が必要です: {raw.get('item_name')!r}"
            )
        if name in seen:
            raise MenuValidationError(f"item_name が重複しています: {name!r}")
        unit = raw.get("unit_amount")
        if not isinstance(unit, int) or isinstance(unit, bool) or unit < 0:
            raise MenuValidationError(
                f"unit_amount は 0 以上の整数が必要です: {unit!r}（{name}）"
            )
        sold_out = raw.get("sold_out", False)
        if not isinstance(sold_out, bool):
            raise MenuValidationError(f"sold_out は bool が必要です: {sold_out!r}（{name}）")
        seen.add(name)
        item: dict = {"item_name": name, "unit_amount": unit}
        if sold_out:
            item["sold_out"] = True
        normalized.append(item)
    return normalized


class MenuMaster:
    """menu.json のロード・検索・編集（thread-safe / reload-on-read, ADR-0046）。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path is not None else _DEFAULT_MENU_FILE
        self._lock = threading.RLock()
        self._items: list[dict] = []
        self._loaded_mtime: float | None = None
        self._load()

    @property
    def path(self) -> Path:
        return self._path

    def _load(self) -> None:
        self._items = []
        if not self._path.exists():
            logger.warning("Menu file not found: %s (注文メニューは空になります)", self._path)
            self._loaded_mtime = None
            return
        try:
            mtime = self._path.stat().st_mtime
            with self._path.open(encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Could not load menu file (%s), starting empty.", e)
            return
        for raw in data.get("items", []):
            name = str(raw.get("item_name", "")).strip()
            unit = raw.get("unit_amount")
            if not name or not isinstance(unit, int) or unit < 0:
                logger.warning("Skipping malformed menu item: %r", raw)
                continue
            item: dict = {"item_name": name, "unit_amount": unit}
            if raw.get("sold_out") is True:
                item["sold_out"] = True
            self._items.append(item)
        self._loaded_mtime = mtime

    def _maybe_reload(self) -> None:
        """別プロセスの write に追従する（mtime が進んでいたら読み直す）。"""
        try:
            mtime = self._path.stat().st_mtime if self._path.exists() else None
        except OSError:
            return
        if mtime != self._loaded_mtime:
            self._load()

    def list_items(self) -> list[dict]:
        with self._lock:
            self._maybe_reload()
            return [dict(item) for item in self._items]

    def unit_amount(self, item_name: str) -> int | None:
        """品名の単価を返す（menu に無ければ None。sold_out でも単価は返す）。"""
        with self._lock:
            self._maybe_reload()
            for item in self._items:
                if item["item_name"] == item_name:
                    return item["unit_amount"]
            return None

    def is_sold_out(self, item_name: str) -> bool:
        """品切れか（menu に無い品名は False — 存在チェックは unit_amount が担う）。"""
        with self._lock:
            self._maybe_reload()
            for item in self._items:
                if item["item_name"] == item_name:
                    return bool(item.get("sold_out"))
            return False

    def set_items(self, items: list[dict]) -> list[dict]:
        """menu 全量を置換して永続化する（staff 編集, ADR-0046 D3）。

        validation 違反は MenuValidationError（invalid_menu）。書き込みは atomic。
        """
        normalized = _normalize_items(items)
        with self._lock:
            atomic_write_json(self._path, {"items": normalized})
            self._items = normalized
            try:
                self._loaded_mtime = self._path.stat().st_mtime
            except OSError:
                self._loaded_mtime = None
            logger.info("Menu updated: %d items (%s)", len(normalized), self._path)
            return [dict(item) for item in normalized]
