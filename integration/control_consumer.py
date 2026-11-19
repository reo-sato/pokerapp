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
from typing import Any, Callable, Optional

from core.control_queue import ControlCommand, ControlCommandLog, command_to_audio_event

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
        backlog: Optional[Callable[[], int]] = None,
        backlog_wait_sec: float = 60.0,
        pending_before: Optional[Callable[[float], int]] = None,
        commands: Optional[Any] = None,
        on_result: Optional[Callable[[ControlCommand, Any], None]] = None,
        on_stop: Optional[Callable[[ControlCommand], None]] = None,
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
        # 聞き取り待ちの発話の数（`AudioThread.backlog`）。0 になるまで待ってからコマンドを積む（先に言われた発話を
        # 追い越さない = CLI の入力と同じ。台本のハンドの開始が前のハンドの最後のアクションより先に入らないように）
        self._backlog = backlog
        self._backlog_wait_sec = backlog_wait_sec
        # コマンドを作った時刻より前に話し始めた発話の数（`AudioThread.pending_before`）。あればこちらで待つ
        self._pending_before = pending_before
        # 端末と共通の操作（`integration/logger_commands.LoggerCommands`, ADR-0073 D4）。あれば、行の形がある制御は
        # 同じ意味の端末の 1 行に直して同じ処理を通す（同じ記録になる・副作用のある操作も届く）。結果は `on_result` へ
        self._commands = commands
        self._on_result = on_result
        # iPad の「終了」（stop, ADR-0073 D6）。発話の聞き取り待ちを通さずにすぐ渡す（先の操作の後ろに並ばせない）
        self._on_stop = on_stop

    def _wait_for_speech(self, since: Optional[float]) -> None:
        """コマンドより前に話し始めた発話（`since` より前）の聞き取りを待つ。

        あとから話し始めた発話（台本の次のハンドの行）は待たない。聞き取りが話す速さに追いつかず遅れていても、
        先の発話が減っていく（聞き取りが進む）あいだは待つ。`backlog_wait_sec` 秒進まなければ先に積む（認識が
        止まっても制御は届く）。`pending_before` が無ければ、認識待ちが 0 になるまで（最大 `backlog_wait_sec` 秒）。
        """
        if self._pending_before is None or since is None:
            if self._backlog is None:
                return
            deadline = time.monotonic() + self._backlog_wait_sec
            while self._backlog() > 0 and time.monotonic() < deadline and not self._stop_event.is_set():
                time.sleep(0.05)
            return
        last = self._pending_before(since)
        moved_at = time.monotonic()
        clear = 0
        while not self._stop_event.is_set():
            n = self._pending_before(since)
            if n == 0:
                clear += 1
                if clear >= 2:               # 推論の受け渡しの一瞬（待ちから取り出した直後）を 0 と見誤らない
                    return
            else:
                clear = 0
                if n < last:
                    last, moved_at = n, time.monotonic()
                elif time.monotonic() - moved_at >= self._backlog_wait_sec:
                    logger.warning("先に話し始めた発話の聞き取りが %.0f 秒進まないので、制御を先に積みます（残り %d 件）",
                                   self._backlog_wait_sec, n)
                    return
            time.sleep(0.05)

    def poll_once(self) -> int:
        """新規コマンドを 1 回分処理し、積んだ件数を返す（テスト用に分離）。"""
        commands, new_offset = self._log.read_from(self._offset)
        self._offset = new_offset
        n = 0
        for command in commands:
            if command.command_id in self._seen:
                continue
            self._seen.add(command.command_id)
            if command.type == "stop":
                logger.info("control stop (%s)", command.command_id)
                if self._on_stop is not None:
                    self._on_stop(command)
                continue
            self._wait_for_speech(command.created_ts if command.created_ts is not None else self._clock())
            if self._commands is not None:
                handled = self._run_command(command)
                if handled is not None:
                    n += handled
                    continue
            event = command_to_audio_event(command, self._clock)
            if event is not None:
                self._audio_queue.put(event)
                n += 1
                logger.info("control → audio_queue: %s %s", command.type, command.args)
        return n

    def _run_command(self, command: ControlCommand) -> Optional[int]:
        """端末と共通の処理で扱う（積んだ件数）。行の形が無い制御（休み / 参加・台本・持ち点）は None = 今までの翻訳。"""
        from integration.logger_commands import CommandResult, control_line

        args = command.args or {}
        if command.type == "text":
            text = args.get("text")
            result = (self._commands.run_text(text) if isinstance(text, str) and text.strip()
                      else CommandResult(messages=["text には読み上げ文が要ります"], ok=False))
        else:
            try:
                line = control_line(command.type, args)
            except ValueError as e:
                result = CommandResult(messages=[str(e)], ok=False)
            else:
                if line is None:
                    return None
                result = self._commands.run_line(line)
        for event in result.events:
            self._audio_queue.put(event)
        logger.info("control → %s %s: %d event(s)%s", command.type, args, len(result.events),
                    "" if result.ok else f"（未適用: {' / '.join(result.messages)}）")
        if self._on_result is not None:
            try:
                self._on_result(command, result)
            except Exception:  # noqa: BLE001 — 知らせの失敗で制御を止めない
                logger.exception("control on_result failed")
        return len(result.events)

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.poll_once()
            except Exception:  # noqa: BLE001 — consumer は録音を巻き込まず生き残る
                logger.exception("control consumer poll failed")
            self._stop_event.wait(self._poll_s)
