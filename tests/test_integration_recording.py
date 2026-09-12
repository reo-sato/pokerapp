"""tests/test_integration_recording.py

R1 (ADR-0010): IntegrationThread に event_recorder を渡すと、生イベントが解釈前に
sidecar (events.jsonl) へ記録されること、未指定なら記録されない (挙動不変) こと。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from core.event_queue import make_audio_queue, make_rfid_queue
from core.events import AudioEvent, RFIDEvent
from core.game_state import GameStateManager, PlayerState
from integration.engine import IntegrationThread
from output.event_recorder import EventRecorder
from output.json_writer import JsonWriter


def _make_game() -> GameStateManager:
    gs = GameStateManager(
        players=[
            PlayerState(seat=1, name="Alice", stack=10000),
            PlayerState(seat=2, name="Bob", stack=10000),
        ],
        sb=100,
        bb=200,
    )
    gs.new_hand()
    return gs


def _run(thread: IntegrationThread, stop: threading.Event) -> None:
    thread.start()
    time.sleep(0.3)
    stop.set()
    thread.join(timeout=2.0)


def test_records_audio_and_rfid_events(tmp_path: Path) -> None:
    gs = _make_game()
    audio_q = make_audio_queue()
    rfid_q = make_rfid_queue()
    writer = JsonWriter(log_dir=tmp_path, session_id="s1")
    stop = threading.Event()
    rec_path = tmp_path / "s1.events.jsonl"

    thread = IntegrationThread(
        audio_queue=audio_q,
        game_state=gs,
        json_writer=writer,
        rfid_queue=rfid_q,
        stop_event=stop,
        event_recorder=EventRecorder(rec_path),
    )

    rfid_q.put(RFIDEvent(
        tag_id="04:AA", card="Ah", reader_id="seat_1", role="seat",
        seat=1, timestamp=time.time(), raw_tag_id="04AA", board_index=None,
    ))
    audio_q.put(AudioEvent(action="bet", amount=500, timestamp=time.time(), raw_text="ベット500"))
    _run(thread, stop)

    assert rec_path.exists()
    records = [json.loads(ln) for ln in rec_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    types = [r["type"] for r in records]
    assert "audio" in types
    assert "rfid" in types
    # raw_text が解釈前のまま保存されている (記録は解釈と独立)
    audio_rec = next(r for r in records if r["type"] == "audio")
    assert audio_rec["raw_text"] == "ベット500"
    assert audio_rec["action"] == "bet"


def test_no_recorder_writes_no_sidecar(tmp_path: Path) -> None:
    gs = _make_game()
    audio_q = make_audio_queue()
    writer = JsonWriter(log_dir=tmp_path, session_id="s2")
    stop = threading.Event()

    thread = IntegrationThread(
        audio_queue=audio_q,
        game_state=gs,
        json_writer=writer,
        stop_event=stop,
    )
    audio_q.put(AudioEvent(action="check", amount=0, timestamp=time.time(), raw_text="チェック"))
    _run(thread, stop)

    # recorder 未指定 → sidecar は作られない (挙動不変)
    assert not (tmp_path / "s2.events.jsonl").exists()
