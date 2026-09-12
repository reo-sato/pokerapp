"""tests/test_action_street_label.py

ISSUE-0029: ActionRecord.street は「そのアクションが行われたストリート」。

pokerkit backend はベッティングラウンドが閉じると `apply_action` の中で次ストリートへ
自動進行する。`street` を適用**後**に読むと、ラウンドを閉じたアクション（BB のチェック、
最後のコール、fold）が次ストリートに記録され、ハンド履歴 / PHH が実際と食い違う。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from core.event_queue import make_audio_queue
from core.events import AudioEvent
from core.game_state import GameStateManager, PlayerState, Street
from core.poker_engine import PokerkitGameState
from integration.engine import IntegrationThread
from output.json_writer import JsonWriter


def _thread(gs, tmp_path: Path, sid: str):
    captured: list = []
    t = IntegrationThread(
        audio_queue=make_audio_queue(), game_state=gs,
        json_writer=JsonWriter(tmp_path, sid),
        on_action=captured.append, stop_event=threading.Event(),
    )
    return t, captured


def _send(t, action: str, amount: int = 0) -> None:
    t._handle_audio_event(AudioEvent(action, amount, time.time(), action))  # noqa: SLF001


class TestStreetIsCapturedBeforeApply:
    def test_heads_up_hand_labels_each_street_correctly(self, tmp_path: Path):
        """実機 2026-09-12 と同じ手順（SB call → BB check → flop bet/call → turn bet/fold）。"""
        pytest.importorskip("pokerkit")
        gs = PokerkitGameState(
            [PlayerState(1, "a", 100), PlayerState(2, "b", 100)], sb=1, bb=2
        )
        t, cap = _thread(gs, tmp_path, "street1")
        _send(t, "new_hand")
        for action, amount in [("check", 0), ("check", 0), ("bet", 5),
                               ("call", 0), ("bet", 50), ("fold", 0)]:
            _send(t, action, amount)

        # 1 件目の check は preflop で非合法 → call へ射影（合法手射影, ADR-0009）。
        assert [(r.street, r.action) for r in cap] == [
            ("preflop", "call"),    # SB がコール
            ("preflop", "check"),   # BB のチェックが preflop を閉じる ← ここが flop になっていた
            ("flop", "bet"),
            ("flop", "call"),       # flop を閉じるコール ← turn になっていた
            ("turn", "bet"),
            ("turn", "fold"),       # ハンドを終わらせる fold ← showdown になっていた
        ]

    def test_round_closing_action_is_not_labeled_next_street(self, tmp_path: Path):
        """最小形: ラウンドを閉じた瞬間に street が進んでも、記録は行われたストリート。"""
        pytest.importorskip("pokerkit")
        gs = PokerkitGameState(
            [PlayerState(1, "a", 100), PlayerState(2, "b", 100)], sb=1, bb=2
        )
        t, cap = _thread(gs, tmp_path, "street2")
        _send(t, "new_hand")
        _send(t, "call")
        _send(t, "check")
        assert gs.street == "flop"          # backend は先へ進んでいる
        assert cap[-1].street == "preflop"  # 記録は行われたストリートのまま

    def test_legacy_backend_is_unaffected(self, tmp_path: Path):
        """legacy は apply_action でストリートが動かないので従来と同じ値（挙動不変）。"""
        gs = GameStateManager(
            [PlayerState(i + 1, f"P{i + 1}", 100) for i in range(3)], sb=1, bb=2
        )
        gs.advance_street(Street.FLOP)
        t, cap = _thread(gs, tmp_path, "street3")
        _send(t, "check")
        assert cap[-1].street == "flop"
