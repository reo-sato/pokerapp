from __future__ import annotations

import json
import logging
from pathlib import Path

from core.atomic_io import atomic_write_json
from core.hand_log import HandSummary

logger = logging.getLogger(__name__)


class JsonWriter:
    """ハンドアクションとサマリーをセッション単位の JSON ファイルに追記保存する。

    ファイル形式:
        {
          "session_id": "...",
          "hands": [
            {
              ...HandSummary フィールド...,
              "actions": [ActionRecord, ...]
            }
          ]
        }

    ハンド完了ごとにディスクへ書き込む（NFR-10: セッション中断時のログ保全）。
    """

    def __init__(self, log_dir: str | Path, session_id: str) -> None:
        self._log_dir = Path(log_dir)
        self._session_id = session_id
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._path = self._log_dir / f"{session_id}.json"
        self._data: dict = {"session_id": session_id, "hands": []}

        # 既存ファイルがあれば読み込む（セッション再開対応）
        if self._path.exists():
            try:
                with self._path.open(encoding="utf-8") as f:
                    self._data = json.load(f)
                logger.info("Loaded existing session log: %s", self._path)
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Could not load existing log (%s), starting fresh.", e)
                self._data = {"session_id": session_id, "hands": []}

    def append_hand_summary(self, summary: HandSummary) -> None:
        """ハンドサマリー（ActionRecord を含む）をファイルに追記保存する。"""
        self._data["hands"].append(summary.to_dict())
        self._flush()
        logger.info("Hand %d saved to %s", summary.hand_id, self._path)

    def _flush(self) -> None:
        """データをディスクへ書き込む。書き込み失敗時もクラッシュしない。"""
        try:
            atomic_write_json(self._path, self._data)
        except OSError:
            logger.exception("Failed to write log file: %s", self._path)

    @property
    def path(self) -> Path:
        return self._path
