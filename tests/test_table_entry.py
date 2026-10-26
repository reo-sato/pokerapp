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


def _rows(r):
    return [(a["seat"], a["action"], a.get("auto", False)) for a in r["rows"]]


def test_skipped_seats_not_facing_a_bet_are_checked(log_dir):
    """補うのはルールどおりのものだけ: ベットに向き合っていない席はチェック（チェックできるのに降りたことにしない）。"""
    repo = GroundTruthRepository(log_dir)
    pre = [{"seat": 4, "action": "call"}, {"seat": 5, "action": "call"}, {"seat": 6, "action": "check"}]
    status, r = table_legal(log_dir, SID, 2, {"actions": pre + [{"seat": 6, "action": "raise", "amount": 400}]}, repo)
    assert status == 200 and r["error"] is None
    assert _rows(r)[3:] == [(5, "check", True), (6, "raise", False)]
    assert r["next"]["actor_seat"] == 4


def test_a_fill_that_ends_the_hand_is_not_made_and_the_click_is_rejected(log_dir):
    """補うとほかが全員降りて押した席が動けなくなるなら補わない。押した行は入れずに理由を返す（前の行は変えない）。"""
    repo = GroundTruthRepository(log_dir)
    rows = [{"seat": 4, "action": "raise", "amount": 600}, {"seat": 5, "action": "fold"},
            {"seat": 6, "action": "raise", "amount": 1800}]
    body = {"actions": rows + [{"seat": 6, "action": "call"}], "check_last": True}
    status, r = table_legal(log_dir, SID, 2, body, repo)
    assert status == 200 and r["error"] is None
    assert r["rejected"]["message"]
    assert _rows(r) == [(4, "raise", False), (5, "fold", False), (6, "raise", False)]
    assert r["next"]["actor_seat"] == 4
    # check_last が無ければ従来どおり、その行で止まったことを見せる
    status, r = table_legal(log_dir, SID, 2, {"actions": rows + [{"seat": 6, "action": "call"}]}, repo)
    assert r["error"]["index"] == 3 and "rejected" not in r


def test_filled_rows_are_redone_for_the_current_button(log_dir):
    """前に補った行は捨てて、いまのボタンで補い直す（ボタンを直したとき合わない行が残らない）。"""
    repo = GroundTruthRepository(log_dir)
    rows = [{"seat": 4, "action": "fold", "auto": True}, {"seat": 5, "action": "raise", "amount": 600}]
    status, r = table_legal(log_dir, SID, 2, {"actions": rows, "button_seat": 5}, repo)
    assert status == 200 and r["error"] is None
    assert _rows(r) == [(5, "raise", False)]          # ボタン 5 の 3 人卓は席5 から


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
                                     data=json.dumps({"actions": [{"seat": 4, "action": "call"}]}).encode(),
                                     method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as resp:
            r = json.loads(resp.read())
        assert [a["seat"] for a in r["rows"]] == [6, 4]         # ロガーのハンド 2 はボタン 6 → 手番は席6 から
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


def test_the_first_hand_can_be_entered_when_the_session_starts(tmp_path):
    """セッションが始まったら（まだ 1 ハンドも配っていなくても）、卓の設定から最初のハンドを入れられる（2026-10-26）。"""
    d = tmp_path / "logs_new"
    d.mkdir()
    table = {"table_mode": "ring", "sb": 100, "bb": 200, "ante": 0, "stackless": False, "first_button": 6,
             "seats": [{"seat": s, "name": n, "stack": 20000} for s, n in ((4, "A"), (5, "B"), (6, "C"))]}
    (d / f"{SID}.json").write_text(json.dumps({"session_id": SID, "hands": [], "table": table}), encoding="utf-8")
    repo = GroundTruthRepository(d)
    assert current_hand_id(d, SID) == 1
    t = table_hand(d, SID, 1, repo)
    assert t["state"] == "pending"
    assert [(p["seat"], p["name"], p["stack_start"]) for p in t["base"]["players"]] == [
        (4, "A", 20000), (5, "B", 20000), (6, "C", 20000)]
    assert t["base"]["button_seat"] == 6 and t["base"]["blinds"] == {"sb": 100, "bb": 200}
    status, r = table_legal(d, SID, 1, {"actions": []}, repo)
    assert status == 200 and r["next"]["actor_seat"] == 6          # ボタン 6 → SB 4・BB 5、3 人なので最初の手番はボタン
    # 卓の設定が無い記録（古い記録）は今までどおり何も出さない
    (d / f"{SID}.json").write_text(json.dumps({"session_id": SID, "hands": []}), encoding="utf-8")
    assert current_hand_id(d, SID) is None


def test_table_entry_reaches_the_players_history_with_show_truth(tmp_path):
    """卓の形の画面で保存した真のアクションが、お客さんの履歴（viewer の `show_truth`）に出る（2026-10-26 オーナーの確認）。

    画面と同じ本文（行は `table_legal` の `actions`・席は番号だけ・`blind`）を同じ受け口（PUT）で保存し、
    `list_player_hands` / `get_hand` の `truth=True` で真のアクションの形になって返ること。保存していないハンドは出ない。
    """
    from api.read_models import get_hand, list_player_hands
    from core.player_repository import PlayerRepository
    from core.session_repository import SessionRepository
    from tests.test_tools_ground_truth_ui import _hand

    pr = PlayerRepository(tmp_path / "players.json")
    sr = SessionRepository(tmp_path / "sessions.json", player_repo=pr)
    sid = sr.create_session(label="見本").session_id
    ids = {s: pr.create_player(n).player_id for s, n in ((4, "A"), (5, "B"), (6, "C"))}
    for hid in (1, 2):
        for seat, pid in ids.items():
            sr.assign_seat(sid, hid, seat, pid)
    d = tmp_path / "logs"
    d.mkdir()
    hands = [dict(_hand(h), session_id=sid) for h in (1, 2)]
    (d / f"{sid}.json").write_text(json.dumps({"session_id": sid, "hands": hands}), encoding="utf-8")

    repo = GroundTruthRepository(d)
    # 真: 席6 レイズ 600・席4 フォールド・席5 コール → フロップ 席5 チェック・席6 ベット 800・席5 フォールド（記録とは違う）
    rows = [{"seat": 6, "action": "raise", "amount": 600}, {"seat": 4, "action": "fold"},
            {"seat": 5, "action": "call"}, {"seat": 5, "action": "check"},
            {"seat": 6, "action": "raise", "amount": 800}, {"seat": 5, "action": "fold"}]
    status, r = table_legal(d, sid, 1, {"actions": rows, "button_seat": 6}, repo)
    assert status == 200 and r["error"] is None and r["next"]["hand_over"]
    hand = {"board": ["As", "Kd", "7h"], "players": [{"seat": s} for s in (4, 5, 6)], "button_seat": 6,
            "notes": "", "blind": True, "winner_seat": r["next"]["foldout_winner"],
            "actions": [{"seat": a["seat"], "action": a["action"], "amount": a.get("amount") or 0, "street": a["street"]}
                        for a in r["actions"]]}
    srv = make_server(d, "127.0.0.1", 0, tmp_path / "corr.json")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{srv.server_address[1]}/api/sessions/{sid}/hands/1",
            data=json.dumps({"source": "manual-edit", "annotator": "table", "hand": hand, "entry_sec": 30.0,
                             "confirm_lint": True}).encode(),
            method="PUT", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as resp:
            assert resp.status == 200
    finally:
        srv.shutdown()
        srv.server_close()

    shown = list_player_hands(ids[5], sid, sr, d, truth=True)
    assert [h["hand_id"] for h in shown] == [1]                       # 保存していないハンド 2 は出ない
    got = get_hand(sid, 1, d, truth=True)
    assert [(a["seat"], a["action"]) for a in got["actions"]] == [
        (6, "raise"), (4, "fold"), (5, "call"), (5, "check"), (6, "bet"), (5, "fold")]
    assert got["winner_seat"] == 6 and got["button_seat"] == 6
    # show_truth が false（既定）なら記録のまま（真のアクションは出ない）
    assert get_hand(sid, 1, d)["winner_seat"] == 5


def _stackless_two_hands(tmp_path: Path) -> Path:
    d = tmp_path / "logs_carry"
    d.mkdir()
    hands = [{"hand_id": h, "session_id": SID, "started_at": f"2026-10-26T20:0{h}:00", "ended_at": f"2026-10-26T20:0{h}:50",
              "stackless": True, "blinds": {"sb": 400, "bb": 800}, "board": [], "button_seat": b,
              "players": [{"seat": s, "name": f"player{s}"} for s in (2, 3, 4, 5, 8)], "actions": [], "winner_seat": 2}
             for h, b in ((1, 8), (2, 2))]
    (d / f"{SID}.json").write_text(json.dumps({"session_id": SID, "hands": hands}), encoding="utf-8")
    return d


def test_stackless_seating_set_on_the_table_carries_to_the_next_hands(tmp_path):
    """持ち点を数えないモード: 画面で空席にした・席に入れた状態を次のハンド以降も引き継ぐ（ロガーにだけある席は
    `captured_seats` で返し、画面は空席で出す。オーナー 2026-10-26）。"""
    d = _stackless_two_hands(tmp_path)
    repo = GroundTruthRepository(d)
    # ハンド 1 の真のアクション: 席 7 に人が座った（席に戻した）・席 3 は空席
    gt1 = {"button_seat": 8, "winner_seat": 2, "blind": True, "actions": [],
           "players": [{"seat": 2}, {"seat": 3, "absent": True}, {"seat": 4}, {"seat": 5}, {"seat": 8},
                       {"seat": 7, "name": "席7", "stack_start": None}]}
    repo.upsert(SID, 1, gt1, annotator="table", source="manual-edit")
    t2 = table_hand(d, SID, 2, repo)
    assert [p["seat"] for p in t2["base"]["players"]] == [2, 4, 5, 7, 8]
    assert t2["captured_seats"] == [2, 3, 4, 5, 8]
    t3 = table_hand(d, SID, 3, repo)                    # まだ配っていない次のハンドも同じ席・ボタンは席 2 の次
    assert t3["state"] == "pending" and [p["seat"] for p in t3["base"]["players"]] == [2, 4, 5, 7, 8]
    assert t3["base"]["button_seat"] == 4


def test_seat_toggle_is_sent_to_the_logger(tmp_path):
    """画面の「空席にする / 席に戻す」は制御（sit_out / sit_in）でロガーへ（次のハンドから）。"""
    from core.control_queue import ControlCommandLog
    from tools.table_entry import send_seat

    d = _stackless_two_hands(tmp_path)
    assert send_seat(d, SID, {"seat": 7, "in": True})[0] == 200
    assert send_seat(d, SID, {"seat": 3, "in": False})[0] == 200
    assert send_seat(d, SID, {"seat": 12, "in": True})[0] == 400
    cmds, _ = ControlCommandLog(d / f"{SID}.control.jsonl").read_from(0)
    assert [(c.type, c.args) for c in cmds] == [("sit_in", {"seat": 7}), ("sit_out", {"seat": 3})]
