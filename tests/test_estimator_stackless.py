"""推定器: 持ち点を数えないモードのセッションは、ハンドごとの記録の席で配る（店舗 2026-10-29）。

10/29 はトーナメントの持ち点を数えないモード（記録の持ち点が空）。推定器の再生は持ち点で配る席を決めるので、休みの
席（3・8）も配り、推定の手番が 1 席ずれて、全部正しいハンドが 0/120 だった。
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

pytest.importorskip("pokerkit")

from core.atomic_io import read_jsonl  # noqa: E402
from integration.estimator import PARAMS, SessionEstimator  # noqa: E402
from integration.world_replay import PresenceTimeline  # noqa: E402
from tools import eval_store  # noqa: E402

ZIP = Path(__file__).resolve().parent.parent / "data" / "store" / "2026-10-29" / "pokerlogs_20261029_224302_text.zip"


@pytest.mark.slow
@pytest.mark.skipif(not ZIP.exists(), reason="10/29 の記録が無い")
def test_the_baseline_deals_the_recorded_seats():
    root, tmp = eval_store.open_input([ZIP])
    try:
        files = next(s for s in eval_store.find_sessions(root) if s.session_id.startswith("ce31c79e"))
        record = eval_store._read_json(files.path(".json"))                  # noqa: SLF001
        events = eval_store.load_events(files.events)
        est = SessionEstimator(
            events, eval_store._read_jsonl(files.path(".transcripts.jsonl")),   # noqa: SLF001
            PresenceTimeline.from_table_state(read_jsonl(files.path(".table_state.jsonl"))),
            eval_store.session_setup(record), eval_store.replay_flags(eval_store.find_config(root), events),
            files.session_id, PARAMS)
        seats = {h["hand_id"]: sorted(p["seat"] for p in h["players"]) for h in record["hands"]}
        base = est.baseline()
        assert len(base) == len(seats)
        assert [h["hand_id"] for h in base if sorted(p["seat"] for p in h["players"]) != seats[h["hand_id"]]] == []
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)
