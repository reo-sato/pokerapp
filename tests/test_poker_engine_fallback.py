"""tests/test_poker_engine_fallback.py

create_game_state の pokerkit フォールバック (review hardening)。

既定 backend が pokerkit (Phase G) のため、pokerkit 未導入環境で起動がクラッシュ
しないよう legacy GameStateManager へ自動フォールバックすることを固定する。
"""
from __future__ import annotations

import sys

from core.game_state import GameStateManager, PlayerState
from core.poker_engine import create_game_state


def _players() -> list[PlayerState]:
    return [PlayerState(seat=1, name="A", stack=10000),
            PlayerState(seat=2, name="B", stack=10000)]


def test_pokerkit_missing_falls_back_to_legacy(monkeypatch):
    # sys.modules[name] = None で `from pokerkit import ...` が ImportError になる
    monkeypatch.setitem(sys.modules, "pokerkit", None)
    gs = create_game_state("pokerkit", _players(), sb=100, bb=200)
    assert isinstance(gs, GameStateManager)
    # フォールバック後も通常運転できる（rules-aware は無効 = 空 legal_context）
    gs.new_hand()
    assert gs.legal_context().legal_actions == frozenset()


def test_legacy_backend_unaffected(monkeypatch):
    monkeypatch.setitem(sys.modules, "pokerkit", None)
    gs = create_game_state("legacy", _players(), sb=100, bb=200)
    assert isinstance(gs, GameStateManager)
