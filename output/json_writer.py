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

    def ensure_created(self) -> None:
        """記録ファイルがまだ無ければ、ハンド 0 のまま作る。

        セッションを始めた時点で真のアクション入力の画面（`logs/*.json` を一覧にする）に出すため。前は最初のハンドが
        終わるまで作らず、ロガーを起動しても画面にセッションが出なかった（店舗 2026-09-30）。
        """
        if not self._path.exists():
            self._flush()

    def append_hand_summary(self, summary: HandSummary) -> None:
        """ハンドサマリー（ActionRecord を含む）をファイルに追記保存する。"""
        self._data["hands"].append(summary.to_dict())
        self._flush()
        logger.info("Hand %d saved to %s", summary.hand_id, self._path)

    @property
    def live_path(self) -> Path:
        """進行中のハンド（`write_live_hand`）の置き場所 `logs/{session_id}.live_hand.json`。"""
        return self._log_dir / f"{self._session_id}.live_hand.json"

    def write_live_hand(self, hand: dict) -> None:
        """進行中のハンド（ここまでの記録）を書く。真のアクション入力の画面が、ハンドの途中で入力できるように
        フロップから出す（オーナー 2026-09-30）。ハンドの記録（`logs/{session_id}.json`）には入れない。"""
        try:
            atomic_write_json(self.live_path, {"session_id": self._session_id, "hand": hand})
        except OSError:
            logger.exception("Failed to write the hand in progress: %s", self.live_path)

    def clear_live_hand(self) -> None:
        """進行中のハンドを消す（ハンドが終わったとき）。"""
        try:
            self.live_path.unlink(missing_ok=True)
        except OSError:
            logger.exception("Failed to remove the hand in progress: %s", self.live_path)

    def _flush(self) -> None:
        """データをディスクへ書き込む。書き込み失敗時もクラッシュしない。"""
        try:
            atomic_write_json(self._path, self._data)
        except OSError:
            logger.exception("Failed to write log file: %s", self._path)

    @property
    def path(self) -> Path:
        return self._path
