"""tests/test_evidence_log.py

EvidenceLogWriter の単体テスト。M1 の raw 観測ログ機能を検証する。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from core.events import ASRAlternative, AudioEvent, CameraEvent, RFIDEvent, WordTiming
from output.evidence_log import EvidenceLogWriter


def _read_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_evidence_log_writes_audio_with_alternatives(tmp_path: Path) -> None:
    writer = EvidenceLogWriter(log_dir=tmp_path, session_id="s1")

    now = 1716000123.456
    event = AudioEvent(
        action="call",
        amount=600,
        timestamp=now,
        raw_text="コール 600",
        alternatives=[
            ASRAlternative(
                text="コール 600",
                confidence=0.91,
                words=[
                    WordTiming(word="コール", start=now + 0.42, end=now + 0.78, confidence=0.91),
                    WordTiming(word="600",    start=now + 0.81, end=now + 1.32, confidence=0.84),
                ],
            ),
        ],
        word_timestamps=[
            WordTiming(word="コール", start=now + 0.42, end=now + 0.78, confidence=0.91),
            WordTiming(word="600",    start=now + 0.81, end=now + 1.32, confidence=0.84),
        ],
        t_end=now + 1.32,
    )
    writer.write_audio(event)
    writer.close()

    rows = _read_lines(writer.path)
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "audio"
    assert row["action"] == "call"
    assert row["amount"] == 600
    assert row["t_end"] == pytest.approx(now + 1.32)
    assert len(row["alternatives"]) == 1
    alt = row["alternatives"][0]
    assert alt["text"] == "コール 600"
    assert alt["confidence"] == pytest.approx(0.91)
    assert [w["word"] for w in alt["words"]] == ["コール", "600"]
    assert [w["word"] for w in row["word_timestamps"]] == ["コール", "600"]


def test_evidence_log_writes_rfid_with_interval(tmp_path: Path) -> None:
    writer = EvidenceLogWriter(log_dir=tmp_path, session_id="s2")
    t0 = time.time()
    ev = RFIDEvent(
        tag_id="04A1B2C3",
        card="Ah",
        reader_id="seat_3",
        role="seat",
        seat=3,
        timestamp=t0,
        raw_tag_id="04:A1:B2:C3",
        board_index=None,
        t_end=t0 + 4.2,  # fold-on-release のタイミング
    )
    writer.write_rfid(ev)
    writer.close()

    rows = _read_lines(writer.path)
    assert len(rows) == 1
    assert rows[0]["kind"] == "rfid"
    assert rows[0]["seat"] == 3
    assert rows[0]["card"] == "Ah"
    assert rows[0]["t_end"] == pytest.approx(t0 + 4.2)


def test_evidence_log_writes_camera(tmp_path: Path) -> None:
    writer = EvidenceLogWriter(log_dir=tmp_path, session_id="s3")
    ev = CameraEvent(seat=2, timestamp=time.time())
    writer.write_camera(ev)
    writer.close()

    rows = _read_lines(writer.path)
    assert len(rows) == 1
    assert rows[0]["kind"] == "camera"
    assert rows[0]["seat"] == 2


def test_evidence_log_append_mode_preserves_existing(tmp_path: Path) -> None:
    """同じセッション ID で 2 回目を開いても既存行を消さない (append-only)。"""
    w1 = EvidenceLogWriter(log_dir=tmp_path, session_id="s4")
    w1.write_camera(CameraEvent(seat=1, timestamp=1.0))
    w1.close()

    w2 = EvidenceLogWriter(log_dir=tmp_path, session_id="s4")
    w2.write_camera(CameraEvent(seat=2, timestamp=2.0))
    w2.close()

    rows = _read_lines(w1.path)
    assert len(rows) == 2
    assert rows[0]["seat"] == 1
    assert rows[1]["seat"] == 2


def test_evidence_log_extra_fields(tmp_path: Path) -> None:
    """extra dict が record に merge される (M3 で beam_snapshot_top3 を渡すための機構)。"""
    writer = EvidenceLogWriter(log_dir=tmp_path, session_id="s5")
    ev = AudioEvent(action="fold", amount=0, timestamp=1.0, raw_text="フォールド")
    writer.write_audio(ev, extra={"map_action_id": "h1#a3", "beam_snapshot_top3": []})
    writer.close()

    rows = _read_lines(writer.path)
    assert rows[0]["map_action_id"] == "h1#a3"
    assert rows[0]["beam_snapshot_top3"] == []


def test_integration_thread_creates_evidence_log(tmp_path: Path) -> None:
    """IntegrationThread が _evidence_log を生成し、AudioEvent を書き込むことを確認。"""
    import threading
    from core.event_queue import EventQueue
    from core.game_state import GameStateManager, PlayerState
    from integration.engine import IntegrationThread
    from output.json_writer import JsonWriter

    audio_q = EventQueue()
    players = [PlayerState(seat=1, name="A", stack=10000),
               PlayerState(seat=2, name="B", stack=10000)]
    gs = GameStateManager(players=players, sb=100, bb=200)
    gs.new_hand()
    writer = JsonWriter(log_dir=tmp_path, session_id="evlog_test")
    stop = threading.Event()
    thread = IntegrationThread(
        audio_queue=audio_q,
        game_state=gs,
        json_writer=writer,
        stop_event=stop,
    )

    # AudioEvent (new_hand) を 1 件流す
    audio_q.put(AudioEvent(action="new_hand", amount=0, timestamp=1.0, raw_text="ハンド開始"))
    thread.start()
    time.sleep(0.3)
    stop.set()
    thread.join(timeout=2.0)

    evlog_path = tmp_path / "evidence_evlog_test.jsonl"
    assert evlog_path.exists(), f"Evidence log not created at {evlog_path}"
    rows = _read_lines(evlog_path)
    assert any(r["kind"] == "audio" and r["action"] == "new_hand" for r in rows)
