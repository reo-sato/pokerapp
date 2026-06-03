"""tests/test_event_recorder.py

R1 (ADR-0010): EventRecorder の envelope 変換・JSONL 追記・code↔contract 整合を検査する。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.events import AudioEvent, CameraEvent, RFIDEvent
from output.event_recorder import EventRecorder, event_to_envelope

_SCHEMA = (
    Path(__file__).parent.parent
    / "docs" / "contracts" / "schemas" / "reconstruction_event.schema.json"
)


def test_audio_envelope():
    ev = AudioEvent(action="bet", amount=500, timestamp=123.5, raw_text="ベット 500")
    assert event_to_envelope(ev) == {
        "type": "audio",
        "timestamp": 123.5,
        "action": "bet",
        "amount": 500,
        "raw_text": "ベット 500",
    }


def test_rfid_envelope():
    ev = RFIDEvent(
        tag_id="04:A1:B2", card="Ah", reader_id="seat_1", role="seat",
        seat=1, timestamp=1.0, raw_tag_id="04A1B2", board_index=None,
    )
    env = event_to_envelope(ev)
    assert env["type"] == "rfid"
    assert env["card"] == "Ah"
    assert env["seat"] == 1
    assert env["board_index"] is None


def test_camera_envelope_excludes_frame():
    np = pytest.importorskip("numpy")
    ev = CameraEvent(seat=2, timestamp=2.0, frame=np.zeros((2, 2)))
    env = event_to_envelope(ev)
    assert env == {"type": "camera", "timestamp": 2.0, "seat": 2}
    assert "frame" not in env


def test_unsupported_type_raises():
    with pytest.raises(TypeError):
        event_to_envelope(object())


def test_record_appends_jsonl(tmp_path: Path):
    path = tmp_path / "sub" / "s.events.jsonl"
    rec = EventRecorder(path)
    rec.record(AudioEvent(action="call", amount=0, timestamp=1.0, raw_text="コール"))
    rec.record(CameraEvent(seat=3, timestamp=2.0))
    lines = path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2
    assert json.loads(lines[0])["type"] == "audio"
    assert json.loads(lines[1])["type"] == "camera"


def test_record_unsupported_does_not_raise(tmp_path: Path):
    rec = EventRecorder(tmp_path / "s.events.jsonl")
    rec.record(object())  # warning のみ、例外なし
    # ファイルは作られない / 空
    assert not rec.path.exists() or rec.path.read_text(encoding="utf-8") == ""


def test_recorded_envelopes_match_schema():
    """recorder 出力が reconstruction_event schema を通る (code↔contract)。"""
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    events = [
        AudioEvent(action="bet", amount=200, timestamp=1.0, raw_text="ベット"),
        RFIDEvent(
            tag_id="04:AA", card="Kd", reader_id="board_1", role="board",
            seat=None, timestamp=2.0, raw_tag_id="04AA", board_index=1,
        ),
        RFIDEvent(
            tag_id="04:BB", card="Ah", reader_id="seat_2", role="seat",
            seat=2, timestamp=3.0, raw_tag_id="04BB", board_index=None,
        ),
        CameraEvent(seat=4, timestamp=4.0),
    ]
    for ev in events:
        env = event_to_envelope(ev)
        errors = list(validator.iter_errors(env))
        assert not errors, f"{env} -> {errors}"
