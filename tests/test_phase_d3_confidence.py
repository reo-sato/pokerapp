"""tests/test_phase_d3_confidence.py

Phase D (#7) D3 — 派生 confidence（3 因子 L/A/Q）+ needs_review 5 条件（ADR-0009 §6）。

- derive_confidence: 合法性ゲート / ソース順位 / whisper スケール / 合意度。
- 閾値条件⑤: rules-aware 経路で低 confidence のクリーンアクションが needs_review になる。

legacy 経路の calc_confidence（固定 8 行）は不変なので本テストの対象外。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from core.event_queue import make_audio_queue
from core.events import AudioEvent
from core.game_state import PlayerState
from core.poker_engine import PokerkitGameState
from integration.engine import REVIEW_THRESHOLD, IntegrationThread, derive_confidence
from output.json_writer import JsonWriter


def _dc(**kw) -> float:
    base = dict(
        apply_ok=True, whisper_conf=1.0, audio_agree=True,
        rfid_present=False, rfid_agree=False, camera_present=False, camera_agree=False,
    )
    base.update(kw)
    return derive_confidence(**base)


class TestDeriveConfidence:
    def test_source_ordering(self):
        camera = _dc(audio_agree=False, camera_present=True, camera_agree=True)
        audio = _dc()
        rfid = _dc(audio_agree=False, rfid_present=True, rfid_agree=True)
        rfid_audio = _dc(rfid_present=True, rfid_agree=True)
        all3 = _dc(rfid_present=True, rfid_agree=True, camera_present=True, camera_agree=True)
        assert camera < audio < rfid < rfid_audio < all3 <= 1.0

    def test_legality_gate_lowers_confidence(self):
        assert _dc(apply_ok=False) < _dc(apply_ok=True)

    def test_whisper_scales_audio(self):
        assert _dc(whisper_conf=0.3) < _dc(whisper_conf=1.0)

    def test_disagreeing_present_source_lowers_agreement(self):
        agree_only = _dc()                                   # audio 一致のみ（A=1）
        with_disagree = _dc(rfid_present=True, rfid_agree=False)  # rfid 存在だが不一致（A=0.5）
        assert with_disagree < agree_only

    def test_clamped_unit_interval(self):
        assert 0.0 <= _dc(rfid_present=True, rfid_agree=True,
                          camera_present=True, camera_agree=True) <= 1.0


def _thread(gs, tmp_path: Path, sid: str):
    cap: list = []
    t = IntegrationThread(
        audio_queue=make_audio_queue(), game_state=gs,
        json_writer=JsonWriter(tmp_path, sid),
        on_action=cap.append, stop_event=threading.Event(),
    )
    return t, cap


def _pk(n: int = 3) -> PokerkitGameState:
    pytest.importorskip("pokerkit")
    gs = PokerkitGameState(
        [PlayerState(seat=i + 1, name=f"P{i + 1}", stack=10000) for i in range(n)],
        sb=100, bb=200,
    )
    gs.new_hand()
    return gs


class TestReviewThresholdCondition:
    def test_low_confidence_clean_action_triggers_review(self, tmp_path: Path):
        # クリーンな call（訂正なし・競合なし・合法）でも低 whisper → 条件⑤で review。
        gs = _pk(3)
        t, cap = _thread(gs, tmp_path, "d3-low")
        t._handle_audio_event(AudioEvent("call", 0, time.time(), "コール", confidence=0.3))
        rec = cap[-1]
        assert rec.action == "call"
        assert rec.confidence < REVIEW_THRESHOLD
        assert rec.needs_review is True

    def test_good_confidence_clean_action_no_review(self, tmp_path: Path):
        gs = _pk(3)
        t, cap = _thread(gs, tmp_path, "d3-good")
        t._handle_audio_event(AudioEvent("call", 0, time.time(), "コール", confidence=0.9))
        rec = cap[-1]
        assert rec.confidence >= REVIEW_THRESHOLD
        assert rec.needs_review is False
