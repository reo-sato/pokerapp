"""tests/test_phase_d2_wiring.py

Phase D (#7) D2a — rules-aware 経路の結線（apply_corrections ライブ適用 + actor 競合検出）。

- pokerkit backend: 合法手射影が ActionRecord に反映され、明示席の競合が needs_review を立てる。
- legacy backend: 空 legal_context により従来経路（_handle_legacy_action）へ分岐し挙動不変。

silent-fold 合成（fold_through 結線）と派生 confidence（D3）は後続増分のため本テストは対象外。
"""
from __future__ import annotations

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


def _pk(n: int = 3) -> PokerkitGameState:
    pytest.importorskip("pokerkit")
    gs = PokerkitGameState(_players(n), sb=100, bb=200)
    gs.new_hand()
    return gs


class TestRulesAwareWiring:
    def test_call_uses_state_amount_not_heard(self, tmp_path: Path):
        gs = _pk(3)
        t, cap = _thread(gs, tmp_path, "pk1")
        actor = gs.get_current_player()
        t._handle_audio_event(AudioEvent("call", 9999, time.time(), "コール"))
        rec = cap[-1]
        assert rec.seat == actor
        assert rec.action == "call"
        assert rec.amount != 9999 and rec.amount > 0   # heard 無視・状態の call 額
        assert rec.needs_review is False

    def test_check_facing_bet_becomes_call_review(self, tmp_path: Path):
        gs = _pk(3)
        t, cap = _thread(gs, tmp_path, "pk2")
        # プリフロップ先頭 actor は BB に直面 → "check" 非合法 → call へ射影 + review
        t._handle_audio_event(AudioEvent("check", 0, time.time(), "チェック"))
        rec = cap[-1]
        assert rec.action == "call"
        assert rec.needs_review is True

    def test_explicit_seat_conflict_flags_review(self, tmp_path: Path):
        gs = _pk(3)
        t, cap = _thread(gs, tmp_path, "pk3")
        actor = gs.get_current_player()
        other = next(s for s in (1, 2, 3) if s != actor)
        t._handle_audio_event(
            AudioEvent("call", 0, time.time(), f"シート{other} コール", seat=other)
        )
        rec = cap[-1]
        assert rec.seat == actor          # D2a: prior に固定（合成しない）
        assert rec.needs_review is True    # 競合検出

    def test_explicit_seat_match_no_conflict(self, tmp_path: Path):
        gs = _pk(3)
        t, cap = _thread(gs, tmp_path, "pk4")
        actor = gs.get_current_player()
        t._handle_audio_event(
            AudioEvent("call", 0, time.time(), f"シート{actor} コール", seat=actor)
        )
        rec = cap[-1]
        assert rec.seat == actor
        assert rec.needs_review is False


class TestLegacyRoutingUnchanged:
    def test_legacy_records_raw_action(self, tmp_path: Path):
        gs = GameStateManager(_players(2), sb=100, bb=200)
        gs.new_hand()
        t, cap = _thread(gs, tmp_path, "lg1")
        seat = gs.get_current_player()
        t._handle_audio_event(AudioEvent("bet", 500, time.time(), "ベット500"))
        rec = cap[-1]
        assert rec.seat == seat
        assert rec.action == "bet"      # legacy は射影しない（生 action）
        assert rec.amount == 500        # heard そのまま
        assert rec.needs_review is False
