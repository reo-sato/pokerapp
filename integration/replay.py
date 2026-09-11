"""integration/replay.py

Phase F1 (#8) — 決定的 replay ハーネス (R4)。

記録済み `events.jsonl` (R1 / `output/event_recorder.py` の `reconstruction_event` envelope) を
読み戻し、live と同じ `IntegrationThread` の per-event 処理に **timestamp 昇順**で通して
`HandSummary` を再構築する。非決定性の源だった wall-clock は `IntegrationThread` に注入する
`clock` で固定するため、同一入力は同一出力になる (round-trip 決定性、DoD #3)。

設計判断 (ADR-0011): threaded `run()` は回さず、timestamp 昇順の同期 driver が
`_handle_audio_event` / `_process_rfid_event` / camera buffer / `_expire_buffers` を再利用する。
理由は `run()` の queue ポーリング / drain 順序が wall-clock スケジューリングに依存し決定的に
扱いにくいため。sidecar 記録 (`_record`) は replay 入力が記録そのものなので呼ばない。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Union

from core.event_queue import make_audio_queue
from core.events import AudioEvent, CameraEvent, RFIDEvent
from core.game_state import PlayerState
from core.hand_log import HandSummary
from core.poker_engine import create_game_state
from integration.engine import IntegrationThread
from output.json_writer import JsonWriter

Event = Union[AudioEvent, RFIDEvent, CameraEvent]

# 同一 timestamp での処理順を live `run()` の drain 順（camera→rfid→audio）に合わせる tie-break。
# これにより同 ts の sensor が audio の corroboration に間に合う（ISSUE-0010 の fidelity）。
_ORDER: dict[type, int] = {CameraEvent: 0, RFIDEvent: 1, AudioEvent: 2}


class _ReplayClock:
    """各イベントの timestamp を「現在時刻」として返す注入用時計。"""

    def __init__(self) -> None:
        self.now: float = 0.0

    def __call__(self) -> float:
        return self.now


def event_from_envelope(d: dict) -> Event:
    """`reconstruction_event` envelope (dict) を Event データクラスに復元する。

    `output/event_recorder.py:event_to_envelope` の逆。
    """
    t = d.get("type")
    if t == "audio":
        return AudioEvent(
            action=d["action"], amount=d["amount"], timestamp=d["timestamp"],
            raw_text=d["raw_text"], seat=d.get("seat"), confidence=d.get("confidence"),
        )
    if t == "rfid":
        return RFIDEvent(
            tag_id=d["tag_id"], card=d["card"], reader_id=d["reader_id"], role=d["role"],
            seat=d.get("seat"), timestamp=d["timestamp"], raw_tag_id=d.get("raw_tag_id") or "",
            board_index=d.get("board_index"),
        )
    if t == "camera":
        return CameraEvent(seat=d["seat"], timestamp=d["timestamp"])
    raise ValueError(f"unknown reconstruction_event type: {t!r}")


def load_events(path: str | Path) -> list[Event]:
    """events.jsonl (1 行 1 envelope) を Event 列に復元する。空行は無視。"""
    events: list[Event] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        events.append(event_from_envelope(json.loads(line)))
    return events


def replay_events(
    events: list[Event],
    *,
    backend: str,
    players: list[PlayerState],
    sb: int,
    bb: int,
    session_id: str,
    out_dir: str | Path,
) -> list[HandSummary]:
    """Event 列を timestamp 昇順で再構築し、確定した HandSummary 群を返す。

    `out_dir` には JsonWriter が `{session_id}.json` を書く (session_id 源でもある)。
    決定性のため clock を注入し、ActionRecord/HandSummary の timestamp を各イベントの
    timestamp に固定する。
    """
    gs = create_game_state(backend, players, sb, bb)
    json_writer = JsonWriter(out_dir, session_id)
    summaries: list[HandSummary] = []
    clock = _ReplayClock()

    thread = IntegrationThread(
        audio_queue=make_audio_queue(),
        game_state=gs,
        json_writer=json_writer,
        on_hand=summaries.append,
        clock=clock,
        stop_event=threading.Event(),
    )

    for ev in sorted(events, key=lambda e: (e.timestamp, _ORDER[type(e)])):
        clock.now = ev.timestamp
        if isinstance(ev, AudioEvent):
            thread._expire_buffers()          # noqa: SLF001 — live run() と同じ前処理
            thread._handle_audio_event(ev)    # noqa: SLF001
            thread._expire_buffers()          # noqa: SLF001
        elif isinstance(ev, RFIDEvent):
            thread._process_rfid_event(ev)    # noqa: SLF001
        elif isinstance(ev, CameraEvent):
            thread._camera_buffer.append(ev)  # noqa: SLF001 — live は drain で buffer 追加
        else:  # pragma: no cover — load_events が型を保証
            raise TypeError(f"unexpected event: {ev!r}")

    return summaries


def replay_fixture(case_dir: str | Path, out_dir: str | Path) -> list[HandSummary]:
    """`<case_dir>/{setup.json, events.jsonl}` を読み replay する。

    setup.json = {"backend", "sb", "bb", "session_id", "players":[{"seat","name","stack"},...]}。
    """
    case_dir = Path(case_dir)
    setup = json.loads((case_dir / "setup.json").read_text(encoding="utf-8"))
    events = load_events(case_dir / "events.jsonl")
    players = [
        PlayerState(seat=p["seat"], name=p["name"], stack=p["stack"])
        for p in setup["players"]
    ]
    return replay_events(
        events,
        backend=setup.get("backend", "legacy"),
        players=players,
        sb=setup["sb"], bb=setup["bb"],
        session_id=setup["session_id"],
        out_dir=out_dir,
    )
