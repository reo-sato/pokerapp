"""core/menu.py

Phase M5 (ADR-0015): 注文メニューマスタ。

`menu.json`（{"items": [{"item_name", "unit_amount"}]}）をロードする読み取り専用マスタ。
rfid_cards.json と同じ「コミット済みサンプルを店側で編集」運用。価格の最終決定権は
スタッフ確定時にある（ここは prefill 用の参考値）。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_MENU_FILE = Path(__file__).parent.parent / "menu.json"


class MenuMaster:
    """menu.json のロードと検索。ファイル不在・破損は空メニューとして継続する。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path is not None else _DEFAULT_MENU_FILE
        self._items: list[dict] = []
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            logger.warning("Menu file not found: %s (注文メニューは空になります)", self._path)
            return
        try:
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
            self._items.append({"item_name": name, "unit_amount": unit})

    def list_items(self) -> list[dict]:
        return [dict(item) for item in self._items]

    def unit_amount(self, item_name: str) -> int | None:
        """品名の単価を返す（menu に無ければ None）。"""
        for item in self._items:
            if item["item_name"] == item_name:
                return item["unit_amount"]
        return None
