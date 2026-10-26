"""店の PC の卓の形の入力画面（`tools/table_entry.py`）: 次のハンドの見込み・飛ばした席のフォールド・保存。"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

pytest.importorskip("pokerkit")

from core.ground_truth_repository import GroundTruthRepository  # noqa: E402
from tests.test_tools_ground_truth_ui import SID, _hand  # noqa: E402
from tools.ground_truth_ui import make_server  # noqa: E402
from tools.table_entry import current_hand_id, skeleton_after, table_hand, table_legal  # noqa: E402


@pytest.fixture
def log_dir(tmp_path: Path) -> Path:
    d = tmp_path / "logs"
    d.mkdir()
    (d / f"{SID}.json").write_text(json.dumps({"session_id": SID, "hands": [_hand(1)]}), encoding="utf-8")
    return d


def test_the_next_hand_is_ready_before_the_cards_are_dealt(log_dir):
    """ロガーのハンド 1 が終わっていれば、配る前にハンド 2 を入れられる（席・持ち点・ボタンは前のハンドから）。"""
    repo = GroundTruthRepository(log_dir)
    assert current_hand_id(log_dir, SID) == 2
    t = table_hand(log_dir, SID, 2, repo)
    assert t["state"] == "pending"
    base = t["base"]
    assert [(p["seat"], p["stack_start"]) for p in base["players"]] == [(4, 9800), (5, 10400), (6, 9800)]
    assert base["button_seat"] == 4                     # ハンド 1 のボタン 6 の次
    assert base["blinds"] == {"sb": 100, "bb": 200}
    assert table_hand(log_dir, SID, 3, repo) is None    # 2 つ先は無い
    assert table_hand(log_dir, SID, 1, repo)["state"] == "done"


def test_the_next_hand_uses_the_truth_of_the_previous_hand(log_dir):
    """前のハンドの真のアクションがあれば、その収支で持ち点を見込む（記録の持ち点の誤りを引き継がない）。"""
    gt = {"button_seat": 6, "actions": [{"seat": 6, "action": "fold"}, {"seat": 4, "action": "fold"}],
          "players": [{"seat": s} for s in (4, 5, 6)], "winner_seat": 5}
    base = skeleton_after(_hand(1), gt)
    assert [(p["seat"], p["stack_start"]) for p in base["players"]] == [(4, 9900), (5, 10100), (6, 10000)]


def test_skipped_seats_are_folded(log_dir):
    """手番より先の席を押したら、間の席はフォールド（フォールドは省略されることがある）。チェックとコール、
    ベットとレイズは同じボタン（状況で決まる）。"""
    repo = GroundTruthRepository(log_dir)
    # ハンド 2: ボタン 4 → SB 5・BB 6、最初の手番は席4
    status, r = table_legal(log_dir, SID, 2, {"actions": [{"seat": 5, "action": "raise", "amount": 600}]}, repo)
    assert status == 200 and r["error"] is None
    assert [(a["seat"], a["action"], a.get("auto", False)) for a in r["rows"]] == [
        (4, "fold", True), (5, "raise", False)]
    assert [(a["seat"], a["action"], a["amount"]) for a in r["actions"]] == [(4, "fold", 0), (5, "raise", 600)]
    status, r = table_legal(log_dir, SID, 2, {"actions": r["rows"] + [{"seat": 6, "action": "call"}]}, repo)
    assert r["actions"][-1]["action"] == "call" and r["next"]["street"] == "flop"


def test_pending_entry_is_saved_once_the_logger_starts_the_hand(log_dir, tmp_path):
    """まだ配っていないハンドは保存できない（404）。ロガーが始めたら保存できる。"""
    srv = make_server(log_dir, "127.0.0.1", 0, tmp_path / "corr.json")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        with urllib.request.urlopen(f"{base}/table") as resp:
            assert "卓" in resp.read().decode("utf-8")
        with urllib.request.urlopen(f"{base}/api/table/{SID}/current") as resp:
            assert json.loads(resp.read())["hand_id"] == 2
        body = {"source": "manual-edit", "hand": {"actions": [{"seat": 4, "action": "fold"}], "players": [],
                                                    "blind": True, "button_seat": 4}}
        req = urllib.request.Request(f"{base}/api/sessions/{SID}/hands/2", data=json.dumps(body).encode(),
                                     method="PUT", headers={"Content-Type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req)
        assert e.value.code == 404
        live = dict(_hand(2), actions=[])
        (log_dir / f"{SID}.live_hand.json").write_text(json.dumps({"hand": live}), encoding="utf-8")
        with urllib.request.urlopen(f"{base}/api/table/{SID}/hands/2") as resp:
            assert json.loads(resp.read())["state"] == "in_progress"
        # 前のハンド（2）が配られた時点で、開いたときに入れるハンドは次（3）に移る（終わるのを待たない, 2026-10-26）
        with urllib.request.urlopen(f"{base}/api/table/{SID}/current") as resp:
            assert json.loads(resp.read())["hand_id"] == 3
        with urllib.request.urlopen(f"{base}/api/table/{SID}/hands/3") as resp:
            assert json.loads(resp.read())["state"] == "pending"
        with urllib.request.urlopen(req) as resp:
            assert resp.status == 200
        req = urllib.request.Request(f"{base}/api/table/{SID}/hands/2/legal",
                                     data=json.dumps({"actions": [{"seat": 5, "action": "call"}]}).encode(),
                                     method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as resp:
            r = json.loads(resp.read())
        assert [a["seat"] for a in r["rows"]] == [6, 4, 5]      # ロガーのハンド 2 はボタン 6 → 手番は席6 から
    finally:
        srv.shutdown()
        srv.server_close()


def _stackless_log(tmp_path: Path) -> Path:
    """持ち点を数えないモードのセッション（記録に持ち点が無い = `core/stackless.py:strip_hand` のあと）。"""
    d = tmp_path / "logs_stackless"
    d.mkdir()
    hand = {"hand_id": 1, "session_id": SID, "started_at": "2026-10-26T18:00:00", "ended_at": "2026-10-26T18:01:00",
            "stackless": True, "blinds": {"sb": 100, "bb": 200}, "board": [], "button_seat": 6,
            "players": [{"seat": s, "name": n} for s, n in ((4, "A"), (5, "B"), (6, "C"))],
            "actions": [], "winner_seat": 5}
    (d / f"{SID}.json").write_text(json.dumps({"session_id": SID, "hands": [hand]}), encoding="utf-8")
    return d


def test_stackless_next_hand_keeps_the_seats(tmp_path):
    """持ち点を数えないモード: 記録に持ち点が無くても、次のハンドに前のハンドの席が並ぶ（前は席が 0 で入れられなかった）。"""
    d = _stackless_log(tmp_path)
    repo = GroundTruthRepository(d)
    t = table_hand(d, SID, 2, repo)
    assert t["state"] == "pending" and t["base"]["stackless"] is True
    assert [p["seat"] for p in t["base"]["players"]] == [4, 5, 6] and t["base"]["button_seat"] == 4
    # 真のアクションで「いなかった」席は次のハンドに入れない
    gt = {"button_seat": 6, "actions": [], "players": [{"seat": 4}, {"seat": 5}, {"seat": 6, "absent": True}]}
    assert [p["seat"] for p in skeleton_after(json.loads((d / f"{SID}.json").read_text())["hands"][0], gt)["players"]] == [4, 5]


def test_stackless_hides_the_stacks_and_the_allin_amount(tmp_path):
    """持ち点を数えないモード: 席の持ち点（内部の仮の値）と、オールインの額・それに向き合ったコールの額・ポットを出さない。"""
    d = _stackless_log(tmp_path)
    repo = GroundTruthRepository(d)
    status, r = table_legal(d, SID, 2, {"actions": [{"seat": 4, "action": "allin"}]}, repo)
    assert status == 200 and r["error"] is None
    assert all(s["stack"] is None for s in r["seats"])
    allin = next(s for s in r["seats"] if s["seat"] == 4)
    assert allin["committed"] is None
    assert r["actions"][0]["action"] == "allin" and r["actions"][0]["total"] is None
    assert r["next"]["actor_seat"] == 5 and r["next"]["call_unknown"] is True and r["next"]["pot"] is None
    status, r = table_legal(d, SID, 2, {"actions": r["rows"] + [{"seat": 5, "action": "call"}]}, repo)
    assert next(s for s in r["seats"] if s["seat"] == 5)["committed"] is None   # オールインに向き合ったコールの額
