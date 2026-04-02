## tests/test_logger.py
from pathlib import Path
import json
from output.json_writer import JsonWriter
from core.hand_log import HandSummary, ActionRecord

def test_json_writer_creates_file(tmp_path: Path):
    log_dir = tmp_path / "logs"
    writer = JsonWriter(log_dir=log_dir, session_id="test_session")

    action = ActionRecord(
        hand_id=1,
        timestamp="2026-04-02T12:00:00",
        street="preflop",
        seat=1,
        player_name="Sato",
        action="raise",
        amount=800,
        pot_after=800,
        stack_after=9200,
        source={"audio": True, "camera": False, "rfid": False},
        needs_review=False,
        confidence=0.0,
    )
    summary = HandSummary(
        hand_id=1,
        session_id="test_session",
        started_at="2026-04-02T12:00:00",
        ended_at="2026-04-02T12:01:00",
        blinds={"sb": 100, "bb": 200},
        board=[],
        board_source="",
        players=[],
        pot_total=800,
        winner_seat=1,
        actions=[action],
        review_required=False,
    )
    writer.append_hand_summary(summary)

    path = writer.path
    assert path.exists()

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["session_id"] == "test_session"
    assert len(data["hands"]) == 1
    assert data["hands"][0]["hand_id"] == 1
