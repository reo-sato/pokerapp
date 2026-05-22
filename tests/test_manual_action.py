"""tests/test_manual_action.py

Phase 5-I: GUI 手動入力 (``ManualActionEvent``) が IntegrationThread を経由して
``BettingState`` / ``GameStateManager`` を音声経路と同等に更新することを検証する。

旧 ``_cmd_manual_action`` は ``GameStateManager.apply_action`` を直叩きしていて:
  - actor_seat 検証なし → 同 seat 連続 raise が通る
  - to_call 補完なし → call 0 がそのまま記録される
  - BettingState を更新しない → street 進行 / current_bet 追跡が壊れる
だったが、Phase 5-I で manual event を IntegrationThread 経由に変更してこれを解消。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from core.event_queue import EventQueue
from core.events import AudioEvent, ManualActionEvent
from core.game_state import GameStateManager, PlayerState
from integration.engine import IntegrationThread
from output.json_writer import JsonWriter


def _build_thread(tmp_path: Path, session_id: str = "manual_test"):
    """3 人卓 (SB=100/BB=200) の IntegrationThread を起動する。"""
    players = [
        PlayerState(seat=1, name="a", stack=10000),
        PlayerState(seat=2, name="b", stack=10000),
        PlayerState(seat=3, name="c", stack=10000),
    ]
    gs = GameStateManager(players=players, sb=100, bb=200)
    audio_q = EventQueue()
    writer = JsonWriter(log_dir=tmp_path, session_id=session_id)
    stop = threading.Event()
    actions: list = []
    thread = IntegrationThread(
        audio_queue=audio_q,
        game_state=gs,
        json_writer=writer,
        stop_event=stop,
        initial_button_seat=3,         # BTN=3, SB=1, BB=2 で start
        sb_amount=100, bb_amount=200,
        auto_post_blinds=True,
        on_action=actions.append,
    )
    return thread, audio_q, stop, writer, gs, actions


def _start_hand_and_wait(thread, audio_q, actions, expected_post_count: int = 2,
                         timeout: float = 2.0) -> None:
    """new_hand を投入し、SB_POST / BB_POST が積まれるまで待つ。"""
    thread.start()
    audio_q.put(AudioEvent("new_hand", 0, time.time(), ""))
    deadline = time.time() + timeout
    while time.time() < deadline:
        if len(actions) >= expected_post_count:
            return
        time.sleep(0.05)
    raise AssertionError(
        f"SB/BB POST が積まれませんでした (actions={[a.action for a in actions]})"
    )


def _wait_for_action_count(actions: list, expected: int,
                            timeout: float = 2.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if len(actions) >= expected:
            return
        time.sleep(0.05)
    raise AssertionError(
        f"アクション数が {expected} に達しませんでした (got {len(actions)}: "
        f"{[(a.seat, a.action, a.amount) for a in actions]})"
    )


# ────────────────────────────────────────────────────────────────────────────
# Phase 5-I: 基本動作
# ────────────────────────────────────────────────────────────────────────────


class TestManualActionBasics:
    def test_manual_event_drives_betting_state(self, tmp_path: Path) -> None:
        """ManualActionEvent が BettingState の current_bet / actor_seat を更新する。"""
        thread, audio_q, stop, _w, gs, actions = _build_thread(tmp_path)
        try:
            _start_hand_and_wait(thread, audio_q, actions)
            # BTN=3, SB=1, BB=2 → preflop first actor は UTG = SB の左隣 (3-handed
            # では UTG=BTN=3)。BettingState.start_hand 経由で SB_POST/BB_POST 後に
            # current_bet=200, is_opened=True, actor_seat=3 が確定している
            bs = thread.betting_state
            assert bs.current_bet == 200
            assert bs.is_opened is True
            assert bs.actor_seat == 3

            # seat 3 が raise to 600 → BettingState が反映するはず
            thread.manual_queue.put(ManualActionEvent(
                seat=3, action="raise", amount=600, timestamp=time.time(),
            ))
            _wait_for_action_count(actions, 3)   # SB_POST + BB_POST + raise

            assert bs.current_bet == 600
            assert bs.last_raise_to == 600
            assert bs.last_aggressor == 3
            # raise 後は次の live seat に actor が進む (3 → 1)
            assert bs.actor_seat == 1
        finally:
            stop.set()
            thread.join(timeout=2.0)

    def test_consecutive_same_seat_raises_flagged_as_review(
        self, tmp_path: Path,
    ) -> None:
        """旧バグ: 同一 seat 連続 raise が通る → 新実装では actor mismatch で
        review_required を立てる (apply はするが warning として残す)。"""
        thread, audio_q, stop, _w, _gs, actions = _build_thread(tmp_path)
        try:
            _start_hand_and_wait(thread, audio_q, actions)
            # seat 3 が raise → actor は seat 1 に進む
            thread.manual_queue.put(ManualActionEvent(
                seat=3, action="raise", amount=600, timestamp=time.time(),
            ))
            _wait_for_action_count(actions, 3)
            assert thread.betting_state.actor_seat == 1

            # 続けて seat 3 が raise (= 旧バグ: そのまま通っていた)
            thread.manual_queue.put(ManualActionEvent(
                seat=3, action="raise", amount=900, timestamp=time.time(),
            ))
            _wait_for_action_count(actions, 4)

            last = actions[-1]
            # actor mismatch なので needs_review=True
            assert last.needs_review is True
            assert last.seat == 3
        finally:
            stop.set()
            thread.join(timeout=2.0)

    def test_call_zero_auto_fills_to_call(self, tmp_path: Path) -> None:
        """旧バグ: ``call 0`` がそのまま記録される → 新実装では to_call が補完される。

        BB=200、seat 3 が UTG なので call は 200 を払うべき。amount=0 で投げると
        自動で 200 になる。
        """
        thread, audio_q, stop, _w, gs, actions = _build_thread(tmp_path)
        try:
            _start_hand_and_wait(thread, audio_q, actions)
            pot_before = gs.pot

            # seat 3 (UTG) が call 0 で投げる → to_call=200 で補完されるはず
            thread.manual_queue.put(ManualActionEvent(
                seat=3, action="call", amount=0, timestamp=time.time(),
            ))
            _wait_for_action_count(actions, 3)
            last = actions[-1]
            assert last.action == "call"
            assert last.amount == 200            # auto-filled
            assert gs.pot == pot_before + 200    # 実際に pot にも 200 入る

        finally:
            stop.set()
            thread.join(timeout=2.0)

    def test_call_zero_with_no_bet_becomes_check(self, tmp_path: Path) -> None:
        """current_bet も自分の contrib も 0 のときは call → check に置き換える。"""
        thread, audio_q, stop, _w, gs, actions = _build_thread(tmp_path)
        try:
            _start_hand_and_wait(thread, audio_q, actions)
            # preflop は current_bet=200 (BB) で 0 にならないので、状況を作るために
            # 全員 call → flop に進める必要があるが、それは重いので unit 的な検証
            # は test_manual_action_event_handler の方に寄せる。
            # ここでは BB option を間接的にテスト: actor を seat 2 (BB) に持ってきて
            # seat 1 (SB) が call → actor=2, current_bet=200, contrib(2)=200 で
            # to_call=0 → BB が call 0 を投げると check 化されるはず

            # seat 3 (UTG) call → actor=1
            thread.manual_queue.put(ManualActionEvent(
                seat=3, action="call", amount=200, timestamp=time.time(),
            ))
            _wait_for_action_count(actions, 3)
            # seat 1 (SB) call → actor=2
            thread.manual_queue.put(ManualActionEvent(
                seat=1, action="call", amount=100, timestamp=time.time(),  # +100
            ))
            _wait_for_action_count(actions, 4)
            # seat 2 (BB) call 0 → check 化されるはず (contrib(2)=200, current_bet=200)
            thread.manual_queue.put(ManualActionEvent(
                seat=2, action="call", amount=0, timestamp=time.time(),
            ))
            _wait_for_action_count(actions, 5)
            last = actions[-1]
            assert last.action == "check"
            assert last.amount == 0
        finally:
            stop.set()
            thread.join(timeout=2.0)


class TestManualActionContractWithGui:
    """Phase 5-I: 旧バグの再現シナリオで「新実装ではきちんと壊れる (= review が立つ)」
    ことを検証する。ユーザーが報告したログ:

      13:59:23  席3 raise 3       (1st raise)
      13:59:35  席3 raise 5       (= 同 seat 連続、旧バグ)
      14:09:55  席1 call  0       (= to_call 補完されてなかった)
      14:09:59  席2 call  0
      14:10:12  席3 bet   7       (= preflop が閉じてないし bet と raise も混乱)

    新実装で:
      - 2 回目の "席3 raise" は actor mismatch で needs_review が立つ
      - "席1 call 0" は to_call で補完される
      - "席2 call 0" も同様
    """

    def test_full_scenario_from_user_log(self, tmp_path: Path) -> None:
        thread, audio_q, stop, _w, gs, actions = _build_thread(tmp_path)
        try:
            _start_hand_and_wait(thread, audio_q, actions)
            bs = thread.betting_state
            # 1. seat 3 raise (これは正規 turn なので review なし)
            thread.manual_queue.put(ManualActionEvent(
                seat=3, action="raise", amount=600, timestamp=time.time(),
            ))
            _wait_for_action_count(actions, 3)
            assert actions[-1].needs_review is False
            assert bs.actor_seat == 1

            # 2. seat 3 が再 raise (= 同 seat 連続: 旧バグ) → review 必須
            thread.manual_queue.put(ManualActionEvent(
                seat=3, action="raise", amount=900, timestamp=time.time(),
            ))
            _wait_for_action_count(actions, 4)
            assert actions[-1].needs_review is True

            # 3. seat 1 call 0 → to_call で補完される (現在 current_bet=900、
            #    contrib(1)=100 (SB) なので to_call=800)
            thread.manual_queue.put(ManualActionEvent(
                seat=1, action="call", amount=0, timestamp=time.time(),
            ))
            _wait_for_action_count(actions, 5)
            assert actions[-1].action == "call"
            assert actions[-1].amount == 800   # 補完済み

        finally:
            stop.set()
            thread.join(timeout=2.0)


class TestManualQueueProperty:
    """``IntegrationThread.manual_queue`` プロパティの基本動作。"""

    def test_manual_queue_property_exists(self, tmp_path: Path) -> None:
        thread, _audio_q, _stop, _w, _gs, _actions = _build_thread(tmp_path)
        # スレッドを起動しなくても property は使える
        q = thread.manual_queue
        assert q is not None
        # put / get_nowait が動く
        q.put(ManualActionEvent(seat=1, action="fold", amount=0, timestamp=0.0))
        ev = q.get_nowait()
        assert ev.seat == 1 and ev.action == "fold"

    def test_explicit_manual_queue_is_used(self, tmp_path: Path) -> None:
        """constructor で manual_queue を渡すと、その instance が使われる。"""
        import queue as q_mod
        players = [PlayerState(seat=1, name="a", stack=10000)]
        gs = GameStateManager(players=players, sb=100, bb=200)
        writer = JsonWriter(log_dir=tmp_path, session_id="explicit_q")
        external_q: EventQueue = q_mod.Queue()
        thread = IntegrationThread(
            audio_queue=EventQueue(),
            game_state=gs, json_writer=writer,
            manual_queue=external_q,
            stop_event=threading.Event(),
        )
        assert thread.manual_queue is external_q
