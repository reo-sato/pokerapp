"""tests/test_estimate_logs.py

店舗のログのハンドごとに推定器 v1 を回して推定のファイルを書く（`tools/estimate_logs.py`）。推定のハンドは
始まりの時刻でライブの記録のハンドに合わせ、記録を読む側（`core/hand_estimate.py`）がそのまま重ねられる形にする。
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from core.hand_estimate import apply_estimate
from integration.replay import load_events
from tools.estimate_logs import _live_hand, estimate_session, newest_session, presence_for

STORE = Path(__file__).resolve().parent / "fixtures" / "store"


def test_live_hand_is_matched_by_start_time():
    live = [{"hand_id": 7, "started_at": "2026-09-29T08:52:40.000"},
            {"hand_id": 8, "started_at": "2026-09-29T08:55:00.000"}]
    assert _live_hand(live, "2026-09-29T08:52:43.500")["hand_id"] == 7
    assert _live_hand(live, "2026-09-29T08:53:30.000") is None          # 10 秒より離れている
    assert _live_hand(live, None) is None


def test_the_newest_session(tmp_path):
    """`--latest`: 店舗 PC でセッションのあとに所要を測る（事前登録の手順 = コピペで回せる 1 行）。"""
    import os

    assert newest_session([tmp_path]) is None
    for i, sid in enumerate(("aaa", "bbb", "ccc")):
        path = tmp_path / f"{sid}.events.jsonl"
        path.write_text("", encoding="utf-8")
        os.utime(path, (1_000_000 + i * (1 if sid != "bbb" else 100),) * 2)
    assert newest_session([tmp_path]) == "bbb"


def test_estimate_file_overlays_the_live_record():
    pytest.importorskip("pokerkit")
    folder = next(STORE.glob("*a6ee12e4"))
    expected = json.loads((folder / "expected.json").read_text(encoding="utf-8"))
    setup = expected["setup"]
    flags = {k: bool(setup.get(k)) for k in ("auto_new_hand", "auto_winner", "rfid_folds")}
    transcripts = [json.loads(line) for line in (folder / "transcripts.jsonl").read_text(encoding="utf-8").splitlines()
                   if line]
    events = load_events(folder / "events.jsonl")
    presence = presence_for(folder, expected["session_id"])
    assert presence is not None

    first = estimate_session(events, transcripts, presence, setup, flags, expected["session_id"], [])
    entry = first["hands"]["1"]
    # ライブの記録（ハンド番号が違い、アクションが 1 つ違う）に合わせる
    live = copy.deepcopy(entry["hand"])
    live.update(hand_id=12, players=[dict(p, name=f"live{p['seat']}") for p in live.get("players") or []])
    live["actions"] = [dict(a) for a in live["actions"]]
    live["actions"][0]["amount"] = (live["actions"][0].get("amount") or 0) + 100
    data = estimate_session(events, transcripts, presence, setup, flags, expected["session_id"], [live])
    assert list(data["hands"]) == ["12"] and data["params_hash"] and data["estimator_version"]
    entry = data["hands"]["12"]
    assert entry["changed"] is True and entry["hand"]["hand_id"] == 12
    assert 0.0 < entry["posterior"] <= 1.0
    shown = apply_estimate(live, entry)
    assert shown["actions"] == entry["hand"]["actions"]                  # 推定のアクション
    assert [p["name"] for p in shown["players"]] == [p["name"] for p in live["players"]]   # 席の人はライブの記録
    assert shown["_live"]["actions"] == live["actions"] and shown["estimate"]["posterior"] == entry["posterior"]
