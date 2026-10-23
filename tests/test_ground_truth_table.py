"""真のアクションの入力画面: 席の有無・始めの持ち点（`players[].absent` / `stack_start`, 2026-10-23）。"""
from __future__ import annotations

import json

import pytest

pytest.importorskip("pokerkit")

from tools.ground_truth_ui import (  # noqa: E402
    GroundTruthError, replay_legal, table_seats, validate_gt_hand,
)
from core.truth_hand import with_table  # noqa: E402


def _hand(hid, stacks, results=None, button=3):
    return {"hand_id": hid, "started_at": f"2026-10-19T19:0{hid}:00", "blinds": {"sb": 100, "bb": 200},
            "button_seat": button, "board": [], "actions": [],
            "players": [{"seat": s, "name": f"P{s}", "stack_start": v, "result": (results or {}).get(s, 0)}
                        for s, v in stacks.items()]}


def test_validate_keeps_absent_and_stack():
    hand = validate_gt_hand({"board": [], "actions": [], "players": [
        {"seat": 2, "absent": True, "hole_cards": []}, {"seat": 4, "stack_start": 5000, "hole_cards": []}]})
    assert hand["players"][0]["absent"] is True and hand["players"][1]["stack_start"] == 5000
    with pytest.raises(GroundTruthError):
        validate_gt_hand({"board": [], "actions": [], "players": [{"seat": 2, "absent": True, "hole_cards": ["As"]}]})
    with pytest.raises(GroundTruthError):
        validate_gt_hand({"board": [], "actions": [], "players": [{"seat": 2, "stack_start": -5}]})


def test_absent_seat_changes_the_turn_order():
    cap = _hand(2, {1: 10000, 2: 10000, 3: 10000, 4: 10000})
    # ボタン 席3 → SB 席4・BB 席1 → 最初の手番 席2。席2 がいなければ席3
    assert replay_legal(cap, [])["next"]["actor_seat"] == 2
    table = with_table(cap, [{"seat": 2, "absent": True}])
    assert replay_legal(table, [])["next"]["actor_seat"] == 3


def test_table_seats_estimates_the_stack(tmp_path):
    log = {"session_id": "s", "hands": [
        _hand(1, {1: 10000, 2: 10000, 3: 10000}, {1: -2000, 2: 2000}),
        _hand(2, {1: 8000, 3: 10000}),              # 席2 は記録から外れた
        _hand(3, {1: 8000, 3: 10000, 5: 7000}),
    ]}
    (tmp_path / "s.json").write_text(json.dumps(log), encoding="utf-8")
    seats = {s["seat"]: s["stack"] for s in table_seats(tmp_path, "s", 2)}
    assert seats == {1: 8000, 2: 12000, 3: 10000, 5: 7000}
