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


class TestButtonAutoAdvance:
    """各ハンド開始時に button が前ハンドの次の active seat へ自動移動する。"""

    def _run_two_hands(self, stacks: dict[int, int], initial_btn: int,
                       tmp_path: Path) -> "IntegrationThread":
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
        # active=[1..6], initial=1 → 2 ハンド目で button=2
        thread = self._run_two_hands(
            {i: 10000 for i in range(1, 7)}, initial_btn=1, tmp_path=tmp_path,
        )
        assert thread.betting_state.button_seat == 2

    def test_skips_empty_seats(self, tmp_path: Path) -> None:
        # active=[1,3,5,6] (stack=0 を欠席扱い), initial=1 → next=3
        stacks = {1: 10000, 2: 0, 3: 10000, 4: 0, 5: 10000, 6: 10000}
        thread = self._run_two_hands(stacks, initial_btn=1, tmp_path=tmp_path)
        assert thread.betting_state.button_seat == 3

    def test_wraps_around(self, tmp_path: Path) -> None:
        # active=[2,4,6,8], initial=8 → next=2
        stacks = {2: 10000, 4: 10000, 6: 10000, 8: 10000}
        thread = self._run_two_hands(stacks, initial_btn=8, tmp_path=tmp_path)
        assert thread.betting_state.button_seat == 2


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
        assert bs.actor_seat == 5  # preflop first actor


class TestDuplicateBlindReview:
    def test_voice_bet_same_as_bb_flags_review(self, tmp_path: Path) -> None:
        gs = _make_gs(seats=6)
        audio_q = make_audio_queue()
        writer = JsonWriter(log_dir=tmp_path, session_id="dup")
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
        # BB (seat 3) が "ベット 200" を発話 = 既に自動ポスト済みの blind 額と同額
        audio_q.put(AudioEvent(
            action="bet", amount=200, timestamp=ts + 0.05,
            raw_text="シート3 ベット 200",
        ))
        _drive(thread, stop, settle=0.5)

        bb_voice = [r for r in captured
                    if r.seat == 3 and r.action != "BB_POST" and r.action != "SB_POST"]
        assert bb_voice, "expected the voice action to be recorded"
        assert bb_voice[0].needs_review is True


class TestManualOverrideOneShot:
    """set_next_button_seat() は 1 回適用したら自動進行に戻る。"""

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
        # 1 ハンド目: initial=1
        # 2 ハンド目: 手動で 5 に補正
        # 3 ハンド目: 自動で 6 へ進むはず
        ts = time.time()
        audio_q.put(AudioEvent(action="new_hand", amount=0, timestamp=ts, raw_text=""))
        audio_q.put(AudioEvent(
            action="new_hand", amount=0, timestamp=ts + 0.1, raw_text="",
            metadata={"button_seat": 5},
        ))
        audio_q.put(AudioEvent(action="new_hand", amount=0, timestamp=ts + 0.2, raw_text=""))
        _drive(thread, stop, settle=0.7)

        assert thread.betting_state.button_seat == 6


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
