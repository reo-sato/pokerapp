"""テスト全体の設定（テストのダイエット, docs/worklog/2026-10-06-test-diet.md）。

- `vision/` はレガシー（CI の対象外）。手元でも `pytest` だけで同じ範囲を回せるように、ここで外す。
- `slow` の印のテストには、時間切れを 300 秒にする（速い組の既定は pyproject の `timeout = 20`）。
"""
from __future__ import annotations

import pytest

collect_ignore = ["test_vision.py"]

SLOW_TIMEOUT_SEC = 300


def pytest_collection_modifyitems(config, items):
    for item in items:
        if item.get_closest_marker("slow") and not item.get_closest_marker("timeout"):
            item.add_marker(pytest.mark.timeout(SLOW_TIMEOUT_SEC))


@pytest.fixture(autouse=True)
def _logger_state_dir(tmp_path_factory, monkeypatch):
    """ロガーのロック・状態・お知らせのファイル（`core/logger_status.py`）は、試験ごとの一時フォルダに書く
    （リポジトリの logs/ を汚さない・並列の試験でロックを取り合わない, ADR-0073 D2）。"""
    monkeypatch.setenv("POKERAPP_LOGGER_STATE_DIR", str(tmp_path_factory.mktemp("logger_state")))
