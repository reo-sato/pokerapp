"""tests/test_tools_ground_truth_ui.py

真のアクション入力の画面（`tools/ground_truth_ui.py`）: ハンドログを読み、pokerkit で手番を補い、
`logs/<sid>.ground_truth.json` を書く。staff API（ADR-0043）と同じ規則（要確認のハンドは「記録どおり」を拒む）。
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from tools.ground_truth_ui import (
    list_sessions,
    make_server,
    replay_legal,
    validate_gt_hand,
)
from core.ground_truth import GroundTruthError
from core.ground_truth_repository import GroundTruthRepository
from tools.measure_capture_accuracy import measure_session

SID = "2026-09-27_200000_session1"
PLAYERS = [
    {"seat": 4, "name": "A", "hole_cards": None, "stack_start": 10000, "stack_end": 9800, "result": -200},
    {"seat": 5, "name": "B", "hole_cards": ["Ah", "Ad"], "stack_start": 10000, "stack_end": 10400, "result": 400},
    {"seat": 6, "name": "C", "hole_cards": ["Kc", "Qc"], "stack_start": 10000, "stack_end": 9800, "result": -200},
]
# 席6 コール / 席4 コール / 席5 チェック → flop: 席4 チェック / 席5 ベット 600 / 席6 フォールド / 席4 フォールド
ACTIONS = [
    ("preflop", 6, "call", 200), ("preflop", 4, "call", 100), ("preflop", 5, "check", 0),
    ("flop", 4, "check", 0), ("flop", 5, "bet", 600), ("flop", 6, "fold", 0), ("flop", 4, "fold", 0),
]


def _hand(hand_id: int, *, review: bool = False) -> dict:
    actions = []
    for i, (street, seat, action, amount) in enumerate(ACTIONS):
        actions.append({
            "street": street, "seat": seat, "action": action, "amount": amount,
            "raw_text": action, "needs_review": review and i == 4,
            "reason": "ambiguous_amount" if review and i == 4 else None,
        })
    return {
        "hand_id": hand_id, "session_id": SID,
        "started_at": f"2026-09-27T20:0{hand_id}:00", "ended_at": f"2026-09-27T20:0{hand_id}:40",
        "blinds": {"sb": 100, "bb": 200}, "board": ["As", "Kd", "7h"], "board_source": "rfid",
        "board_timeline": [], "button_seat": 6, "position_map": {"4": "SB", "5": "BB", "6": "BTN"},
        "players": [dict(p) for p in PLAYERS], "pot_total": 1200, "pots": [],
        "winner_seat": 5, "winner_source": "fold", "actions": actions, "review_required": review,
    }


@pytest.fixture
def log_dir(tmp_path: Path) -> Path:
    d = tmp_path / "logs"
    d.mkdir()
    (d / f"{SID}.json").write_text(
        json.dumps({"session_id": SID, "hands": [_hand(1), _hand(2, review=True)]}, ensure_ascii=False),
        encoding="utf-8",
    )
    (d / f"{SID}.table_state.json").write_text("{}", encoding="utf-8")     # sidecar = セッションではない
    (d / "broken.json").write_text("{not json", encoding="utf-8")
    return d


@pytest.fixture
def base(log_dir: Path):
    server = make_server(log_dir, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _req(base: str, method: str, path: str, body: dict | None = None) -> tuple[int, dict | str]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            status, raw, ctype = resp.status, resp.read(), resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        status, raw, ctype = e.code, e.read(), e.headers.get("Content-Type", "")
    if "json" in ctype:
        return status, json.loads(raw.decode("utf-8"))
    return status, raw.decode("utf-8")


class TestRead:
    def test_sessions_exclude_sidecars_and_broken_files(self, log_dir):
        sessions = list_sessions(log_dir, GroundTruthRepository(log_dir))
        assert [s["session_id"] for s in sessions] == [SID]
        assert sessions[0]["hands"] == 2 and sessions[0]["annotated"] == 0

    def test_page_is_served(self, base):
        status, body = _req(base, "GET", "/")
        assert status == 200 and "真のアクション入力" in body
        assert "＋ 行を追加" in body and "function addRow()" in body     # 最後に 1 行足すボタン（オーナー要望）

    def test_hand_list_is_newest_first_with_review_and_status(self, base):
        status, d = _req(base, "GET", f"/api/sessions/{SID}/hands")
        assert status == 200
        assert [h["hand_id"] for h in d["hands"]] == [2, 1]
        by_id = {h["hand_id"]: h for h in d["hands"]}
        assert by_id[2]["has_needs_review"] is True and by_id[1]["has_needs_review"] is False
        assert all(h["ground_truth"] is None for h in d["hands"])
        assert d["summary"]["annotated"] == 0 and d["summary"]["review"] == 1

    def test_hand_detail_prefills_and_replays_with_pokerkit(self, base):
        status, d = _req(base, "GET", f"/api/sessions/{SID}/hands/1")
        assert status == 200
        assert d["captured"]["hand_id"] == 1 and d["ground_truth"] is None
        legal = d["legal"]
        assert legal["error"] is None
        assert [a["street"] for a in legal["actions"]] == [s for s, _, _, _ in ACTIONS]
        assert legal["actions"][1] == {"seat": 4, "action": "call", "amount": 100, "street": "preflop"}
        assert legal["next"]["hand_over"] is True and legal["next"]["foldout_winner"] == 5

    def test_unknown_session_and_hand_are_404(self, base):
        assert _req(base, "GET", "/api/sessions/nope/hands")[0] == 404
        assert _req(base, "GET", f"/api/sessions/{SID}/hands/99")[0] == 404
        assert _req(base, "PUT", f"/api/sessions/{SID}/hands/99", {"source": "captured-passthrough"})[0] == 404


class TestSave:
    def test_passthrough_writes_ground_truth_that_measures_as_a_match(self, base, log_dir):
        status, d = _req(base, "PUT", f"/api/sessions/{SID}/hands/1",
                         {"source": "captured-passthrough", "annotator": "owner"})
        assert status == 200, d
        assert d["accuracy"]["all_match"] is True and d["saved"]["source"] == "captured-passthrough"
        gt_path = log_dir / f"{SID}.ground_truth.json"
        gt = json.loads(gt_path.read_text(encoding="utf-8"))
        assert [h["hand_id"] for h in gt["hands"]] == [1] and gt["hands"][0]["annotator"] == "owner"
        log = json.loads((log_dir / f"{SID}.json").read_text(encoding="utf-8"))
        result = measure_session(log, gt)
        assert result.action_accuracy == 1.0 and result.board_accuracy == 1.0
        _, rows = _req(base, "GET", f"/api/sessions/{SID}/hands")
        row = next(h for h in rows["hands"] if h["hand_id"] == 1)
        assert row["ground_truth"]["source"] == "captured-passthrough" and row["accuracy"]["all_match"]
        assert rows["summary"]["annotated"] == 1 and rows["summary"]["hands_match"] == 1

    def test_passthrough_is_rejected_for_a_hand_that_needs_review(self, base, log_dir):
        status, d = _req(base, "PUT", f"/api/sessions/{SID}/hands/2", {"source": "captured-passthrough"})
        assert status == 400 and "要確認" in d["message"]
        assert not (log_dir / f"{SID}.ground_truth.json").exists()

    def test_manual_edit_is_saved_and_compared_with_the_record(self, base, log_dir):
        hand = {
            "board": ["As", "Kd", "7h"],
            "actions": [
                {"seat": 6, "action": "call", "amount": 200, "street": "preflop"},
                {"seat": 4, "action": "call", "amount": 100, "street": "preflop"},
                {"seat": 5, "action": "check", "amount": 0, "street": "preflop"},
                {"seat": 4, "action": "check", "amount": 0, "street": "flop"},
                {"seat": 5, "action": "bet", "amount": 800, "street": "flop"},    # 記録は 600（聞き違い）
                {"seat": 6, "action": "fold", "amount": 0, "street": "flop"},
                {"seat": 4, "action": "fold", "amount": 0, "street": "flop"},
            ],
            "players": [{"seat": 5, "hole_cards": ["Ah", "Ad"], "showed_down": False}],
            "winner_seat": 5, "notes": "ベットは 800 だった",
        }
        status, d = _req(base, "PUT", f"/api/sessions/{SID}/hands/2",
                         {"source": "manual-edit", "annotator": "owner", "hand": hand})
        assert status == 200, d
        assert d["accuracy"]["all_match"] is False
        assert (d["accuracy"]["action_correct"], d["accuracy"]["action_total"]) == (6, 7)
        assert d["accuracy"]["board_match"] is True and d["accuracy"]["winner_match"] is True
        status, detail = _req(base, "GET", f"/api/sessions/{SID}/hands/2")
        assert detail["ground_truth"]["source"] == "manual-edit"
        assert detail["ground_truth"]["actions"][4]["amount"] == 800
        assert detail["ground_truth"]["notes"] == "ベットは 800 だった"
        assert detail["legal"]["actions"][4]["amount"] == 800       # 入力欄は GT から始まる
        # 上書き（LWW）: もう一度「記録どおり」相当の内容で保存すると一致になる
        hand["actions"][4]["amount"] = 600
        status, d = _req(base, "PUT", f"/api/sessions/{SID}/hands/2",
                         {"source": "manual-edit", "hand": hand})
        assert status == 200 and d["accuracy"]["all_match"] is True
        gt = json.loads((log_dir / f"{SID}.ground_truth.json").read_text(encoding="utf-8"))
        assert len(gt["hands"]) == 1

    @pytest.mark.parametrize("hand, fragment", [
        ({"board": ["Xx"], "actions": []}, "カード"),
        ({"board": [], "actions": [{"seat": 4, "action": "limp"}]}, "不正"),
        ({"board": ["As"], "actions": [], "players": [{"seat": 4, "hole_cards": ["As", "Kd"]}]}, "両方"),
        ({"board": [], "actions": "x"}, "配列"),
        (None, "hand"),
    ])
    def test_manual_edit_validation(self, base, hand, fragment):
        status, d = _req(base, "PUT", f"/api/sessions/{SID}/hands/1",
                         {"source": "manual-edit", "hand": hand})
        assert status == 400 and fragment in d["message"], d

    def test_unknown_source_and_bad_json(self, base):
        status, d = _req(base, "PUT", f"/api/sessions/{SID}/hands/1", {"source": "guess"})
        assert status == 400
        req = urllib.request.Request(f"{base}/api/sessions/{SID}/hands/1", data=b"{nope", method="PUT",
                                     headers={"Content-Type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=5)
        assert e.value.code == 400


class TestLegal:
    def test_endpoint_reports_the_first_illegal_row(self, base):
        actions = [{"seat": 6, "action": "call"}, {"seat": 5, "action": "check"}]     # 席5 は手番ではない
        status, d = _req(base, "POST", f"/api/sessions/{SID}/hands/1/legal", {"actions": actions})
        assert status == 200
        assert d["error"] == {"index": 1, "message": "手番は席4 です（席5 の番ではありません）"}
        assert len(d["actions"]) == 1 and d["next"]["actor_seat"] == 4

    def test_endpoint_requires_actions(self, base):
        assert _req(base, "POST", f"/api/sessions/{SID}/hands/1/legal", {"x": 1})[0] == 400

    def test_replay_fills_call_amounts_streets_and_the_foldout_winner(self):
        cap = _hand(1)
        r = replay_legal(cap, [{"seat": s, "action": a, "amount": m} for _, s, a, m in ACTIONS])
        assert r["error"] is None
        assert [(a["seat"], a["action"], a["amount"], a["street"]) for a in r["actions"]] == [
            (6, "call", 200, "preflop"), (4, "call", 100, "preflop"), (5, "check", 0, "preflop"),
            (4, "check", 0, "flop"), (5, "bet", 600, "flop"), (6, "fold", 0, "flop"), (4, "fold", 0, "flop"),
        ]
        assert r["next"]["hand_over"] and r["next"]["foldout_winner"] == 5 and r["next"]["pot"] == 1200

    def test_replay_normalizes_check_call_and_flags_bad_amounts(self):
        cap = _hand(1)
        r = replay_legal(cap, [{"seat": 6, "action": "check"}])            # ベットに対しては コール
        assert r["actions"][0]["action"] == "call" and r["actions"][0]["amount"] == 200
        r = replay_legal(cap, [{"seat": 6, "action": "raise", "amount": 50}])
        assert r["error"]["index"] == 0 and "最小 400" in r["error"]["message"]
        r = replay_legal(cap, [{"seat": 6, "action": "fold"}, {"seat": 4, "action": "fold"}])
        assert r["next"]["foldout_winner"] == 5

    def test_replay_allows_a_fold_when_checking_was_possible(self):
        cap = _hand(1)
        r = replay_legal(cap, [{"seat": 6, "action": "call"}, {"seat": 4, "action": "call"},
                               {"seat": 5, "action": "check"}, {"seat": 4, "action": "fold"}])
        assert r["error"] is None and r["actions"][-1]["action"] == "fold"

    def test_replay_without_players_reports_why(self):
        r = replay_legal({"hand_id": 1}, [])
        assert r["next"] is None and r["error"]["index"] == -1


class TestValidate:
    def test_keeps_only_known_fields_and_zeroes_fold_check_amounts(self):
        out = validate_gt_hand({
            "board": ["As", "Kd", None], "actions": [{"seat": 4, "action": "fold", "amount": 300}],
            "players": [{"seat": 4, "name": "A", "hole_cards": [], "extra": 1}],
            "winner_seat": "4", "notes": "  memo  ", "junk": True,
        })
        assert out == {
            "board": ["As", "Kd"], "actions": [{"seat": 4, "action": "fold", "amount": 0}],
            "players": [{"seat": 4, "hole_cards": None, "showed_down": False, "name": "A"}],
            "winner_seat": 4, "notes": "memo",
        }

    def test_rejects_more_than_two_hole_cards(self):
        with pytest.raises(GroundTruthError):
            validate_gt_hand({"board": [], "actions": [],
                              "players": [{"seat": 4, "hole_cards": ["As", "Kd", "Qh"]}]})
