"""tests/test_sync_scheduler.py

ADR-0032: SyncScheduler（sync の定期 auto-trigger）の決定的テスト（clock 注入）。
"""
from __future__ import annotations

from core.sync_scheduler import SyncScheduler


def test_disabled_when_interval_zero():
    calls = []
    s = SyncScheduler(0, lambda: calls.append(1))
    assert s.enabled is False
    assert s.tick(now=100) is False
    assert calls == []


def test_first_tick_runs_immediately():
    calls = []
    s = SyncScheduler(60, lambda: calls.append(1))
    assert s.tick(now=1000) is True
    assert calls == [1]
    assert s.last_run == 1000


def test_does_not_run_before_interval():
    calls = []
    s = SyncScheduler(60, lambda: calls.append(1))
    s.tick(now=1000)
    assert s.tick(now=1030) is False  # 30s < 60s
    assert calls == [1]
    assert s.tick(now=1060) is True  # 60s 経過で再実行
    assert calls == [1, 1]


def test_callback_exception_does_not_stop_schedule():
    state = {"n": 0}

    def boom():
        state["n"] += 1
        raise RuntimeError("sync failed")

    s = SyncScheduler(10, boom)
    assert s.tick(now=0) is True  # 例外でも True（last_run 更新）
    assert s.last_run == 0
    assert s.tick(now=10) is True  # 次 interval で再試行される
    assert state["n"] == 2
