"""integration/control_consumer.py

hand logger プロセス側の control-command queue consumer（ADR-0039）。

`{log_dir}/{session_id}.control.jsonl` を tail し、新規コマンドを `AudioEvent` に翻訳して
`audio_queue` に積む。適用は従来の IntegrationThread が行う（状態変更経路は増やさない,
ISSUE-0012）。既定 off（`hand_control.enabled`）なので、起動されない限り挙動は不変。

設計:
  - 起動時に **ファイル末尾へシーク**（過去コマンドを再実行しない）。
  - poll ごとに完全な新規行のみ読み、`command_id` で重複排除して audio_queue へ。
  - clock は注入（テスト容易性）。stop_event で停止。
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Callable, Optional

from core.control_queue import ControlCommandLog, command_to_audio_event

logger = logging.getLogger(__name__)


class ControlConsumerThread(threading.Thread):
    """control queue を tail して audio_queue に AudioEvent を積む daemon スレッド。"""

    def __init__(
        self,
        control_log: ControlCommandLog,
        audio_queue: queue.Queue,
        stop_event: threading.Event,
        poll_interval_ms: int = 200,
        clock: Callable[[], float] = time.time,
        start_offset: Optional[int] = None,
    ) -> None:
        super().__init__(daemon=True, name="ControlConsumerThread")
        self._log = control_log
        self._audio_queue = audio_queue
        self._stop_event = stop_event
        self._poll_s = max(0.02, poll_interval_ms / 1000.0)
        self._clock = clock
        # 既定では末尾から（過去コマンドを再生しない）。テストは 0 を渡せる。
        self._offset = control_log.end_offset() if start_offset is None else start_offset
        self._seen: set[str] = set()

    def poll_once(self) -> int:
        """新規コマンドを 1 回分処理し、積んだ件数を返す（テスト用に分離）。"""
        commands, new_offset = self._log.read_from(self._offset)
        self._offset = new_offset
        n = 0
        for command in commands:
            if command.command_id in self._seen:
                continue
            self._seen.add(command.command_id)
            event = command_to_audio_event(command, self._clock)
            if event is not None:
                self._audio_queue.put(event)
                n += 1
                logger.info("control → audio_queue: %s %s", command.type, command.args)
        return n

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.poll_once()
            except Exception:  # noqa: BLE001 — consumer は録音を巻き込まず生き残る
                logger.exception("control consumer poll failed")
            self._stop_event.wait(self._poll_s)
