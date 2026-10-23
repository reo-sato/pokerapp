"""推定の見直しの画面（`tools/estimate_review.py`）: 推定・真のアクション・ライブの記録を行ごとに突き合わせる。"""
from __future__ import annotations

import json
import threading
import urllib.request
from pathlib import Path

import pytest

pytest.importorskip("pokerkit")

from core.ground_truth_repository import GroundTruthRepository  # noqa: E402
from tests.test_tools_ground_truth_ui import SID, _hand  # noqa: E402
from tools.estimate_review import review_detail, review_summary, save_note  # noqa: E402
from tools.ground_truth_ui import make_server  # noqa: E402

TRUTH = [  # 真: 記録と同じだが、フロップの席5 のベットは 800
    {"seat": 6, "action": "call", "amount": 0}, {"seat": 4, "action": "call", "amount": 0},
    {"seat": 5, "action": "check", "amount": 0}, {"seat": 4, "action": "check", "amount": 0},
    {"seat": 5, "action": "bet", "amount": 800}, {"seat": 6, "action": "fold", "amount": 0},
    {"seat": 4, "action": "fold", "amount": 0},
]


@pytest.fixture
def log_dir(tmp_path: Path) -> Path:
    d = tmp_path / "logs"
    d.mkdir()
    (d / f"{SID}.json").write_text(json.dumps({"session_id": SID, "hands": [_hand(1), _hand(2)]}), encoding="utf-8")
    repo = GroundTruthRepository(d)
    for hid in (1, 2):
        repo.upsert(SID, hid, {"board": ["As", "Kd", "7h"], "actions": TRUTH, "winner_seat": 5,
                               "players": [{"seat": s} for s in (4, 5, 6)]}, annotator="t", source="manual-edit")
    est = _hand(1)
    est["actions"][4] = dict(est["actions"][4], amount=800)       # 推定はハンド 1 の額を直した
    (d / f"{SID}.estimate.shadow.json").write_text(json.dumps({
        "content_hash": "abc", "hands": {"1": {"hand_id": 1, "hand": est, "margin": 2.0, "review": False,
                                               "edits": ["発話 1 を「ベット 800」と読む"], "notes": [],
                                               "alternatives": []}}}), encoding="utf-8")
    return d


def test_summary_marks_the_estimate_and_the_record(log_dir):
    s = review_summary(log_dir, SID, GroundTruthRepository(log_dir))
    h1, h2 = s["hands"]
    assert h1["est"]["exact"] and not h1["live"]["exact"] and h1["live"]["correct"] == 6
    assert "est" not in h2 and h2["live"]["total"] == 7
    assert s["estimate"]["content_hash"] == "abc"


def test_detail_has_row_marks_and_edits(log_dir):
    d = review_detail(log_dir, SID, 1, GroundTruthRepository(log_dir))
    assert d["compare"]["est"]["rows_ok"] == [True] * 7
    assert d["compare"]["live"]["rows_ok"][4] is False
    assert d["truth"]["rows"][4]["street"] == "flop"           # 真のアクションにもストリートが付く
    assert d["estimate"]["edits"] == ["発話 1 を「ベット 800」と読む"]


def test_notes_are_saved_separately(log_dir):
    status, body = save_note(log_dir, SID, 2, {"tags": ["額", "知らない"], "note": " 2 千を 1 千 "})
    assert status == 200 and body["tags"] == ["額"]
    s = review_summary(log_dir, SID, GroundTruthRepository(log_dir))
    assert s["hands"][1]["note"]["note"] == "2 千を 1 千"
    assert not (log_dir / f"{SID}.ground_truth.json").read_text(encoding="utf-8").count("知らない")
    save_note(log_dir, SID, 2, {"tags": [], "note": ""})
    assert "2" not in json.loads((log_dir / f"{SID}.estimate_review.json").read_text(encoding="utf-8"))["hands"]


def test_served_by_the_entry_server(log_dir):
    server = make_server(log_dir, "127.0.0.1", 0, evaluation=False)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urllib.request.urlopen(base + "/review") as r:
            assert "推定の見直し" in r.read().decode("utf-8")
        with urllib.request.urlopen(f"{base}/api/review/{SID}/1") as r:
            assert json.loads(r.read())["compare"]["est"]["exact"] is True
        req = urllib.request.Request(f"{base}/api/review/{SID}/1", method="PUT",
                                     data=json.dumps({"tags": ["額"], "note": "x"}).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as r:
            assert json.loads(r.read())["tags"] == ["額"]
    finally:
        server.shutdown()
        server.server_close()
