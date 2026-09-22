"""tests/test_ground_truth_repository.py

ADR-0043: ground truth ストア（LWW、per-session file）の単体テスト。
"""
from __future__ import annotations

import json
from pathlib import Path

from core.ground_truth import (
    SOURCE_EDITED,
    SOURCE_PASSTHROUGH,
    GroundTruthError,
    hand_has_needs_review,
    validate_source,
)
from core.ground_truth_repository import GroundTruthRepository


def _hand_body(winner_seat: int = 2) -> dict:
    return {
        "board": ["As", "Kc", "Qd", "5h", "2s"],
        "actions": [
            {"street": "preflop", "seat": 2, "action": "raise", "amount": 200},
            {"street": "preflop", "seat": 4, "action": "call", "amount": 200},
        ],
        "players": [
            {"seat": 2, "hole_cards": ["Ah", "Ad"], "showed_down": True},
            {"seat": 4, "hole_cards": None, "showed_down": False},
        ],
        "winner_seat": winner_seat,
    }


def test_upsert_creates_file_and_get_returns_entry(tmp_path: Path):
    repo = GroundTruthRepository(tmp_path)
    entry = repo.upsert(
        "sess-A", 1, _hand_body(), annotator="staff1", source=SOURCE_EDITED
    )

    assert entry.hand_id == 1
    assert entry.annotator == "staff1"
    assert entry.source == SOURCE_EDITED
    assert entry.hand["winner_seat"] == 2

    got = repo.get("sess-A", 1)
    assert got is not None
    assert got.annotated_at == entry.annotated_at
    assert (tmp_path / "sess-A.ground_truth.json").exists()


def test_lwq_overwrites_same_hand(tmp_path: Path):
    repo = GroundTruthRepository(tmp_path)
    repo.upsert("sess-A", 1, _hand_body(2), annotator="a", source=SOURCE_EDITED)
    repo.upsert("sess-A", 1, _hand_body(4), annotator="b", source=SOURCE_PASSTHROUGH)

    entries = repo.list_for_session("sess-A")
    assert len(entries) == 1
    assert entries[0].hand["winner_seat"] == 4
    assert entries[0].annotator == "b"
    assert entries[0].source == SOURCE_PASSTHROUGH


def test_multiple_hands_are_sorted_by_hand_id_in_file(tmp_path: Path):
    repo = GroundTruthRepository(tmp_path)
    repo.upsert("sess-A", 2, _hand_body(), annotator="a", source=SOURCE_EDITED)
    repo.upsert("sess-A", 1, _hand_body(), annotator="a", source=SOURCE_EDITED)
    repo.upsert("sess-A", 3, _hand_body(), annotator="a", source=SOURCE_EDITED)

    payload = json.loads((tmp_path / "sess-A.ground_truth.json").read_text("utf-8"))
    assert [h["hand_id"] for h in payload["hands"]] == [1, 2, 3]
    assert payload["session_id"] == "sess-A"


def test_per_session_files_are_independent(tmp_path: Path):
    repo = GroundTruthRepository(tmp_path)
    repo.upsert("sess-A", 1, _hand_body(2), annotator="a", source=SOURCE_EDITED)
    repo.upsert("sess-B", 1, _hand_body(4), annotator="b", source=SOURCE_EDITED)

    assert repo.get("sess-A", 1).hand["winner_seat"] == 2
    assert repo.get("sess-B", 1).hand["winner_seat"] == 4
    assert (tmp_path / "sess-A.ground_truth.json").exists()
    assert (tmp_path / "sess-B.ground_truth.json").exists()


def test_get_missing_returns_none(tmp_path: Path):
    repo = GroundTruthRepository(tmp_path)
    assert repo.get("sess-X", 99) is None


def test_load_from_disk_in_fresh_repo(tmp_path: Path):
    repo1 = GroundTruthRepository(tmp_path)
    repo1.upsert("sess-A", 1, _hand_body(), annotator="staff1", source=SOURCE_EDITED)

    repo2 = GroundTruthRepository(tmp_path)
    got = repo2.get("sess-A", 1)
    assert got is not None
    assert got.annotator == "staff1"


def test_reload_clears_cache(tmp_path: Path):
    repo = GroundTruthRepository(tmp_path)
    repo.upsert("sess-A", 1, _hand_body(), annotator="staff1", source=SOURCE_EDITED)
    raw = (tmp_path / "sess-A.ground_truth.json").read_text("utf-8")
    parsed = json.loads(raw)
    parsed["hands"][0]["annotator"] = "staff2"
    (tmp_path / "sess-A.ground_truth.json").write_text(
        json.dumps(parsed), encoding="utf-8"
    )
    repo.reload()
    got = repo.get("sess-A", 1)
    assert got.annotator == "staff2"


def test_malformed_hand_is_skipped_not_crashed(tmp_path: Path):
    path = tmp_path / "sess-A.ground_truth.json"
    path.write_text(
        json.dumps({
            "session_id": "sess-A",
            "hands": [
                {"hand_id": 1, "annotator": "ok", "annotated_at": "t", "source": SOURCE_EDITED, "board": []},
                {"this": "is broken"},
                {"hand_id": "not-int"},
            ],
        }),
        encoding="utf-8",
    )
    repo = GroundTruthRepository(tmp_path)
    entries = repo.list_for_session("sess-A")
    assert len(entries) == 1
    assert entries[0].hand_id == 1


# --- ground_truth module-level helpers ---

def test_validate_source_accepts_valid():
    validate_source(SOURCE_EDITED)
    validate_source(SOURCE_PASSTHROUGH)


def test_validate_source_rejects_invalid():
    try:
        validate_source("free-form")
    except GroundTruthError:
        pass
    else:
        raise AssertionError("invalid source should raise")


def test_hand_has_needs_review_at_hand_level():
    assert hand_has_needs_review({"review_required": True, "actions": []}) is True
    assert hand_has_needs_review({"review_required": False, "actions": []}) is False


def test_hand_has_needs_review_at_action_level():
    assert (
        hand_has_needs_review(
            {
                "review_required": False,
                "actions": [
                    {"action": "call", "needs_review": False},
                    {"action": "raise", "needs_review": True},
                ],
            }
        )
        is True
    )


def test_hand_has_needs_review_negative():
    assert (
        hand_has_needs_review(
            {
                "review_required": False,
                "actions": [{"action": "call", "needs_review": False}],
            }
        )
        is False
    )
