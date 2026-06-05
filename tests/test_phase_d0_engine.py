"""tests/test_phase_d0_engine.py

Phase D (#7) D0 — PokerEngine 境界の rules-aware additive メソッド。

- pokerkit: fold_through による silent-fold 合成 / legal_context / is_legal_actor
  （pokerkit 未導入なら _pk() が skip）。
- legacy: rules-aware でない stub（空 legal_context / fold_through 未対応）— 常に実行。
- 両 backend が PokerEngine Protocol に conform する。
"""
from __future__ import annotations

import pytest

from core.engine_types import LegalContext
from core.game_state import GameStateManager, PlayerState
from core.poker_engine import PokerEngine, PokerkitGameState


def _players(n: int) -> list[PlayerState]:
    return [PlayerState(seat=i + 1, name=f"P{i + 1}", stack=10000) for i in range(n)]


def _pk(n: int = 3) -> PokerkitGameState:
    pytest.importorskip("pokerkit")
    return PokerkitGameState(_players(n), sb=100, bb=200)


# ――― pokerkit backend（skip-able）―――

class TestFoldThrough:
    def test_noop_when_already_actor(self):
        gs = _pk(3)
        gs.new_hand()
        a0 = gs.get_current_player()
        gs.fold_through(a0)
        assert gs.get_current_player() == a0
        assert len(gs.get_active_seats()) == 3

    def test_synthesizes_intermediate_folds(self):
        gs = _pk(3)
        gs.new_hand()
        a0 = gs.get_current_player()
        # 通常 fold で「次の actor」を知る（button 固定なので新ハンドでも同順）。
        gs.apply_action(a0, "fold", 0)
        a1 = gs.get_current_player()
        assert a1 != a0

        gs.new_hand()
        assert gs.get_current_player() == a0
        gs.fold_through(a1)
        assert gs.get_current_player() == a1
        assert gs.is_legal_actor(a1)
        assert a0 not in gs.get_active_seats()

    def test_unknown_seat_raises(self):
        gs = _pk(3)
        gs.new_hand()
        with pytest.raises(ValueError):
            gs.fold_through(99)

    def test_no_active_hand_raises(self):
        gs = _pk(3)
        with pytest.raises(ValueError):
            gs.fold_through(1)


class TestLegalContextPokerkit:
    def test_preflop_actor_has_legal_actions(self):
        gs = _pk(3)
        gs.new_hand()
        ctx = gs.legal_context()
        assert isinstance(ctx, LegalContext)
        assert ctx.actor_seat == gs.get_current_player()
        assert "fold" in ctx.legal_actions
        # プリフロップ UTG は BB に直面しているので call が合法。
        assert "call" in ctx.legal_actions
        assert ctx.amount_to_call > 0

    def test_pokerkit_is_poker_engine(self):
        assert isinstance(_pk(2), PokerEngine)


# ――― legacy backend（常に実行）―――

class TestLegacyStubs:
    def _legacy(self) -> GameStateManager:
        gs = GameStateManager(_players(3), sb=100, bb=200)
        gs.new_hand()
        return gs

    def test_legal_context_is_empty(self):
        ctx = self._legacy().legal_context()
        assert isinstance(ctx, LegalContext)
        assert ctx.legal_actions == frozenset()
        assert ctx.actor_seat is None

    def test_is_legal_actor_tracks_current_player(self):
        gs = self._legacy()
        assert gs.is_legal_actor(gs.get_current_player()) is True

    def test_fold_through_not_supported(self):
        with pytest.raises(NotImplementedError):
            self._legacy().fold_through(1)

    def test_pots_and_committed_defaults(self):
        gs = self._legacy()
        assert gs.pots() == []
        assert gs.committed(1) == 0

    def test_legacy_is_poker_engine(self):
        assert isinstance(GameStateManager(_players(2), 100, 200), PokerEngine)
