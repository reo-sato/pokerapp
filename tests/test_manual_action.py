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
from core.events import AudioEvent, ManualActionEvent, ManualActionRejection
from core.game_state import GameStateManager, PlayerState
from integration.engine import IntegrationThread
from output.json_writer import JsonWriter


def _build_thread(tmp_path: Path, session_id: str = "manual_test",
                   on_manual_rejected=None):
    """3 人卓 (SB=100/BB=200) の IntegrationThread を起動する。

    Phase 5-J: ``on_manual_rejected`` を渡すと actor mismatch で reject された
    ``ManualActionRejection`` を確認できる。
    """
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
        on_manual_rejected=on_manual_rejected,
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

    def test_consecutive_same_seat_raises_strict_rejected(
        self, tmp_path: Path,
    ) -> None:
        """Phase 5-J: 同一 seat 連続 raise は **strict reject** される。

        旧 Phase 5-I 仕様では actor mismatch でも apply して needs_review=True を
        立てていたが、Phase 5-J で「state を変えずに reject」に変更。
          - ``_current_actions`` に新 record は積まれない (= len 3 のまま)
          - ``BettingState.actor_seat`` / ``current_bet`` / ``last_aggressor`` も
            変わらない
          - ``on_manual_rejected`` callback が rejection 情報付きで発火する
        """
        rejections: list[ManualActionRejection] = []
        thread, audio_q, stop, _w, _gs, actions = _build_thread(
            tmp_path, on_manual_rejected=rejections.append,
        )
        try:
            _start_hand_and_wait(thread, audio_q, actions)
            # seat 3 が raise → actor は seat 1 に進む
            thread.manual_queue.put(ManualActionEvent(
                seat=3, action="raise", amount=600, timestamp=time.time(),
            ))
            _wait_for_action_count(actions, 3)
            bs = thread.betting_state
            assert bs.actor_seat == 1
            assert bs.current_bet == 600
            assert bs.last_aggressor == 3

            # 続けて seat 3 が raise (= actor mismatch) → strict reject
            thread.manual_queue.put(ManualActionEvent(
                seat=3, action="raise", amount=900, timestamp=time.time(),
            ))
            # reject callback が呼ばれるまで待つ
            deadline = time.time() + 1.0
            while time.time() < deadline and not rejections:
                time.sleep(0.05)

            # 1. _current_actions は増えていない (= apply されていない)
            assert len(actions) == 3, (
                f"reject should not append record (got {len(actions)})"
            )
            # 2. BettingState も変化していない
            assert bs.actor_seat == 1
            assert bs.current_bet == 600
            assert bs.last_aggressor == 3
            # 3. rejection callback が actor mismatch 理由で発火している
            assert len(rejections) == 1
            r = rejections[0]
            assert r.seat == 3
            assert r.attempted_action == "raise"
            assert r.attempted_amount == 900
            assert r.expected_actor == 1
            assert r.reason == "actor_mismatch"
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
    """Phase 5-I / 5-J: 旧バグの再現シナリオで新実装が正しく動くことを検証する。
    ユーザーが報告したログ:

      13:59:23  席3 raise 3       (1st raise)
      13:59:35  席3 raise 5       (= 同 seat 連続、旧バグ)
      14:09:55  席1 call  0       (= to_call 補完されてなかった)

    Phase 5-J 仕様で:
      - 1 回目の "席3 raise" は正規 turn なので apply される
      - 2 回目の "席3 raise" は actor mismatch で **strict reject**
        (= action は積まれず BettingState は不変、``on_manual_rejected`` 発火)
      - その後 actor は依然 seat 1 のままなので、"席1 call 0" は正規入力として
        通り、to_call で補完される
    """

    def test_full_scenario_from_user_log(self, tmp_path: Path) -> None:
        rejections: list[ManualActionRejection] = []
        thread, audio_q, stop, _w, gs, actions = _build_thread(
            tmp_path, on_manual_rejected=rejections.append,
        )
        try:
            _start_hand_and_wait(thread, audio_q, actions)
            bs = thread.betting_state
            # 1. seat 3 raise (これは正規 turn なので reject されない)
            thread.manual_queue.put(ManualActionEvent(
                seat=3, action="raise", amount=600, timestamp=time.time(),
            ))
            _wait_for_action_count(actions, 3)
            assert actions[-1].needs_review is False
            assert bs.actor_seat == 1
            assert bs.current_bet == 600

            # 2. seat 3 が再 raise (= actor mismatch) → strict reject
            thread.manual_queue.put(ManualActionEvent(
                seat=3, action="raise", amount=900, timestamp=time.time(),
            ))
            # reject callback の発火を待つ
            deadline = time.time() + 1.0
            while time.time() < deadline and not rejections:
                time.sleep(0.05)
            assert len(actions) == 3   # apply されていない
            assert len(rejections) == 1
            assert rejections[0].reason == "actor_mismatch"
            assert rejections[0].expected_actor == 1
            # BettingState は不変
            assert bs.actor_seat == 1
            assert bs.current_bet == 600

            # 3. seat 1 call 0 → to_call で補完される (current_bet=600、
            #    contrib(1)=100 (SB) なので to_call=500)
            thread.manual_queue.put(ManualActionEvent(
                seat=1, action="call", amount=0, timestamp=time.time(),
            ))
            _wait_for_action_count(actions, 4)
            assert actions[-1].action == "call"
            assert actions[-1].amount == 500   # 補完済み

        finally:
            stop.set()
            thread.join(timeout=2.0)


class TestManualActionStrictReject:
    """Phase 5-J: actor mismatch の manual action は state を変えずに reject する。

    観点別の単体テスト (= シナリオではなく性質ごとに 1 test)。
    """

    def test_reject_does_not_append_record(self, tmp_path: Path) -> None:
        """reject 時 ``_current_actions`` に record が積まれない。"""
        thread, audio_q, stop, _w, _gs, actions = _build_thread(tmp_path)
        try:
            _start_hand_and_wait(thread, audio_q, actions)
            count_before = len(actions)
            # actor は seat 3 (UTG)。seat 1 から action を入れて mismatch
            thread.manual_queue.put(ManualActionEvent(
                seat=1, action="raise", amount=600, timestamp=time.time(),
            ))
            # 少し待っても actions は増えない
            time.sleep(0.3)
            assert len(actions) == count_before
        finally:
            stop.set()
            thread.join(timeout=2.0)

    def test_reject_does_not_mutate_betting_state(self, tmp_path: Path) -> None:
        """reject 時 ``BettingState`` の actor / current_bet / contrib が不変。"""
        thread, audio_q, stop, _w, _gs, actions = _build_thread(tmp_path)
        try:
            _start_hand_and_wait(thread, audio_q, actions)
            bs = thread.betting_state
            actor_before = bs.actor_seat
            current_bet_before = bs.current_bet
            last_aggressor_before = bs.last_aggressor
            contrib_before = dict(bs.player_contrib_this_street)

            # mismatch
            thread.manual_queue.put(ManualActionEvent(
                seat=2, action="call", amount=200, timestamp=time.time(),
            ))
            time.sleep(0.3)

            assert bs.actor_seat == actor_before
            assert bs.current_bet == current_bet_before
            assert bs.last_aggressor == last_aggressor_before
            assert dict(bs.player_contrib_this_street) == contrib_before
        finally:
            stop.set()
            thread.join(timeout=2.0)

    def test_reject_does_not_mutate_game_state(self, tmp_path: Path) -> None:
        """reject 時 ``GameStateManager`` の pot / stack が不変。"""
        thread, audio_q, stop, _w, gs, actions = _build_thread(tmp_path)
        try:
            _start_hand_and_wait(thread, audio_q, actions)
            pot_before = gs.pot
            stacks_before = dict(gs.get_stacks())

            # actor は seat 3。seat 1 で raise を入れて mismatch
            thread.manual_queue.put(ManualActionEvent(
                seat=1, action="raise", amount=800, timestamp=time.time(),
            ))
            time.sleep(0.3)

            assert gs.pot == pot_before
            assert dict(gs.get_stacks()) == stacks_before
        finally:
            stop.set()
            thread.join(timeout=2.0)

    def test_reject_does_not_fire_on_action(self, tmp_path: Path) -> None:
        """reject 時 ``on_action`` callback が呼ばれない。"""
        thread, audio_q, stop, _w, _gs, actions = _build_thread(tmp_path)
        try:
            _start_hand_and_wait(thread, audio_q, actions)
            count_before = len(actions)
            thread.manual_queue.put(ManualActionEvent(
                seat=1, action="fold", amount=0, timestamp=time.time(),
            ))
            time.sleep(0.3)
            # on_action は actions.append にバインドしているので count は不変
            assert len(actions) == count_before
        finally:
            stop.set()
            thread.join(timeout=2.0)

    def test_reject_fires_on_manual_rejected_with_payload(
        self, tmp_path: Path,
    ) -> None:
        """reject 時 ``on_manual_rejected`` が ``ManualActionRejection`` で呼ばれる。"""
        rejections: list[ManualActionRejection] = []
        thread, audio_q, stop, _w, _gs, actions = _build_thread(
            tmp_path, on_manual_rejected=rejections.append,
        )
        try:
            _start_hand_and_wait(thread, audio_q, actions)
            # actor は seat 3 (= UTG)。seat 1 で mismatch を起こす
            ts = time.time()
            thread.manual_queue.put(ManualActionEvent(
                seat=1, action="bet", amount=400, timestamp=ts,
            ))
            deadline = time.time() + 1.0
            while time.time() < deadline and not rejections:
                time.sleep(0.05)
            assert len(rejections) == 1
            r = rejections[0]
            assert isinstance(r, ManualActionRejection)
            assert r.seat == 1
            assert r.attempted_action == "bet"
            assert r.attempted_amount == 400
            assert r.expected_actor == 3
            assert r.reason == "actor_mismatch"
            assert r.timestamp == ts
        finally:
            stop.set()
            thread.join(timeout=2.0)

    def test_actor_match_still_applies(self, tmp_path: Path) -> None:
        """actor 一致時は従来どおり apply され、reject callback は呼ばれない。"""
        rejections: list[ManualActionRejection] = []
        thread, audio_q, stop, _w, gs, actions = _build_thread(
            tmp_path, on_manual_rejected=rejections.append,
        )
        try:
            _start_hand_and_wait(thread, audio_q, actions)
            bs = thread.betting_state
            assert bs.actor_seat == 3
            # 正規 turn (seat 3) の入力 → 通る
            thread.manual_queue.put(ManualActionEvent(
                seat=3, action="raise", amount=600, timestamp=time.time(),
            ))
            _wait_for_action_count(actions, 3)
            assert len(rejections) == 0
            assert actions[-1].seat == 3
            assert actions[-1].action == "raise"
            assert actions[-1].amount == 600
            assert actions[-1].needs_review is False
        finally:
            stop.set()
            thread.join(timeout=2.0)

    def test_uninitialized_betting_state_does_not_reject(
        self, tmp_path: Path,
    ) -> None:
        """``BettingState`` 未初期化のときは actor 判定をスキップ (= 既存仕様)。

        Phase 5-J で扱いを変えないことを明示的に固定する。new_hand 前に manual
        event が来たら reject せず適用する (= 既存挙動)。
        """
        rejections: list[ManualActionRejection] = []
        # 新ハンドを開始しないまま手動入力 → bs.is_initialized=False
        players = [PlayerState(seat=1, name="a", stack=10000)]
        gs = GameStateManager(players=players, sb=100, bb=200)
        writer = JsonWriter(log_dir=tmp_path, session_id="uninit")
        stop = threading.Event()
        actions: list = []
        thread = IntegrationThread(
            audio_queue=EventQueue(),
            game_state=gs, json_writer=writer,
            stop_event=stop,
            on_action=actions.append,
            on_manual_rejected=rejections.append,
        )
        thread.start()
        try:
            # bs.is_initialized=False、actor 判定はスキップされ apply される
            thread.manual_queue.put(ManualActionEvent(
                seat=1, action="fold", amount=0, timestamp=time.time(),
            ))
            _wait_for_action_count(actions, 1)
            assert len(rejections) == 0
            assert actions[-1].seat == 1
            assert actions[-1].action == "fold"
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
