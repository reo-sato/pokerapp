"""tests/test_measure_capture_accuracy.py

Phase A 計測ハーネス（`tools/measure_capture_accuracy.py` /
`docs/dogfood/measurement-plan.md` §1）の単体テスト。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from core.hand_correction import HandCorrection
from tools.measure_capture_accuracy import (
    load_corrections_for_session,
    measure_hand,
    measure_session,
)


def _gt_hand(
    hand_id: int = 1,
    board: list[str] | None = None,
    actions: list[dict] | None = None,
    players: list[dict] | None = None,
    winner_seat: int | None = 2,
) -> dict:
    return {
        "hand_id": hand_id,
        "board": board if board is not None else ["As", "Kc", "Qd", "5h", "2s"],
        "actions": actions if actions is not None else [
            {"street": "preflop", "seat": 2, "action": "raise", "amount": 200},
            {"street": "preflop", "seat": 4, "action": "call", "amount": 200},
            {"street": "flop", "seat": 4, "action": "check", "amount": 0},
            {"street": "flop", "seat": 2, "action": "bet", "amount": 300},
        ],
        "players": players if players is not None else [
            {"seat": 2, "hole_cards": ["Ah", "Ad"], "showed_down": True},
            {"seat": 4, "hole_cards": None, "showed_down": False},
        ],
        "winner_seat": winner_seat,
    }


def _captured_hand(gt: dict, **overrides) -> dict:
    """gt を素直に captured 形に変換した perfect match を作る（テストの土台）。"""
    captured = {
        "hand_id": gt["hand_id"],
        "session_id": "sess-A",
        "started_at": "2026-06-26T10:00:00Z",
        "ended_at": "2026-06-26T10:05:00Z",
        "blinds": {"sb": 100, "bb": 200},
        "board": list(gt["board"]),
        "board_source": "rfid",
        "players": [
            {
                "seat": p["seat"],
                "name": f"P{p['seat']}",
                "hole_cards": p.get("hole_cards"),
                "stack_start": 10000,
                "stack_end": 10000,
                "result": 0,
            }
            for p in gt["players"]
        ],
        "pot_total": sum(a["amount"] for a in gt["actions"]),
        "pots": [],
        "winner_seat": gt.get("winner_seat"),
        "actions": [
            {
                "action": a["action"],
                "amount": a["amount"],
                "street": a["street"],
                "seat": a["seat"],
                "needs_review": False,
                "confidence": 1.0,
                "source": {"audio": True, "rfid": False, "camera": False},
                "pot_after": 0,
                "stack_after": 0,
                "timestamp": "2026-06-26T10:00:00Z",
                "hand_id": gt["hand_id"],
                "player_name": f"P{a['seat']}",
            }
            for a in gt["actions"]
        ],
        "review_required": False,
    }
    captured.update(overrides)
    return captured


def _session(hands: list[dict]) -> dict:
    return {"session_id": "sess-A", "hands": hands}


def _gt_session(hands: list[dict]) -> dict:
    return {
        "session_id": "sess-A",
        "annotator": "test",
        "annotated_at": "2026-06-26T11:00:00Z",
        "source": "manual",
        "hands": hands,
    }


# --- 完全一致 ---

def test_perfect_match_is_100_percent():
    gt = _gt_hand()
    captured = _captured_hand(gt)
    result = measure_session(_session([captured]), _gt_session([gt]))

    assert result.hand_coverage == 1.0
    assert result.action_accuracy == 1.0
    assert result.action_type_accuracy == 1.0
    assert result.action_amount_accuracy == 1.0
    assert result.board_accuracy == 1.0
    assert result.hole_card_accuracy == 1.0
    assert result.winner_seat_accuracy == 1.0
    assert result.missed_hands == []
    assert result.phantom_hands == []
    assert result.phase_a_pass is True


# --- 軸別の劣化 ---

def test_missed_hand_lowers_coverage():
    gt1, gt2 = _gt_hand(1), _gt_hand(2)
    captured = _captured_hand(gt1)
    result = measure_session(_session([captured]), _gt_session([gt1, gt2]))

    assert result.hand_coverage == 0.5
    assert result.missed_hands == [2]
    assert result.phantom_hands == []
    assert result.phase_a_pass is False


def test_phantom_hand_in_captured_is_warned_not_counted():
    gt = _gt_hand(1)
    extra = _captured_hand(_gt_hand(2))
    extra["hand_id"] = 99
    captured = [_captured_hand(gt), extra]
    result = measure_session(_session(captured), _gt_session([gt]))

    assert result.hand_coverage == 1.0
    assert result.phantom_hands == [99]
    assert result.phase_a_pass is True


def test_action_type_mismatch_drops_accuracy():
    gt = _gt_hand(actions=[
        {"street": "preflop", "seat": 2, "action": "raise", "amount": 200},
        {"street": "preflop", "seat": 4, "action": "call", "amount": 200},
    ])
    captured = _captured_hand(gt)
    captured["actions"][1]["action"] = "fold"
    captured["actions"][1]["amount"] = 0

    result = measure_session(_session([captured]), _gt_session([gt]))

    assert result.action_accuracy == 0.5
    assert result.action_type_accuracy == 0.5
    assert result.action_amount_accuracy == 0.5
    assert result.phase_a_pass is False


def test_action_amount_mismatch_splits_metric():
    gt = _gt_hand(actions=[
        {"street": "preflop", "seat": 2, "action": "raise", "amount": 200},
    ])
    captured = _captured_hand(gt)
    captured["actions"][0]["amount"] = 999

    result = measure_session(_session([captured]), _gt_session([gt]))

    assert result.action_type_accuracy == 1.0
    assert result.action_amount_accuracy == 0.0
    assert result.action_accuracy == 0.0


def test_extra_captured_action_penalizes_via_max_denominator():
    gt = _gt_hand(actions=[
        {"street": "preflop", "seat": 2, "action": "raise", "amount": 200},
    ])
    captured = _captured_hand(gt)
    captured["actions"].append({
        "action": "bet", "amount": 100, "street": "flop", "seat": 2,
        "needs_review": False, "confidence": 1.0,
        "source": {"audio": True, "rfid": False, "camera": False},
        "pot_after": 0, "stack_after": 0,
        "timestamp": "2026-06-26T10:00:00Z",
        "hand_id": 1, "player_name": "P2",
    })

    result = measure_session(_session([captured]), _gt_session([gt]))

    assert result.action_accuracy == 0.5
    assert result.phase_a_pass is False


def test_board_mismatch_drops_board_accuracy():
    gt = _gt_hand(board=["As", "Kc", "Qd", "5h", "2s"])
    captured = _captured_hand(gt)
    captured["board"] = ["As", "Kc", "Qd", "5h", "3s"]

    result = measure_session(_session([captured]), _gt_session([gt]))

    assert result.board_accuracy == 0.0
    assert result.phase_a_pass is False


def test_board_order_insensitive():
    gt = _gt_hand(board=["As", "Kc", "Qd", "5h", "2s"])
    captured = _captured_hand(gt)
    captured["board"] = ["2s", "Qd", "Kc", "5h", "As"]

    result = measure_session(_session([captured]), _gt_session([gt]))

    assert result.board_accuracy == 1.0


def test_hole_cards_none_in_gt_not_counted():
    gt = _gt_hand(players=[
        {"seat": 2, "hole_cards": ["Ah", "Ad"], "showed_down": True},
        {"seat": 4, "hole_cards": None, "showed_down": False},
        {"seat": 5, "hole_cards": None, "showed_down": False},
    ])
    captured = _captured_hand(gt)

    result = measure_session(_session([captured]), _gt_session([gt]))

    assert result.hole_card_accuracy == 1.0


def test_winner_seat_n_a_when_no_showdown():
    gt = _gt_hand(winner_seat=None)
    gt.pop("winner_seat", None)
    captured = _captured_hand(gt)
    captured.pop("winner_seat", None)

    result = measure_session(_session([captured]), _gt_session([gt]))

    assert result.winner_seat_accuracy is None


def test_fold_amount_zero_normalization_does_not_penalize():
    gt = _gt_hand(actions=[
        {"street": "preflop", "seat": 4, "action": "fold", "amount": 0},
    ])
    captured = _captured_hand(gt)
    captured["actions"][0]["amount"] = 1234

    result = measure_session(_session([captured]), _gt_session([gt]))

    assert result.action_accuracy == 1.0


# --- 訂正適用 ---

def test_corrections_fix_action_mismatch():
    gt = _gt_hand(actions=[
        {"street": "preflop", "seat": 2, "action": "raise", "amount": 200},
        {"street": "preflop", "seat": 4, "action": "call", "amount": 200},
    ])
    captured = _captured_hand(gt)
    captured["actions"][1]["action"] = "fold"
    captured["actions"][1]["amount"] = 0
    captured["actions"][1]["needs_review"] = True

    corrections = [
        HandCorrection(
            correction_id="c1", session_id="sess-A", hand_id=1,
            action_index=1, field="action", new_value="call",
            corrected_by="staff", corrected_at="2026-06-26T10:10:00Z",
        ),
        HandCorrection(
            correction_id="c2", session_id="sess-A", hand_id=1,
            action_index=1, field="amount", new_value=200,
            corrected_by="staff", corrected_at="2026-06-26T10:10:01Z",
        ),
    ]

    result_raw = measure_session(_session([captured]), _gt_session([gt]))
    result_corrected = measure_session(
        _session([captured]), _gt_session([gt]), corrections=corrections
    )

    assert result_raw.action_accuracy == 0.5
    assert result_corrected.action_accuracy == 1.0


def test_load_corrections_filters_by_session(tmp_path: Path):
    p = tmp_path / "hand_corrections.json"
    p.write_text(
        json.dumps({"corrections": [
            {
                "correction_id": "c1", "session_id": "sess-A", "hand_id": 1,
                "action_index": 0, "field": "action", "new_value": "fold",
                "corrected_by": "staff", "corrected_at": "2026-06-26T10:00:00Z",
            },
            {
                "correction_id": "c2", "session_id": "sess-B", "hand_id": 1,
                "action_index": 0, "field": "action", "new_value": "fold",
                "corrected_by": "staff", "corrected_at": "2026-06-26T10:00:00Z",
            },
        ]}),
        encoding="utf-8",
    )
    assert len(load_corrections_for_session(p, "sess-A")) == 1
    assert len(load_corrections_for_session(p, "sess-B")) == 1
    assert load_corrections_for_session(p, "sess-X") == []


def test_load_corrections_missing_file_returns_empty(tmp_path: Path):
    assert load_corrections_for_session(tmp_path / "absent.json", "sess-A") == []


# --- pass-gate ---

def test_phase_a_pass_requires_all_three_axes():
    gt = _gt_hand()
    captured = _captured_hand(gt)
    captured["board"] = ["2c", "3c", "4c", "5c", "6c"]
    result = measure_session(_session([captured]), _gt_session([gt]))
    assert result.action_accuracy == 1.0
    assert result.board_accuracy == 0.0
    assert result.phase_a_pass is False


def test_threshold_is_inclusive():
    gt = _gt_hand()
    captured = _captured_hand(gt)
    result = measure_session(_session([captured]), _gt_session([gt]), threshold=1.0)
    assert result.phase_a_pass is True


# --- CLI smoke ---

def test_cli_runs_and_exit_code_reflects_pass(tmp_path: Path):
    gt = _gt_hand()
    captured = _captured_hand(gt)
    sess_path = tmp_path / "sess.json"
    gt_path = tmp_path / "sess.ground_truth.json"
    sess_path.write_text(json.dumps(_session([captured])), encoding="utf-8")
    gt_path.write_text(json.dumps(_gt_session([gt])), encoding="utf-8")

    root = Path(__file__).resolve().parent.parent
    cmd = [
        sys.executable, str(root / "tools" / "measure_capture_accuracy.py"),
        "--session", str(sess_path),
        "--ground-truth", str(gt_path),
        "--raw",
        "--json",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["phase_a_pass"] is True
    assert payload["hand_coverage"] == 1.0


def test_cli_returns_1_when_below_threshold(tmp_path: Path):
    gt1, gt2 = _gt_hand(1), _gt_hand(2)
    captured = _captured_hand(gt1)
    sess_path = tmp_path / "sess.json"
    gt_path = tmp_path / "sess.ground_truth.json"
    sess_path.write_text(json.dumps(_session([captured])), encoding="utf-8")
    gt_path.write_text(json.dumps(_gt_session([gt1, gt2])), encoding="utf-8")

    root = Path(__file__).resolve().parent.parent
    cmd = [
        sys.executable, str(root / "tools" / "measure_capture_accuracy.py"),
        "--session", str(sess_path),
        "--ground-truth", str(gt_path),
        "--raw",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert proc.returncode == 1
    assert "FAIL" in proc.stdout


def test_cli_input_error_returns_2(tmp_path: Path):
    root = Path(__file__).resolve().parent.parent
    cmd = [
        sys.executable, str(root / "tools" / "measure_capture_accuracy.py"),
        "--session", str(tmp_path / "absent.json"),
        "--ground-truth", str(tmp_path / "absent_gt.json"),
        "--raw",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert proc.returncode == 2


# --- measure_hand 単体 ---

def test_measure_hand_missed_returns_zero_actions():
    gt = _gt_hand()
    result = measure_hand(gt, None)
    assert result.captured is False
    assert result.action_correct == 0
    assert result.action_total == len(gt["actions"])
    assert result.board_match is False
