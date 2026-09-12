"""tests/test_phase_g_default.py

Phase G (#9) — pokerkit を live 既定 backend に切替。

- config_default.json / fresh install の既定が pokerkit。
- legacy は config で rollback 選択可（挙動温存）。
- create_game_state は明示 backend で両実装を返す。

ルール準拠の挙動自体は golden fixtures（tests/test_reconstruction.py）が担保。本テストは
「既定切替」と「rollback 可能性」を固定する。実機 E2E（音声→JSON/PHH）は Phase H で検証。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.config import load_config
from core.game_state import GameStateManager, PlayerState
from core.poker_engine import create_game_state

_ROOT = Path(__file__).parent.parent


def _players() -> list[PlayerState]:
    return [PlayerState(seat=i + 1, name=f"P{i + 1}", stack=10000) for i in range(3)]


def test_config_default_backend_is_pokerkit():
    cfg = json.loads((_ROOT / "config_default.json").read_text(encoding="utf-8"))
    assert cfg["engine"]["backend"] == "pokerkit"


def test_fresh_install_defaults_to_pokerkit(tmp_path: Path):
    # config.json 不在 → config_default.json をコピー → 既定 pokerkit
    cfg = load_config(tmp_path / "config.json")
    assert cfg["engine"]["backend"] == "pokerkit"


def test_legacy_rollback_still_selectable():
    # 明示 legacy で従来 engine に rollback できる
    gs = create_game_state("legacy", _players(), 100, 200)
    assert isinstance(gs, GameStateManager)


def test_pokerkit_backend_constructs_and_plays():
    pytest.importorskip("pokerkit")
    from core.poker_engine import PokerkitGameState

    gs = create_game_state("pokerkit", _players(), 100, 200)
    assert isinstance(gs, PokerkitGameState)
    gs.new_hand()
    assert gs.get_current_player() in (1, 2, 3)
    # rules-aware の合法手が出る（legacy stub は空）
    assert gs.legal_context().legal_actions
