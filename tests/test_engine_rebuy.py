"""tests/test_engine_rebuy.py

ISSUE-0012: rebuy を GUI/CLI から queue 経由で IntegrationThread に適用する経路。

GameStateManager はロックを持たないため、状態変更は IntegrationThread に一元化する
（従来は GUI スレッドが game_state.rebuy() を直接呼んでおり、apply_action とレースしていた）。
rebuy はポーカーアクションではないため HandSummary.actions には積まれないことも固定する。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from core.event_queue import make_audio_queue
from core.events import AudioEvent
from core.game_state import GameStateManager, PlayerState
from integration.engine import IntegrationThread
from output.json_writer import JsonWriter


def _players(n: int = 2) -> list[PlayerState]:
    return [PlayerState(seat=i + 1, name=f"P{i + 1}", stack=10000) for i in range(n)]


def _thread(tmp_path: Path, captured: list) -> IntegrationThread:
    return IntegrationThread(
        audio_queue=make_audio_queue(),
        game_state=GameStateManager(_players(2), sb=100, bb=200),
        json_writer=JsonWriter(tmp_path, "rebuy-test"),
        stop_event=threading.Event(),
        on_action=captured.append,
    )


def _rebuy_event(seat: int, amount: int) -> AudioEvent:
    return AudioEvent(
        action="rebuy", amount=amount, timestamp=time.time(),
        raw_text=f"シート{seat} リバイ {amount}", seat=seat,
    )


class TestRebuyViaQueue:
    def test_rebuy_applies_stack_and_notifies(self, tmp_path: Path):
        captured: list = []
        thread = _thread(tmp_path, captured)
        gs = thread._game_state

        thread._handle_audio_event(_rebuy_event(1, 5000))

        assert gs.get_stack(1) == 15000
        assert len(captured) == 1
        rec = captured[0]
        assert rec.action == "rebuy"
        assert rec.seat == 1
        assert rec.amount == 5000
        assert rec.stack_after == 15000
        assert rec.needs_review is False

    def test_seat_fallback_from_raw_text(self, tmp_path: Path):
        captured: list = []
        thread = _thread(tmp_path, captured)
        ev = AudioEvent(
            action="rebuy", amount=3000, timestamp=time.time(),
            raw_text="シート2 リバイ 3000",  # seat フィールドなし → raw_text から抽出
        )
        thread._handle_audio_event(ev)
        assert thread._game_state.get_stack(2) == 13000

    def test_unknown_seat_does_not_crash_or_notify(self, tmp_path: Path):
        captured: list = []
        thread = _thread(tmp_path, captured)
        thread._handle_audio_event(_rebuy_event(9, 1000))  # 席 9 は存在しない
        assert captured == []
        assert thread._game_state.get_stack(1) == 10000

    def test_non_positive_amount_rejected(self, tmp_path: Path):
        captured: list = []
        thread = _thread(tmp_path, captured)
        thread._handle_audio_event(_rebuy_event(1, 0))
        assert captured == []
        assert thread._game_state.get_stack(1) == 10000

    def test_rebuy_not_recorded_in_hand_actions(self, tmp_path: Path):
        """rebuy はポーカーアクションではないため HandSummary.actions に積まれない。"""
        hands: list = []
        thread = IntegrationThread(
            audio_queue=make_audio_queue(),
            game_state=GameStateManager(_players(2), sb=100, bb=200),
            json_writer=JsonWriter(tmp_path, "rebuy-hand-test"),
            stop_event=threading.Event(),
            on_hand=hands.append,
        )
        now = time.time()
        thread._handle_audio_event(AudioEvent("new_hand", 0, now, ""))
        thread._handle_audio_event(_rebuy_event(2, 4000))
        thread._handle_audio_event(AudioEvent("winner", 0, now, "シート1 ウィナー"))

        assert len(hands) == 1
        actions = hands[0].to_dict()["actions"]
        assert all(a["action"] != "rebuy" for a in actions)
        # mid-hand rebuy は legacy backend では即時反映（stack_end に含まれる）
        assert thread._game_state.get_stack(2) == 14000
