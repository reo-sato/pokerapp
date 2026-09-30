"""tests/test_hand_estimate.py

記録の本体 = ライブの記録 ⊕ 推定 ⊕ スタッフの訂正（ADR-0056 D1, オーナー 2026-09-30: 推定器を記録の本体に）。

- 推定のファイル（`<sid>.estimate.json`）があれば、読む側（viewer API・真のアクション入力・PHH）は推定のハンドを読む。
  記録の識別・時刻・札の読み取り・席の人はライブの記録のまま。ライブの記録のアクションは `_live` に残る。
- 訂正は行番号ではなく「どの行か」（訂正の前の street・seat・action・amount）で当てる。推定で行の並びが変わっても
  同じ行に当たり、その行が無くなれば当てない（`_corrections_skipped`）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.hand_correction import HandCorrection, apply_hand_corrections, row_target
from core.hand_estimate import ESTIMATE_SUFFIX, apply_estimate, load_estimates, load_record, overlay_log

SID = "s1"


def _live_hand() -> dict:
    return {
        "hand_id": 3, "session_id": SID, "started_at": "2026-09-30T19:21:34.576", "ended_at": "2026-09-30T19:22:13.792",
        "button_seat": 7, "board": ["Kh", "Th", "As"], "board_timeline": [{"index": 1, "card": "Kh"}],
        "players": [{"seat": 3, "name": "Aoki", "player_id": "p3", "stack_start": 10000},
                    {"seat": 4, "name": "Baba", "player_id": "p4", "stack_start": 10000}],
        "actions": [{"street": "preflop", "seat": 3, "action": "fold", "amount": 0},
                    {"street": "preflop", "seat": 4, "action": "raise", "amount": 500}],
        "winner_seat": 4,
    }


def _estimated_hand() -> dict:
    return {
        "hand_id": 3, "session_id": "replay", "started_at": "2026-09-30T19:21:34.500", "button_seat": 7,
        "board": ["Kh", "Th", "As"],
        "players": [{"seat": 3, "name": "P3", "stack_start": 10000, "result": -500},
                    {"seat": 4, "name": "P4", "stack_start": 10000, "result": 500}],
        "actions": [{"street": "preflop", "seat": 3, "action": "call", "amount": 400},
                    {"street": "preflop", "seat": 4, "action": "check", "amount": 0},
                    {"street": "flop", "seat": 3, "action": "fold", "amount": 0}],
        "winner_seat": 4, "pot_total": 1000,
    }


def _entry(**extra) -> dict:
    return {"hand_id": 3, "started_at": "2026-09-30T19:21:34.576", "estimated_at": "2026-09-30T19:23:00",
            "changed": True, "margin": 2.5, "posterior": 0.92, "hand": _estimated_hand(),
            "alternatives": [{"posterior": 0.08, "actions": []}], "notes": ["聞こえなかったコール"], **extra}


def _write_estimate(log_dir: Path, entry: dict) -> None:
    (log_dir / f"{SID}{ESTIMATE_SUFFIX}").write_text(json.dumps({
        "tool": "estimator", "estimator_version": "1.0", "params_hash": "abc", "session_id": SID,
        "hands": {str(entry["hand_id"]): entry},
    }, ensure_ascii=False), encoding="utf-8")


class TestOverlay:
    def test_the_estimate_is_the_record(self):
        out = apply_estimate(_live_hand(), dict(_entry(), estimator_version="1.0", params_hash="abc"))
        assert [a["action"] for a in out["actions"]] == ["call", "check", "fold"]
        assert out["pot_total"] == 1000
        assert (out["session_id"], out["started_at"], out["ended_at"]) == (
            SID, "2026-09-30T19:21:34.576", "2026-09-30T19:22:13.792")            # 記録の識別・時刻はライブ
        assert out["board_timeline"] == [{"index": 1, "card": "Kh"}]
        assert [(p["name"], p.get("player_id"), p["result"]) for p in out["players"]] == [
            ("Aoki", "p3", -500), ("Baba", "p4", 500)]                            # 席の人はライブ・結果は推定
        assert out["estimate"]["margin"] == 2.5 and out["estimate"]["estimator_version"] == "1.0"
        assert [a["action"] for a in out["_live"]["actions"]] == ["fold", "raise"]

    def test_the_live_record_is_not_changed(self):
        live = _live_hand()
        apply_estimate(live, _entry())
        assert [a["action"] for a in live["actions"]] == ["fold", "raise"] and "estimate" not in live

    @pytest.mark.parametrize("entry", [None, {}, {"hand": {"hand_id": 9}},
                                       {"hand": {"hand_id": 3}, "started_at": "2026-09-30T20:00:00"}])
    def test_no_or_other_estimate_keeps_the_live_record(self, entry):
        live = _live_hand()
        assert apply_estimate(live, entry) is live

    def test_files(self, tmp_path):
        assert load_estimates(tmp_path, SID) == {}
        (tmp_path / f"{SID}{ESTIMATE_SUFFIX}").write_text("{broken", encoding="utf-8")
        assert load_estimates(tmp_path, SID) == {}
        _write_estimate(tmp_path, _entry())
        assert set(load_estimates(tmp_path, SID)) == {3}
        (tmp_path / f"{SID}.json").write_text(json.dumps({"session_id": SID, "hands": [_live_hand()]}),
                                              encoding="utf-8")
        record = load_record(tmp_path, SID)
        assert [a["action"] for a in record["hands"][0]["actions"]] == ["call", "check", "fold"]
        assert overlay_log({"hands": [_live_hand()]}, {})["hands"][0]["actions"][0]["action"] == "fold"


def _correction(index: int, field: str, value, target: dict | None) -> HandCorrection:
    return HandCorrection(correction_id=f"c{index}", session_id=SID, hand_id=3, action_index=index, field=field,
                          new_value=value, corrected_by="staff", corrected_at="2026-09-30T20:00:00", target=target)


class TestCorrectionTargets:
    def test_the_same_row_after_the_rows_move(self):
        hand = _estimated_hand()
        target = row_target(hand["actions"][2])                 # 「flop 席3 fold」を訂正した
        hand["actions"].insert(0, {"street": "preflop", "seat": 5, "action": "fold", "amount": 0})   # 推定し直した
        out = apply_hand_corrections(hand, [_correction(2, "action", "call", target)])
        assert out["actions"][3]["action"] == "call" and out["actions"][3]["_original"]["action"] == "fold"
        assert out["actions"][2]["action"] == "check"

    def test_a_row_that_is_gone_is_not_corrected(self):
        hand = _estimated_hand()
        target = {"street": "turn", "seat": 3, "action": "bet", "amount": 800}
        out = apply_hand_corrections(hand, [_correction(1, "amount", 900, target)])
        assert [a.get("amount") for a in out["actions"]] == [400, 0, 0]
        assert out["_corrections_skipped"][0]["target"] == target and "_corrections" not in out

    def test_old_corrections_without_a_target_use_the_row_number(self):
        out = apply_hand_corrections(_estimated_hand(), [_correction(1, "action", "bet", None)])
        assert out["actions"][1]["action"] == "bet"

    def test_a_second_correction_of_the_same_row(self):
        hand = _estimated_hand()
        target = row_target(hand["actions"][0])
        out = apply_hand_corrections(hand, [_correction(0, "amount", 300, target),
                                            _correction(0, "action", "raise", target)])
        assert (out["actions"][0]["action"], out["actions"][0]["amount"]) == ("raise", 300)


class TestViewerReadsTheEstimate:
    def test_read_models_and_corrections(self, tmp_path):
        from api.read_models import get_hand, list_session_hands
        from core.hand_correction_repository import HandCorrectionRepository

        (tmp_path / f"{SID}.json").write_text(json.dumps({"session_id": SID, "hands": [_live_hand()]}),
                                              encoding="utf-8")
        assert get_hand(SID, 3, tmp_path)["actions"][0]["action"] == "fold"          # 推定が無ければライブ
        _write_estimate(tmp_path, _entry())
        assert [a["action"] for a in get_hand(SID, 3, tmp_path)["actions"]] == ["call", "check", "fold"]
        repo = HandCorrectionRepository(path=tmp_path / "hand_corrections.json")
        base = get_hand(SID, 3, tmp_path)
        repo.add_correction(SID, 3, "amount", 500, action_index=0, target=row_target(base["actions"][0]))
        hands = list_session_hands(SID, tmp_path, repo)
        assert hands[0]["actions"][0]["amount"] == 500 and hands[0]["estimate"]["posterior"] == 0.92
        assert json.loads((tmp_path / f"{SID}.json").read_text())["hands"][0]["actions"][0]["action"] == "fold"


def test_the_staff_api_stores_the_corrected_row(tmp_path):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from api.client import ViewerApiClient
    from api.server import create_app
    from core.hand_correction_repository import HandCorrectionRepository
    from core.ledger_repository import LedgerRepository
    from core.player_repository import PlayerRepository
    from core.session_repository import SessionRepository

    players = PlayerRepository(path=tmp_path / "players.json")
    sessions = SessionRepository(path=tmp_path / "sessions.json", player_repo=players)
    s = sessions.create_session(label="Fri")
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    live = dict(_live_hand(), session_id=s.session_id)
    (log_dir / f"{s.session_id}.json").write_text(json.dumps({"hands": [live]}), encoding="utf-8")
    entry = _entry()
    (log_dir / f"{s.session_id}{ESTIMATE_SUFFIX}").write_text(json.dumps({"hands": {"3": entry}}), encoding="utf-8")
    corrections = HandCorrectionRepository(path=tmp_path / "hand_corrections.json")
    app = create_app(players, sessions, log_dir, ledger_repo=LedgerRepository(path=tmp_path / "ledger.json",
                                                                               session_repo=sessions),
                     orders_writable=True, staff_token="t", correction_repo=corrections)
    staff = ViewerApiClient(client=TestClient(app), staff_token="t")
    c = staff.add_hand_correction(s.session_id, 3, "amount", 600, action_index=0)
    assert c["target"] == {"street": "preflop", "seat": 3, "action": "call", "amount": 400}   # 推定の行
