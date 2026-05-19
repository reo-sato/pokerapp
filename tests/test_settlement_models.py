"""tests/test_settlement_models.py

Phase 1 settlement 中心モデルの単体テスト。

6 件:
  1. RevealedHand dataclass の基本構築
  2. PotSettlement の基本構築 + Literal 型受容
  3. HandSummary を新 field 込みで named-arg 構築 / to_dict() に新 5 field
  4. HandSummary 新 field 省略時の default 値 (final / None / 空 / 空)
  5. PHH 出力 gate: resolution_status != "final" で空文字
  6. _finalize_hand 経由 E2E: resolution_type == "legacy_winner_finalize" + pots=[] + seat_payouts
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import pytest

from core.event_queue import EventQueue
from core.events import AudioEvent
from core.game_state import GameStateManager, PlayerState
from core.hand_log import (
    ActionRecord,
    HandSummary,
    PotSettlement,
    RevealedHand,
)
from integration.engine import IntegrationThread
from output.json_writer import JsonWriter
from output.phh_exporter import PHHExporter


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────


def _minimal_summary(**overrides) -> HandSummary:
    base = dict(
        hand_id=1,
        session_id="s",
        started_at="2026-01-01T00:00:00",
        ended_at="2026-01-01T00:01:00",
        blinds={"sb": 100, "bb": 200},
        board=[],
        board_source="",
        players=[],
        pot_total=600,
        winner_seat=1,
        actions=[],
        review_required=False,
    )
    base.update(overrides)
    return HandSummary(**base)


# ────────────────────────────────────────────────────────────────────────────
# 1. RevealedHand dataclass の基本構築
# ────────────────────────────────────────────────────────────────────────────


def test_revealed_hand_construction() -> None:
    rh = RevealedHand(seat=3, cards=["Ah", "Kd"], source="rfid", observed_at=1000.5)
    assert rh.seat == 3
    assert rh.cards == ["Ah", "Kd"]
    assert rh.source == "rfid"
    assert rh.observed_at == 1000.5
    # observed_at は Optional (省略可)
    rh2 = RevealedHand(seat=1, cards=["Ts", "9c"], source="manual")
    assert rh2.observed_at is None
    # to_dict は dataclass 全 field を含む
    d = rh.to_dict()
    assert d["seat"] == 3
    assert d["source"] == "rfid"
    assert d["cards"] == ["Ah", "Kd"]


# ────────────────────────────────────────────────────────────────────────────
# 2. PotSettlement の基本構築 + Literal 型受容
# ────────────────────────────────────────────────────────────────────────────


def test_pot_settlement_construction() -> None:
    p = PotSettlement(
        amount=1000,
        eligible_seats=[1, 2, 3],
        winning_seats=[1],
        payouts={1: 1000},
        pot_type="main",
    )
    assert p.amount == 1000
    assert p.eligible_seats == [1, 2, 3]
    assert p.winning_seats == [1]
    assert p.payouts == {1: 1000}
    assert p.pot_type == "main"
    # default pot_type は "main"
    p2 = PotSettlement(amount=500, eligible_seats=[1, 2], winning_seats=[1, 2], payouts={1: 250, 2: 250})
    assert p2.pot_type == "main"
    # "side" も受け入れる (Literal の制約は runtime チェックされないが、値として保持できる)
    p3 = PotSettlement(amount=200, eligible_seats=[1], winning_seats=[1], payouts={1: 200}, pot_type="side")
    assert p3.pot_type == "side"
    # to_dict
    d = p.to_dict()
    assert d["amount"] == 1000
    assert d["pot_type"] == "main"


# ────────────────────────────────────────────────────────────────────────────
# 3. HandSummary に新 5 field が載り to_dict() で出力される
# ────────────────────────────────────────────────────────────────────────────


def test_hand_summary_with_new_fields() -> None:
    pots = [
        PotSettlement(amount=600, eligible_seats=[1, 2], winning_seats=[1], payouts={1: 600}),
    ]
    summary = _minimal_summary(
        resolution_status="final",
        resolution_type="showdown",
        seat_payouts={1: 600},
        showdown_revealed_cards={1: ["Ah", "Kd"], 2: ["7c", "7s"]},
        pots=pots,
    )
    d = summary.to_dict()
    assert d["resolution_status"] == "final"
    assert d["resolution_type"] == "showdown"
    assert d["seat_payouts"] == {1: 600}
    assert d["showdown_revealed_cards"] == {1: ["Ah", "Kd"], 2: ["7c", "7s"]}
    assert d["pots"] == [
        {
            "amount": 600,
            "eligible_seats": [1, 2],
            "winning_seats": [1],
            "payouts": {1: 600},
            "pot_type": "main",
        }
    ]
    # 既存 field と共存している
    assert d["winner_seat"] == 1
    assert d["pot_total"] == 600


# ────────────────────────────────────────────────────────────────────────────
# 4. 新 field を省略すると default は (final / None / 空 / 空 / 空)
# ────────────────────────────────────────────────────────────────────────────


def test_hand_summary_default_resolution_final() -> None:
    summary = _minimal_summary()
    assert summary.resolution_status == "final"
    assert summary.resolution_type is None
    assert summary.seat_payouts == {}
    assert summary.showdown_revealed_cards == {}
    assert summary.pots == []
    d = summary.to_dict()
    assert d["resolution_status"] == "final"
    assert d["resolution_type"] is None
    assert d["pots"] == []


# ────────────────────────────────────────────────────────────────────────────
# 5. PHH 出力 gate: resolution_status != "final" で skip (空文字、intentional log)
# ────────────────────────────────────────────────────────────────────────────


def test_phh_gate_skips_non_final(caplog) -> None:
    exporter = PHHExporter(author="test")
    # provisional → skip
    summary = _minimal_summary(
        hand_id=42,
        resolution_status="provisional",
        players=[{"seat": 1, "name": "A", "stack_start": 10000}],
    )
    with caplog.at_level(logging.INFO):
        out = exporter.export(summary)
    assert out == ""
    # ログに intentional skip の記述
    assert any(
        "PHH export skipped" in rec.message and "intentional" in rec.message
        for rec in caplog.records
    )
    # incomplete も skip
    caplog.clear()
    summary2 = _minimal_summary(
        hand_id=43,
        resolution_status="incomplete",
        players=[{"seat": 1, "name": "A", "stack_start": 10000}],
    )
    assert exporter.export(summary2) == ""
    # final なら通常出力
    summary3 = _minimal_summary(
        hand_id=44,
        resolution_status="final",
        players=[{"seat": 1, "name": "A", "stack_start": 10000}],
    )
    out3 = exporter.export(summary3)
    assert out3 != ""
    assert "variant" in out3


# ────────────────────────────────────────────────────────────────────────────
# 6. _finalize_hand 経由 E2E: legacy 経路で settlement field が正しく立つ
# ────────────────────────────────────────────────────────────────────────────


def test_legacy_finalize_sets_resolution_type(tmp_path: Path) -> None:
    """_finalize_hand 経由で生成された HandSummary が:
      - resolution_status == "final"
      - resolution_type == "legacy_winner_finalize"
      - seat_payouts == {winner_seat: pot_total}
      - pots == [] (Phase 1 では意図的に空)
    """
    players = [
        PlayerState(seat=1, name="A", stack=10000),
        PlayerState(seat=2, name="B", stack=10000),
        PlayerState(seat=3, name="C", stack=10000),
    ]
    gs = GameStateManager(players=players, sb=100, bb=200)
    audio_q = EventQueue()
    writer = JsonWriter(log_dir=tmp_path, session_id="phase1_e2e")
    stop = threading.Event()
    thread = IntegrationThread(
        audio_queue=audio_q,
        game_state=gs,
        json_writer=writer,
        stop_event=stop,
        initial_button_seat=3,
        auto_post_blinds=True,
    )

    now = time.time()
    audio_q.put(AudioEvent("new_hand", 0, now, ""))
    audio_q.put(AudioEvent("fold", 0, now + 0.5, "フォールド"))
    audio_q.put(AudioEvent("winner", 0, now + 1.0, "シート2 ウィナー"))

    thread.start()
    time.sleep(0.8)
    stop.set()
    thread.join(timeout=2.0)

    # JSON に書かれた summary を読み戻して検証
    import json
    json_path = tmp_path / "phase1_e2e.json"
    assert json_path.exists()
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert len(data["hands"]) == 1
    h = data["hands"][0]
    assert h["resolution_status"] == "final"
    assert h["resolution_type"] == "legacy_winner_finalize"
    # winner_seat は文字列 "シート2 ウィナー" から抽出した 2
    assert h["winner_seat"] == 2
    # seat_payouts は {winner_seat: pot_total}
    assert int(next(iter(h["seat_payouts"].keys()))) == 2
    assert sum(h["seat_payouts"].values()) == h["pot_total"]
    # pots は Phase 1 では空のまま
    assert h["pots"] == []
    # showdown_revealed_cards も空
    assert h["showdown_revealed_cards"] == {}
