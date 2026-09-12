"""core/sync_scheduler.py

ADR-0032: sync の定期 auto-trigger（on-demand pull に対する periodic 実行）の純粋ロジック。

「いつ sync を実行すべきか」だけを担い、実 sync 関数（`ViewerApiClient.sync_bidirectional`
等）は callback として注入する。clock も注入できるので決定的にテストできる。実プロセスへの
組み込み（バックグラウンドスレッドで一定間隔に `tick` を呼ぶ）は薄いラッパで、運用判断
（どのプロセスが回すか）は ADR-0029 の「会場主導」に従う。
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable

logger = logging.getLogger(__name__)


class SyncScheduler:
    """interval 経過ごとに sync callback を 1 回呼ぶ最小スケジューラ。

    `interval_sec <= 0` で無効（既定 off）。`tick(now)` を定期的に呼ぶと、due なら callback を
    実行して last_run を更新する。callback の例外は握りつぶしてログのみ（sync 失敗で停止しない）。
    """

    def __init__(
        self,
        interval_sec: float,
        sync_fn: Callable[[], object],
        *,
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        self._interval = float(interval_sec)
        self._sync_fn = sync_fn
        self._now = now_fn
        self._last_run: float | None = None

    @property
    def enabled(self) -> bool:
        return self._interval > 0

    @property
    def last_run(self) -> float | None:
        return self._last_run

    def due(self, now: float | None = None) -> bool:
        """今 sync すべきか（初回は即 due、その後は interval 経過で due）。"""
        if not self.enabled:
            return False
        now = now if now is not None else self._now()
        return self._last_run is None or (now - self._last_run) >= self._interval

    def tick(self, now: float | None = None) -> bool:
        """due なら sync_fn を実行して last_run を更新する。実行したら True。"""
        now = now if now is not None else self._now()
        if not self.due(now):
            return False
        try:
            self._sync_fn()
        except Exception:  # sync 失敗で定期実行を止めない（次 interval で再試行）
            logger.exception("auto-sync callback failed")
        finally:
            self._last_run = now
        return True
