"""tests/test_no_active_hand_guard.py

ISSUE-0028: 進行中のハンドが無いときに届いたアクション / winner 宣言でクラッシュしない。

pokerkit backend は新ハンド前・ハンド終了後に actor を持たず `get_current_player()` が
`RuntimeError` を投げる。`legal_context()` も空になるため dispatch は legacy 経路へ落ち、
そこで例外 → traceback（イベントは黙って捨てられる）になっていた。CLAUDE.md のエラー
ハンドリング方針（認識エラーでクラッシュしない）に合わせ、案内ログを出して落とす。
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import pytest

from core.event_queue import make_audio_queue
from core.events import AudioEvent
from core.game_state import GameStateManager, PlayerState
from core.poker_engine import PokerkitGameState
from integration.engine import IntegrationThread
from output.json_writer import JsonWriter


def _players(n: int) -> list[PlayerState]:
    return [PlayerState(seat=i + 1, name=f"P{i + 1}", stack=10000) for i in range(n)]


def _thread(gs, tmp_path: Path, sid: str):
    captured: list = []
    t = IntegrationThread(
        audio_queue=make_audio_queue(), game_state=gs,
        json_writer=JsonWriter(tmp_path, sid),
        on_action=captured.append, stop_event=threading.Event(),
    )
    return t, captured


def _pk(n: int = 3, *, start: bool = False) -> PokerkitGameState:
    pytest.importorskip("pokerkit")
    gs = PokerkitGameState(_players(n), sb=100, bb=200)
    if start:
        gs.new_hand()
    return gs


class TestActionWithoutActiveHand:
    """新ハンド前（`n` を押していない）のアクション。"""

    def test_action_before_new_hand_does_not_raise(self, tmp_path: Path):
        gs = _pk()
        t, cap = _thread(gs, tmp_path, "guard1")
        t._handle_audio_event(AudioEvent("check", 0, time.time(), "ちぇっく"))  # noqa: SLF001
        assert cap == []          # 記録は作らない（手番が無いので付け先がない）

    def test_action_before_new_hand_warns_with_next_step(self, tmp_path: Path, caplog):
        gs = _pk()
        t, _ = _thread(gs, tmp_path, "guard2")
        with caplog.at_level(logging.WARNING, logger="integration.engine"):
            t._handle_audio_event(AudioEvent("bet", 500, time.time(), "ベット500"))  # noqa: SLF001
        assert "新ハンド" in caplog.text     # 次にやる操作を案内する
        assert "bet" in caplog.text          # 捨てたアクションが分かる

    def test_action_after_hand_end_does_not_raise(self, tmp_path: Path):
        gs = _pk(start=True)
        gs.end_hand(1)
        t, cap = _thread(gs, tmp_path, "guard3")
        t._handle_audio_event(AudioEvent("call", 0, time.time(), "コール"))  # noqa: SLF001
        assert cap == []

    def test_new_hand_after_dropped_action_works(self, tmp_path: Path):
        """落とした後でも `n` → アクションで通常どおり進む（状態を壊さない）。"""
        gs = _pk()
        t, cap = _thread(gs, tmp_path, "guard4")
        t._handle_audio_event(AudioEvent("check", 0, time.time(), "チェック"))  # noqa: SLF001
        t._handle_audio_event(AudioEvent("new_hand", 0, time.time(), "ハンド開始"))  # noqa: SLF001
        t._handle_audio_event(AudioEvent("call", 0, time.time(), "コール"))  # noqa: SLF001
        assert [r.action for r in cap] == ["call"]

    def test_legacy_backend_is_unaffected(self, tmp_path: Path):
        """legacy は手番を常に持つ = 従来どおり記録される（挙動不変）。"""
        gs = GameStateManager(_players(3), sb=100, bb=200)
        t, cap = _thread(gs, tmp_path, "guard5")
        t._handle_audio_event(AudioEvent("check", 0, time.time(), "チェック"))  # noqa: SLF001
        assert len(cap) == 1 and cap[-1].action == "check"


class TestWinnerWithoutActiveHand:
    def test_winner_before_new_hand_does_not_raise(self, tmp_path: Path, caplog):
        gs = _pk()
        t, _ = _thread(gs, tmp_path, "win1")
        with caplog.at_level(logging.WARNING, logger="integration.engine"):
            t._handle_audio_event(AudioEvent("winner", 0, time.time(), "シート1 ウィナー"))  # noqa: SLF001
        assert "新ハンド" in caplog.text
        assert list(tmp_path.glob("*.json")) == []   # ハンドを書き出さない

    def test_double_winner_does_not_pay_twice(self, tmp_path: Path):
        """確定済みハンドへの再宣言はポットを二重加算しない（黙ったデータ破損を防ぐ）。"""
        gs = _pk(start=True)
        t, _ = _thread(gs, tmp_path, "win2")
        t._handle_audio_event(AudioEvent("winner", 0, time.time(), "シート1 ウィナー"))  # noqa: SLF001
        after_first = gs.get_stacks()
        t._handle_audio_event(AudioEvent("winner", 0, time.time(), "シート1 ウィナー"))  # noqa: SLF001
        assert gs.get_stacks() == after_first

    def test_winner_without_seat_uses_current_actor(self, tmp_path: Path):
        """席を言わなかった場合の従来フォールバック（手番がある間）は不変。"""
        gs = _pk(start=True)
        actor = gs.get_current_player()
        t, _ = _thread(gs, tmp_path, "win3")
        t._handle_audio_event(AudioEvent("winner", 0, time.time(), "ウィナー"))  # noqa: SLF001
        assert gs.get_stacks()[actor] > 10000        # actor がポットを取った


class TestIsHandActive:
    def test_pokerkit_tracks_hand_lifecycle(self):
        gs = _pk()
        assert gs.is_hand_active() is False
        gs.new_hand()
        assert gs.is_hand_active() is True
        gs.end_hand(1)
        assert gs.is_hand_active() is False

    def test_legacy_always_active(self):
        gs = GameStateManager(_players(3), sb=100, bb=200)
        assert gs.is_hand_active() is True
