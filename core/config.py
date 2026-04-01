from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG_PATH = Path(__file__).parent.parent / "config_default.json"
_CONFIG_PATH = Path(__file__).parent.parent / "config.json"


def load_config(path: Path | None = None) -> dict:
    """config.json を読み込む。存在しない場合は config_default.json をコピーして使用する。"""
    target = path or _CONFIG_PATH
    if not target.exists():
        logger.info("config.json not found, copying from config_default.json")
        shutil.copy(_DEFAULT_CONFIG_PATH, target)
    with target.open(encoding="utf-8") as f:
        return json.load(f)


def save_config(config: dict, path: Path | None = None) -> None:
    """config.json に書き込む。"""
    target = path or _CONFIG_PATH
    with target.open("w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    logger.info("Config saved to %s", target)
