"""tests/test_hand_correction.py

ADR-0036 (B4): ハンド訂正 core（append-only repo + read-time オーバーレイ）のテスト。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core.hand_correction import HandCorrection, apply_hand_corrections
from core.hand_correction_repository import (
    HandCorrectionError,
    HandCorrectionRepository,
)


def _hand() -> dict:
    return {
        "hand_id": 1, "session_id": "s1", "winner_seat": 1, "review_required": True,
        "actions": [
            {"action": "check", "amount": 0, "needs_review": True, "confidence": 0.4, "seat": 1},
            {"action": "call", "amount": 200, "needs_review": False, "confidence": 0.9, "seat": 2},
        ],
    }


# ――― オーバーレイ ―――

def test_overlay_corrects_action_and_preserves_original(tmp_path: Path):
    c = HandCorrection("c1", "s1", 1, 0, "action", "bet", "staff", "t1", note="誤認識")
    out = apply_hand_corrections(_hand(), [c])
    a0 = out["actions"][0]
    assert a0["action"] == "bet"
    assert a0["_original"]["action"] == "check"  # 元値保持
    assert a0["corrected"] is True
    assert a0["needs_review"] is False           # 訂正でレビュー解除
    assert out["_corrections"][0]["correction_id"] == "c1"  # 監査痕
    assert out["review_required"] is False        # 未解決 review が無くなった


def test_overlay_does_not_mutate_original():
    hand = _hand()
    apply_hand_corrections(hand, [HandCorrection("c1", "s1", 1, 0, "amount", 500, "staff", "t1")])
    assert hand["actions"][0]["amount"] == 0  # 元 dict は不変


def test_overlay_hand_level_winner():
    out = apply_hand_corrections(
        _hand(), [HandCorrection("c1", "s1", 1, None, "winner_seat", 2, "staff", "t1")])
    assert out["winner_seat"] == 2


def test_overlay_later_correction_wins():
    cs = [
        HandCorrection("c1", "s1", 1, 0, "action", "bet", "staff", "t1"),
        HandCorrection("c2", "s1", 1, 0, "action", "raise", "staff", "t2"),  # 後勝ち
    ]
    out = apply_hand_corrections(_hand(), cs)
    assert out["actions"][0]["action"] == "raise"
    assert out["actions"][0]["_original"]["action"] == "check"  # 元は最初の値


def test_overlay_ignores_out_of_range_and_unknown():
    cs = [
        HandCorrection("c1", "s1", 1, 9, "action", "bet", "staff", "t1"),    # 範囲外
        HandCorrection("c2", "s1", 1, 0, "bogus", "x", "staff", "t2"),       # 未知 field
    ]
    out = apply_hand_corrections(_hand(), cs)
    assert out["actions"][0]["action"] == "check"  # 何も適用されない
    assert "_corrections" not in out


# ――― repository ―――

def _repo(tmp_path: Path) -> HandCorrectionRepository:
    return HandCorrectionRepository(path=tmp_path / "hc.json")


def test_repo_add_and_list(tmp_path: Path):
    repo = _repo(tmp_path)
    repo.add_correction("s1", 1, "action", "bet", action_index=0, note="x")
    repo.add_correction("s1", 2, "winner_seat", 3)
    assert len(repo.list_for_hand("s1", 1)) == 1
    assert len(repo.list_for_session("s1")) == 2
    assert repo.list_for_hand("s1", 1)[0].field == "action"


def test_repo_persists_append_only(tmp_path: Path):
    _repo(tmp_path).add_correction("s1", 1, "amount", 300, action_index=1)
    reopened = _repo(tmp_path)
    got = reopened.list_for_hand("s1", 1)
    assert len(got) == 1 and got[0].new_value == 300


def test_repo_validation(tmp_path: Path):
    repo = _repo(tmp_path)
    # action 訂正に不正 field
    with pytest.raises(HandCorrectionError):
        repo.add_correction("s1", 1, "winner_seat", 2, action_index=0)
    # amount は非負整数
    with pytest.raises(HandCorrectionError):
        repo.add_correction("s1", 1, "amount", -5, action_index=0)
    # hand レベルに action field
    with pytest.raises(HandCorrectionError):
        repo.add_correction("s1", 1, "action", "bet", action_index=None)
    # winner_seat は整数
    with pytest.raises(HandCorrectionError):
        repo.add_correction("s1", 1, "winner_seat", "x")
