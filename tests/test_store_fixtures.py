"""tests/test_store_fixtures.py

店舗で真のアクションを入れたセッションの回帰テスト（ADR-0056 追記 1 の S0）。

`python tools/eval_store.py <zip> --export-fixture tests/fixtures/store` で書き出したフォルダ
（`events.jsonl` + `expected.json`。`expected.json` に `setup` がある形式）を再生し、真のアクションとの一致が
書き出したときの baseline より悪くならないことを確かめる。良くなったら書き出し直すと baseline が上がる。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("pokerkit")

from tools.eval_store import check_fixture  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "store"
CASES = sorted(
    p.parent for p in FIXTURES.glob("*/expected.json")
    if "setup" in json.loads(p.read_text(encoding="utf-8"))
)


@pytest.mark.parametrize("folder", CASES, ids=[c.name for c in CASES])
def test_store_session_does_not_get_worse(folder):
    assert check_fixture(folder) == []
