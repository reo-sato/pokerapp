from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from core.hand_log import ActionRecord, HandSummary

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
        tmp_path = self._path.with_suffix(".tmp")
        try:
            with tmp_path.open("w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            # アトミックなリネームで壊れたファイルを防ぐ
            os.replace(tmp_path, self._path)
        except OSError:
            logger.exception("Failed to write log file: %s", self._path)
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)

    @property
    def path(self) -> Path:
        return self._path
