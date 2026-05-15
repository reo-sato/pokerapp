"""tests/test_integration_button.py

IntegrationThread に button_seat / 自動 blind post / state-aware 推定が
組み込まれていることを end-to-end で検証する。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from core.event_queue import make_audio_queue
from core.events import AudioEvent
from core.game_state import GameStateManager, PlayerState
from core.hand_log import ActionRecord
from integration.engine import IntegrationThread
from output.json_writer import JsonWriter


def _make_gs(seats: int = 6, stack: int = 10000, sb: int = 100, bb: int = 200) -> GameStateManager:
    players = [PlayerState(seat=i, name=f"P{i}", stack=stack) for i in range(1, seats + 1)]
    return GameStateManager(players=players, sb=sb, bb=bb)


def _drive(thread: IntegrationThread, stop: threading.Event, settle: float = 0.3) -> None:
    thread.start()
    time.sleep(settle)
    stop.set()
    thread.join(timeout=2.0)


class TestNewHandAutoBlinds:
    def test_blinds_posted_when_button_specified(self, tmp_path: Path) -> None:
        gs = _make_gs()
        audio_q = make_audio_queue()
        writer = JsonWriter(log_dir=tmp_path, session_id="bs")
        stop = threading.Event()
        captured: list[ActionRecord] = []

        thread = IntegrationThread(
            audio_queue=audio_q,
            game_state=gs,
            json_writer=writer,
            on_action=captured.append,
            stop_event=stop,
            initial_button_seat=1,
            sb_amount=100,
            bb_amount=200,
        )

        audio_q.put(AudioEvent(
            action="new_hand", amount=0, timestamp=time.time(), raw_text="",
        ))
        _drive(thread, stop)

        # SB_POST + BB_POST が記録され、ポットが 300 になる
        labels = [(r.seat, r.action, r.amount) for r in captured]
        assert (2, "SB_POST", 100) in labels
        assert (3, "BB_POST", 200) in labels
        assert gs.pot == 300
        bs = thread.betting_state
        assert bs.is_initialized
        assert bs.current_bet == 200
        assert bs.actor_seat == 4  # UTG

    def test_amount_only_voice_resolved_to_call(self, tmp_path: Path) -> None:
        gs = _make_gs()
        audio_q = make_audio_queue()
        writer = JsonWriter(log_dir=tmp_path, session_id="bs2")
        stop = threading.Event()
        captured: list[ActionRecord] = []

        thread = IntegrationThread(
            audio_queue=audio_q,
            game_state=gs,
            json_writer=writer,
            on_action=captured.append,
            stop_event=stop,
            initial_button_seat=1,
            sb_amount=100,
            bb_amount=200,
        )

        ts = time.time()
        audio_q.put(AudioEvent(action="new_hand", amount=0, timestamp=ts, raw_text=""))
        # UTG (seat 4) が金額のみ '200' を発話 → CALL 推定
        audio_q.put(AudioEvent(action="", amount=0, timestamp=ts + 0.05, raw_text="200"))
        _drive(thread, stop, settle=0.5)

        utg_action = [r for r in captured if r.seat == 4]
        assert utg_action, "no action captured for UTG"
        assert utg_action[0].action == "call"
        assert utg_action[0].amount == 200

    def test_button_unset_falls_back_to_legacy(self, tmp_path: Path) -> None:
        """initial_button_seat 未指定 → BettingState は未初期化のまま既存動作。"""
        gs = _make_gs()
        gs.new_hand()  # 既存テストと同じ手順
        audio_q = make_audio_queue()
        writer = JsonWriter(log_dir=tmp_path, session_id="bs3")
        stop = threading.Event()
        captured: list[ActionRecord] = []

        thread = IntegrationThread(
            audio_queue=audio_q,
            game_state=gs,
            json_writer=writer,
            on_action=captured.append,
            stop_event=stop,
        )

        audio_q.put(AudioEvent(action="bet", amount=500, timestamp=time.time(),
                               raw_text="ベット500"))
        _drive(thread, stop)

        assert len(captured) == 1
        assert captured[0].action == "bet"
        assert captured[0].amount == 500
        assert thread.betting_state.is_initialized is False


class TestSeatMismatchReview:
    def test_speech_seat_conflict_marks_needs_review(self, tmp_path: Path) -> None:
        gs = _make_gs()
        audio_q = make_audio_queue()
        writer = JsonWriter(log_dir=tmp_path, session_id="bs4")
        stop = threading.Event()
        captured: list[ActionRecord] = []

        thread = IntegrationThread(
            audio_queue=audio_q,
            game_state=gs,
            json_writer=writer,
            on_action=captured.append,
            stop_event=stop,
            initial_button_seat=1,
            sb_amount=100,
            bb_amount=200,
        )

        ts = time.time()
        audio_q.put(AudioEvent(action="new_hand", amount=0, timestamp=ts, raw_text=""))
        # actor は seat 4 だが、音声に "シート5 コール"
        audio_q.put(AudioEvent(action="call", amount=200, timestamp=ts + 0.05,
                               raw_text="シート5 コール"))
        _drive(thread, stop, settle=0.5)

        mismatched = [r for r in captured
                      if r.needs_review and r.action not in ("SB_POST", "BB_POST")]
        assert mismatched, "expected a needs_review action"


class TestMetadataButtonOverride:
    def test_button_can_be_set_via_event_metadata(self, tmp_path: Path) -> None:
        gs = _make_gs()
        audio_q = make_audio_queue()
        writer = JsonWriter(log_dir=tmp_path, session_id="bs5")
        stop = threading.Event()
        captured: list[ActionRecord] = []

        thread = IntegrationThread(
            audio_queue=audio_q,
            game_state=gs,
            json_writer=writer,
            on_action=captured.append,
            stop_event=stop,
            sb_amount=100,
            bb_amount=200,
        )

        audio_q.put(AudioEvent(
            action="new_hand", amount=0, timestamp=time.time(),
            raw_text="", metadata={"button_seat": 2},
        ))
        _drive(thread, stop)

        bs = thread.betting_state
        assert bs.button_seat == 2
        assert bs.sb_seat == 3
        assert bs.bb_seat == 4
