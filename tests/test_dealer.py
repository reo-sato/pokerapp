"""配っているディーラーの名前（`dealer <名前>` / 制御 `set_dealer`, ADR-0074）。"""
from __future__ import annotations

import json
import queue
import threading
import time
from pathlib import Path

from core.events import AudioEvent
from core.game_state import GameStateManager, PlayerState
from core.hand_log import HandSummary
from integration.engine import IntegrationThread
from output.json_writer import JsonWriter


def _engine(tmp_path: Path) -> IntegrationThread:
    gs = GameStateManager([PlayerState(1, "A", 100), PlayerState(2, "B", 100)], sb=1, bb=2)
    return IntegrationThread(audio_queue=queue.Queue(), game_state=gs, json_writer=JsonWriter(tmp_path, "s"),
                             stop_event=threading.Event())


def _ev(action: str, raw: str = "") -> AudioEvent:
    return AudioEvent(action=action, amount=0, timestamp=time.time(), raw_text=raw)


def _hands(tmp_path: Path) -> list[dict]:
    return json.loads((tmp_path / "s.json").read_text(encoding="utf-8"))["hands"]


def _play(eng: IntegrationThread) -> None:
    eng._handle_audio_event(_ev("new_hand"))                       # noqa: SLF001
    eng._handle_audio_event(_ev("winner", "シート1 ウィナー"))      # noqa: SLF001


def test_the_dealer_is_written_from_the_next_hand(tmp_path: Path):
    eng = _engine(tmp_path)
    eng._handle_audio_event(_ev("set_dealer", "佐藤"))             # noqa: SLF001  ハンドの外 = すぐ
    _play(eng)
    eng._handle_audio_event(_ev("new_hand"))                       # noqa: SLF001
    eng._handle_audio_event(_ev("set_dealer", "鈴木"))             # noqa: SLF001  ハンドの途中 = 次のハンドから
    eng._handle_audio_event(_ev("winner", "シート2 ウィナー"))      # noqa: SLF001
    _play(eng)
    eng._handle_audio_event(_ev("set_dealer", "-"))                # noqa: SLF001  消す
    _play(eng)
    assert [h.get("dealer") for h in _hands(tmp_path)] == ["佐藤", "佐藤", "鈴木", None]


def test_no_dealer_keeps_the_record_as_before():
    base = dict(hand_id=1, session_id="s", started_at="", ended_at="", blinds={}, board=[], board_source="",
                players=[], pot_total=0, winner_seat=None, actions=[], review_required=False)
    assert "dealer" not in HandSummary(**base).to_dict()
    assert HandSummary(**base, dealer="佐藤").to_dict()["dealer"] == "佐藤"


def test_replay_writes_the_same_dealer(tmp_path: Path):
    from core.game_state import PlayerState as P
    from integration.replay import replay_events

    t0 = 1_795_000_000.0
    evs = [AudioEvent(action="set_dealer", amount=0, timestamp=t0, raw_text="佐藤"),
           AudioEvent(action="new_hand", amount=0, timestamp=t0 + 1, raw_text=""),
           AudioEvent(action="set_dealer", amount=0, timestamp=t0 + 2, raw_text="鈴木"),
           AudioEvent(action="winner", amount=0, timestamp=t0 + 3, raw_text="シート1 ウィナー"),
           AudioEvent(action="new_hand", amount=0, timestamp=t0 + 4, raw_text=""),
           AudioEvent(action="winner", amount=0, timestamp=t0 + 5, raw_text="シート2 ウィナー")]
    hands = replay_events(evs, players=[P(1, "A", 100), P(2, "B", 100)], sb=1, bb=2, session_id="r",
                          out_dir=tmp_path, backend="legacy")
    assert [h.dealer for h in hands] == ["佐藤", "鈴木"]
