"""tests/test_integration_button.py

IntegrationThread に button_seat / 自動 blind post / 自動回転が組み込まれて
いることを end-to-end で検証する。
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


def _make_gs(seats: int = 6, stack: int = 10000) -> GameStateManager:
    players = [PlayerState(seat=i, name=f"P{i}", stack=stack) for i in range(1, seats + 1)]
    return GameStateManager(players=players, sb=100, bb=200)


def _drive(thread: IntegrationThread, stop: threading.Event, settle: float = 0.4) -> None:
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
        audio_q.put(AudioEvent(action="new_hand", amount=0,
                               timestamp=time.time(), raw_text=""))
        _drive(thread, stop)

        labels = [(r.seat, r.action, r.amount) for r in captured]
        assert (2, "SB_POST", 100) in labels
        assert (3, "BB_POST", 200) in labels
        assert gs.pot == 300
        bs = thread.betting_state
        assert bs.is_initialized
        assert bs.current_bet == 200
        assert bs.actor_seat == 4

    def test_button_unset_falls_back(self, tmp_path: Path) -> None:
        gs = _make_gs()
        gs.new_hand()  # 既存テストと同じ手順 (button 不要)
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
        audio_q.put(AudioEvent(action="bet", amount=500,
                               timestamp=time.time(), raw_text="ベット 500"))
        _drive(thread, stop)

        assert len(captured) == 1
        assert captured[0].action == "bet"
        assert captured[0].amount == 500
        assert thread.betting_state.is_initialized is False


class TestSequentialRotation:
    """Case 2: 初回 button=1 → 次ハンドで button=2, SB=3, BB=4, first_actor=5。"""

    def test_full_rotation_state(self, tmp_path: Path) -> None:
        gs = _make_gs(seats=6)
        audio_q = make_audio_queue()
        writer = JsonWriter(log_dir=tmp_path, session_id="seq")
        stop = threading.Event()

        thread = IntegrationThread(
            audio_queue=audio_q,
            game_state=gs,
            json_writer=writer,
            stop_event=stop,
            initial_button_seat=1,
            sb_amount=100,
            bb_amount=200,
        )
        ts = time.time()
        audio_q.put(AudioEvent(action="new_hand", amount=0, timestamp=ts, raw_text=""))
        audio_q.put(AudioEvent(action="new_hand", amount=0, timestamp=ts + 0.1, raw_text=""))
        _drive(thread, stop, settle=0.6)

        bs = thread.betting_state
        assert bs.button_seat == 2
        assert bs.sb_seat == 3
        assert bs.bb_seat == 4
        assert bs.actor_seat == 5


class TestButtonAutoAdvance:
    def _run_two_hands(self, stacks: dict[int, int], initial_btn: int,
                       tmp_path: Path) -> IntegrationThread:
        players = [PlayerState(seat=s, name=f"P{s}", stack=st)
                   for s, st in stacks.items()]
        gs = GameStateManager(players=players, sb=100, bb=200)
        audio_q = make_audio_queue()
        writer = JsonWriter(log_dir=tmp_path, session_id="rot")
        stop = threading.Event()

        thread = IntegrationThread(
            audio_queue=audio_q,
            game_state=gs,
            json_writer=writer,
            stop_event=stop,
            initial_button_seat=initial_btn,
            sb_amount=100,
            bb_amount=200,
        )
        ts = time.time()
        audio_q.put(AudioEvent(action="new_hand", amount=0, timestamp=ts, raw_text=""))
        audio_q.put(AudioEvent(action="new_hand", amount=0, timestamp=ts + 0.1, raw_text=""))
        _drive(thread, stop, settle=0.5)
        return thread

    def test_dense_seats_advance_by_one(self, tmp_path: Path) -> None:
        thread = self._run_two_hands(
            {i: 10000 for i in range(1, 7)}, initial_btn=1, tmp_path=tmp_path,
        )
        assert thread.betting_state.button_seat == 2

    def test_skips_stack_zero(self, tmp_path: Path) -> None:
        stacks = {1: 10000, 2: 0, 3: 10000, 4: 0, 5: 10000, 6: 10000}
        thread = self._run_two_hands(stacks, initial_btn=1, tmp_path=tmp_path)
        assert thread.betting_state.button_seat == 3

    def test_wraps_around(self, tmp_path: Path) -> None:
        stacks = {2: 10000, 4: 10000, 6: 10000, 8: 10000}
        thread = self._run_two_hands(stacks, initial_btn=8, tmp_path=tmp_path)
        assert thread.betting_state.button_seat == 2


class TestManualOverrideOneShot:
    def test_override_then_auto_advance(self, tmp_path: Path) -> None:
        gs = _make_gs(seats=6)
        audio_q = make_audio_queue()
        writer = JsonWriter(log_dir=tmp_path, session_id="ov")
        stop = threading.Event()

        thread = IntegrationThread(
            audio_queue=audio_q,
            game_state=gs,
            json_writer=writer,
            stop_event=stop,
            initial_button_seat=1,
            sb_amount=100,
            bb_amount=200,
        )
        # ハンド 1: 初期 button=1
        # ハンド 2: 手動補正で 5
        # ハンド 3: 自動で 6
        thread.start()
        time.sleep(0.05)

        ts = time.time()
        audio_q.put(AudioEvent(action="new_hand", amount=0, timestamp=ts, raw_text=""))
        time.sleep(0.15)
        thread.set_next_button_seat(5)
        audio_q.put(AudioEvent(action="new_hand", amount=0, timestamp=ts + 0.2, raw_text=""))
        time.sleep(0.15)
        audio_q.put(AudioEvent(action="new_hand", amount=0, timestamp=ts + 0.3, raw_text=""))
        time.sleep(0.3)
        stop.set()
        thread.join(timeout=2.0)

        assert thread.betting_state.button_seat == 6
