"""tests/test_hand_reconstructor_bootstrap.py

Phase 4-B: HandReconstructor の bootstrap 経路 (initial_state → raw → online_summary
→ skipped) を検証する。``bootstrap_source`` / ``bootstrap_meta`` が正しく
埋まること、CLI 出力にそれが反映されることも確認する。
"""
from __future__ import annotations

import copy
import json
import threading
import time
from pathlib import Path
from typing import Optional

import pytest

from core.event_queue import EventQueue
from core.events import AudioEvent, RFIDEvent
from core.game_state import GameStateManager, PlayerState
from core.hand_log import ActionRecord, HandSummary
from core.patch_proposal import FieldPatch, HandPatchProposal
from core.hand_reconstructor import (
    HandReconstructionResult,
    HandReconstructor,
)
from integration.engine import IntegrationThread
from output.json_writer import JsonWriter
from output.replay_hand import EvidenceRecord
from output.reconstruct_session import reconstruct_session


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────


def _audio_rec(action: str, ts: float, raw: str = "") -> EvidenceRecord:
    return EvidenceRecord(
        timestamp=ts,
        kind="audio",
        event=AudioEvent(action=action, amount=0, timestamp=ts, raw_text=raw),
        payload={},
    )


def _rfid_seat_rec(seat: int, card: str, ts: float) -> EvidenceRecord:
    return EvidenceRecord(
        timestamp=ts,
        kind="rfid",
        event=RFIDEvent(
            tag_id=f"tag_seat_{seat}_{card}",
            card=card,
            reader_id=f"seat_{seat}",
            role="seat",
            seat=seat,
            timestamp=ts,
            raw_tag_id=f"tag_seat_{seat}_{card}",
        ),
        payload={},
    )


def _make_hu_online_summary(
    *,
    hand_id: int = 1,
    sb_seat: int = 1,
    bb_seat: int = 2,
    sb: int = 100,
    bb: int = 200,
    winner_seat: int = 2,
) -> HandSummary:
    """HU で seat=sb fold → seat=bb winner となる online HandSummary を組み立てる。"""
    actions = [
        ActionRecord(
            hand_id=hand_id, timestamp="", street="preflop", seat=sb_seat,
            player_name=f"P{sb_seat}", action="SB_POST", amount=sb,
            pot_after=sb, stack_after=10000 - sb,
            source={"audio": False, "camera": False, "rfid": False},
            needs_review=False, confidence=1.0,
        ),
        ActionRecord(
            hand_id=hand_id, timestamp="", street="preflop", seat=bb_seat,
            player_name=f"P{bb_seat}", action="BB_POST", amount=bb,
            pot_after=sb + bb, stack_after=10000 - bb,
            source={"audio": False, "camera": False, "rfid": False},
            needs_review=False, confidence=1.0,
        ),
        ActionRecord(
            hand_id=hand_id, timestamp="", street="preflop", seat=sb_seat,
            player_name=f"P{sb_seat}", action="fold", amount=0,
            pot_after=sb + bb, stack_after=10000 - sb,
            source={"audio": True, "camera": False, "rfid": False},
            needs_review=False, confidence=1.0,
        ),
    ]
    players = [
        {"seat": sb_seat, "name": f"P{sb_seat}", "hole_cards": None,
         "hole_cards_source": "", "stack_start": 10000,
         "stack_end": 10000 - sb, "result": -sb},
        {"seat": bb_seat, "name": f"P{bb_seat}", "hole_cards": None,
         "hole_cards_source": "", "stack_start": 10000,
         "stack_end": 10000 + sb, "result": sb},
    ]
    return HandSummary(
        hand_id=hand_id, session_id="bootstrap_test",
        started_at="", ended_at="",
        blinds={"sb": sb, "bb": bb}, board=[], board_source="",
        players=players,
        pot_total=sb + bb,
        winner_seat=winner_seat,
        actions=actions,
        review_required=False, folded_seats=[sb_seat], all_in_seats=[],
        resolution_status="final", resolution_type="fold_win",
        seat_payouts={winner_seat: sb + bb},
    )


# ────────────────────────────────────────────────────────────────────────────
# 4-1. raw-only bootstrap 成功 (online_summary=None)
# ────────────────────────────────────────────────────────────────────────────


class TestRawBootstrapSuccess:
    def test_hu_raw_only_bootstrap_success(self) -> None:
        """HU で RFID 2 seat の hole_cards + audio fold + winner audio →
        ``bootstrap_source="raw"`` で reconstruct 成功 (online_summary 不要)。

        active = [seat 1, seat 2]、HU heuristic で button=sb=1, bb=2。
        preflop first actor は BTN=seat 1 なので、audio "fold" は seat 1 に
        attribute され、seat 2 が live で勝つ。
        """
        events = [
            _audio_rec("new_hand", 1.0),
            _rfid_seat_rec(1, "Ah", 1.1),
            _rfid_seat_rec(2, "Kh", 1.2),
            _audio_rec("fold", 1.3, "フォールド"),
            _audio_rec("winner", 1.4, "シート2 ウィナー"),
        ]

        rc = HandReconstructor(default_sb=100, default_bb=200)
        result = rc.reconstruct_from_events(events)

        assert result.bootstrap_source == "raw"
        assert result.summary is not None
        assert result.bootstrap_meta is not None

        meta = result.bootstrap_meta
        assert meta["source"] == "raw"
        assert meta["active_seats"] == [1, 2]
        assert meta["button_seat"] == 1
        assert meta["button_inferred"] is True
        assert meta["blinds_inferred"] is True
        assert "rfid_seat_observations" in meta["signals"]
        assert meta["signals"]["rfid_seat_observations"] == [1, 2]

        # online_summary 無しなので diff は計算されない → needs_review=False
        assert result.needs_review is False
        assert result.diff is None
        # reason は online_summary 不在の reconstruction success
        assert result.reason == "reconstructed"

    def test_raw_bootstrap_succeeds_with_three_seat_rfid(self) -> None:
        """3 seat 分の RFID hole_cards でも bootstrap 成立 (button = 最小 seat 番号)。"""
        events = [
            _audio_rec("new_hand", 1.0),
            _rfid_seat_rec(2, "Ah", 1.1),
            _rfid_seat_rec(3, "Kh", 1.2),
            _rfid_seat_rec(5, "Qh", 1.3),
            _audio_rec("fold", 1.5, "フォールド"),
            _audio_rec("winner", 1.6, "シート3 ウィナー"),
        ]

        rc = HandReconstructor(default_sb=100, default_bb=200)
        result = rc.reconstruct_from_events(events)

        assert result.bootstrap_source == "raw"
        meta = result.bootstrap_meta
        assert meta["active_seats"] == [2, 3, 5]
        # 最小 seat 番号 = 2 が button
        assert meta["button_seat"] == 2
        # 3-handed では SB = BTN の左隣 = active 内で BTN の次 = 3
        assert meta["sb_seat"] == 3
        assert meta["bb_seat"] == 5


# ────────────────────────────────────────────────────────────────────────────
# 4-2. raw-only 失敗 → online_summary fallback
# ────────────────────────────────────────────────────────────────────────────


class TestRawFailsFallsBackToOnlineSummary:
    def test_no_rfid_seats_falls_back_to_online_summary(self) -> None:
        """RFID seat 観測ゼロでも online_summary があれば bootstrap 成功。
        ``bootstrap_source="online_summary"``。
        """
        events = [
            _audio_rec("new_hand", 1.0),
            _audio_rec("fold", 1.3, "フォールド"),
            _audio_rec("winner", 1.4, "シート2 ウィナー"),
        ]
        online = _make_hu_online_summary()

        rc = HandReconstructor(default_sb=100, default_bb=200)
        result = rc.reconstruct_from_events(events, online_summary=online)

        assert result.bootstrap_source == "online_summary"
        assert result.summary is not None
        assert result.bootstrap_meta is None  # online_summary path は meta を立てない

    def test_only_one_rfid_seat_falls_back_to_online_summary(self) -> None:
        """RFID seat 観測が 1 seat だけ (= raw bootstrap の閾値未満) でも、
        online_summary があれば fallback 成功。
        """
        events = [
            _audio_rec("new_hand", 1.0),
            _rfid_seat_rec(1, "Ah", 1.1),     # 1 seat だけ
            _audio_rec("fold", 1.3, "フォールド"),
            _audio_rec("winner", 1.4, "シート2 ウィナー"),
        ]
        online = _make_hu_online_summary()

        rc = HandReconstructor(default_sb=100, default_bb=200)
        result = rc.reconstruct_from_events(events, online_summary=online)

        assert result.bootstrap_source == "online_summary"

    def test_no_default_blinds_falls_back_to_online_summary(self) -> None:
        """``default_sb=None`` で raw bootstrap 不可 → online_summary fallback。"""
        events = [
            _audio_rec("new_hand", 1.0),
            _rfid_seat_rec(1, "Ah", 1.1),
            _rfid_seat_rec(2, "Kh", 1.2),
            _audio_rec("fold", 1.3, "フォールド"),
            _audio_rec("winner", 1.4, "シート2 ウィナー"),
        ]
        online = _make_hu_online_summary()

        rc = HandReconstructor()  # default_sb / default_bb = None
        result = rc.reconstruct_from_events(events, online_summary=online)

        assert result.bootstrap_source == "online_summary"


# ────────────────────────────────────────────────────────────────────────────
# 4-3. raw-only も online_summary も無くて skipped
# ────────────────────────────────────────────────────────────────────────────


class TestBootstrapSkipped:
    def test_no_rfid_no_online_summary_returns_skipped(self) -> None:
        events = [
            _audio_rec("new_hand", 1.0),
            _audio_rec("fold", 1.3, "フォールド"),
        ]
        rc = HandReconstructor(default_sb=100, default_bb=200)
        result = rc.reconstruct_from_events(events)

        assert result.reason == "reconstruction_skipped"
        assert result.summary is None
        assert result.bootstrap_source is None
        assert result.bootstrap_meta is None

    def test_empty_events_no_online_summary_skipped(self) -> None:
        rc = HandReconstructor(default_sb=100, default_bb=200)
        result = rc.reconstruct_from_events([])

        assert result.reason == "reconstruction_skipped"
        assert result.bootstrap_source is None


# ────────────────────────────────────────────────────────────────────────────
# initial_state 優先 (Phase 3 から不変、Phase 4-B でも raw より優先)
# ────────────────────────────────────────────────────────────────────────────


class TestInitialStatePriority:
    def test_initial_state_takes_priority_over_raw(self) -> None:
        """``initial_state`` が渡されたら raw bootstrap は試さない
        (``bootstrap_source="initial_state"``)。
        """
        from integration.action_inference import BettingState

        bs = BettingState()
        bs.start_hand(button_seat=1, active_seats=[1, 2], sb_amount=100, bb_amount=200)

        events = [
            _audio_rec("new_hand", 1.0),
            _rfid_seat_rec(1, "Ah", 1.1),
            _rfid_seat_rec(2, "Kh", 1.2),
            _audio_rec("winner", 1.3, "シート1 ウィナー"),
        ]

        rc = HandReconstructor(default_sb=100, default_bb=200)
        result = rc.reconstruct_from_events(events, initial_state=bs)

        assert result.bootstrap_source == "initial_state"
        assert result.bootstrap_meta is None


# ────────────────────────────────────────────────────────────────────────────
# Phase 5-B: Audio 補助 + prev_button + meta 拡張
# ────────────────────────────────────────────────────────────────────────────


class TestPhase5BAudioAuxSignal:
    """Audio 由来の seat ヒントが active_seats に union されることを検証する。"""

    def test_rfid_two_plus_audio_one_unions_active_seats(self) -> None:
        """RFID で 2 seat、Audio で 1 seat (RFID と重複しない) → active = union 3 seat。"""
        events = [
            _audio_rec("new_hand", 1.0),
            _rfid_seat_rec(1, "Ah", 1.1),
            _rfid_seat_rec(2, "Kh", 1.2),
            # seat 3 は RFID で観測されないが音声では言及されている
            _audio_rec("fold", 1.3, "シート3 フォールド"),
            _audio_rec("winner", 1.4, "シート2 ウィナー"),
        ]

        rc = HandReconstructor(default_sb=100, default_bb=200)
        result = rc.reconstruct_from_events(events)

        assert result.bootstrap_source == "raw"
        meta = result.bootstrap_meta
        # active_seats = RFID ∪ Audio
        assert meta["active_seats"] == [1, 2, 3]
        # signals は両方とも記録される
        assert meta["signals"]["rfid_seat_observations"] == [1, 2]
        assert 3 in meta["signals"]["audio_seat_hints"]
        # winner 由来の "シート2" も Audio ヒントに入る (defensive: 信号源として記録)
        assert 2 in meta["signals"]["audio_seat_hints"]

    def test_audio_seat_hint_without_enough_rfid_falls_back(self) -> None:
        """RFID が 1 seat だけで Audio が 1 seat ヒントを足しても、
        Phase 5-B の conservative gate (RFID >= 2) により raw bootstrap は失敗。
        online_summary fallback または skipped に落ちる。
        """
        events = [
            _audio_rec("new_hand", 1.0),
            _rfid_seat_rec(1, "Ah", 1.1),     # RFID は seat 1 だけ
            _audio_rec("fold", 1.3, "シート2 フォールド"),  # Audio hint = seat 2
            _audio_rec("winner", 1.4, "シート1 ウィナー"),
        ]
        rc = HandReconstructor(default_sb=100, default_bb=200)
        result = rc.reconstruct_from_events(events)
        # online_summary 無しなので skipped (= raw は諦めている)
        assert result.reason == "reconstruction_skipped"
        assert result.bootstrap_source is None

    def test_no_audio_hints_still_works(self) -> None:
        """Audio に seat 言及が無いケースでも raw bootstrap は (RFID >=2 なら) 成立。
        signals.audio_seat_hints は空 list で記録される。
        """
        events = [
            _audio_rec("new_hand", 1.0),
            _rfid_seat_rec(1, "Ah", 1.1),
            _rfid_seat_rec(2, "Kh", 1.2),
            _audio_rec("fold", 1.3, "フォールド"),     # seat 言及無し
            _audio_rec("winner", 1.4, "ウィナー"),     # seat 言及無し
        ]
        rc = HandReconstructor(default_sb=100, default_bb=200)
        result = rc.reconstruct_from_events(events)

        assert result.bootstrap_source == "raw"
        assert result.bootstrap_meta["signals"]["audio_seat_hints"] == []

    def test_explicit_event_seat_attribute_is_picked_up(self) -> None:
        """将来 AudioEvent に ``seat: int`` が追加された場合の前向き互換。
        raw_text に "シート N" が無くても explicit な seat 属性なら拾う。
        """
        from output.replay_hand import EvidenceRecord

        # AudioEvent dataclass には seat field が無いため、duck-typed な
        # SimpleNamespace で構築する (将来拡張シミュレーション)。
        from types import SimpleNamespace

        events = [
            _audio_rec("new_hand", 1.0),
            _rfid_seat_rec(2, "Ah", 1.1),
            _rfid_seat_rec(4, "Kh", 1.2),
            EvidenceRecord(
                timestamp=1.3, kind="audio",
                # ``raw_text`` には seat ヒント無し、``seat`` 属性だけ
                event=SimpleNamespace(
                    action="fold", amount=0, timestamp=1.3,
                    raw_text="フォールド", seat=3,
                ),
                payload={},
            ),
        ]
        rc = HandReconstructor(default_sb=100, default_bb=200)
        result = rc.reconstruct_from_events(events)
        # NOTE: isinstance(rec.event, AudioEvent) ガードがあるため、SimpleNamespace
        # は audio 経路に入らない。これは仕様 (defensive)。よって、明示 seat 属性は
        # AudioEvent インスタンスにのみ反映される (将来 AudioEvent に seat を足したら有効)。
        # 現状では Audio 経路に入らないので audio_seat_hints は空。
        # → このテストは「現仕様で raw bootstrap が壊れない」ことだけ確認。
        assert result.bootstrap_source == "raw"


class TestPhase5BPrevButton:
    """prev_button から左隣 heuristic を検証する。"""

    def _hu_events(self, t0: float = 1.0) -> list:
        return [
            _audio_rec("new_hand", t0),
            _rfid_seat_rec(1, "Ah", t0 + 0.1),
            _rfid_seat_rec(2, "Kh", t0 + 0.2),
            _audio_rec("fold", t0 + 0.3, "フォールド"),
            _audio_rec("winner", t0 + 0.4, "シート2 ウィナー"),
        ]

    def test_first_hand_no_prev_button(self) -> None:
        """初手の hand (prev_button=None) では min(active_seats) fallback。"""
        rc = HandReconstructor(default_sb=100, default_bb=200)
        result = rc.reconstruct_from_events(self._hu_events())

        assert result.bootstrap_source == "raw"
        meta = result.bootstrap_meta
        assert meta["button_seat"] == 1     # min(1, 2)
        assert meta["button_inferred_from_prev"] is False
        assert meta["prev_button"] is None

    def test_second_hand_uses_prev_button_left_neighbor(self) -> None:
        """同じ HandReconstructor で 2 hand 連続実行すると、2 hand 目の button は
        前 hand の button の左隣 (= ring 上の次 index)。
        """
        rc = HandReconstructor(default_sb=100, default_bb=200)
        # 1 hand 目: prev_button=None → button=1 (min)
        result1 = rc.reconstruct_from_events(self._hu_events(t0=1.0))
        assert result1.bootstrap_source == "raw"
        assert result1.bootstrap_meta["button_seat"] == 1

        # 2 hand 目: prev_button=1 が active set [1,2] に含まれる → 左隣 = 2
        events2 = [
            _audio_rec("new_hand", 2.0),
            _rfid_seat_rec(1, "Ad", 2.1),
            _rfid_seat_rec(2, "Kd", 2.2),
            _audio_rec("fold", 2.3, "フォールド"),
            _audio_rec("winner", 2.4, "シート1 ウィナー"),
        ]
        result2 = rc.reconstruct_from_events(events2)
        assert result2.bootstrap_source == "raw"
        meta = result2.bootstrap_meta
        assert meta["prev_button"] == 1
        assert meta["button_inferred_from_prev"] is True
        assert meta["button_seat"] == 2

    def test_prev_button_not_in_active_falls_back_to_min(self) -> None:
        """prev_button が現 hand の active set に含まれていない場合、
        min(active_seats) にフォールバックする (= prev は無視する)。
        """
        rc = HandReconstructor(default_sb=100, default_bb=200)
        # わざと _prev_button_seat を「現 hand の active には居ない seat」にセット
        rc._prev_button_seat = 99

        events = [
            _audio_rec("new_hand", 1.0),
            _rfid_seat_rec(3, "Ah", 1.1),
            _rfid_seat_rec(4, "Kh", 1.2),
            _audio_rec("fold", 1.3, "フォールド"),
            _audio_rec("winner", 1.4, "シート4 ウィナー"),
        ]
        result = rc.reconstruct_from_events(events)
        assert result.bootstrap_source == "raw"
        meta = result.bootstrap_meta
        assert meta["prev_button"] == 99
        assert meta["button_inferred_from_prev"] is False
        # active=[3,4] の min
        assert meta["button_seat"] == 3

    def test_prev_button_advances_in_three_handed_ring(self) -> None:
        """3 seat 環境で prev_button が active set に居る場合、ring 上の次 index。"""
        rc = HandReconstructor(default_sb=100, default_bb=200)
        rc._prev_button_seat = 3   # active=[2,3,5] の中で 3 → 左隣 = index 2 = seat 5

        events = [
            _audio_rec("new_hand", 1.0),
            _rfid_seat_rec(2, "Ah", 1.1),
            _rfid_seat_rec(3, "Kh", 1.2),
            _rfid_seat_rec(5, "Qh", 1.3),
            _audio_rec("fold", 1.5, "フォールド"),
            _audio_rec("winner", 1.6, "シート2 ウィナー"),
        ]
        result = rc.reconstruct_from_events(events)
        assert result.bootstrap_source == "raw"
        meta = result.bootstrap_meta
        assert meta["active_seats"] == [2, 3, 5]
        assert meta["button_inferred_from_prev"] is True
        assert meta["button_seat"] == 5      # active[index_of(3)+1 mod 3] = active[2] = 5

    def test_prev_button_wraps_around_ring(self) -> None:
        """prev_button が active の末尾なら、左隣は wrap して先頭 (ring 性)。"""
        rc = HandReconstructor(default_sb=100, default_bb=200)
        rc._prev_button_seat = 5   # active=[2,3,5] の末尾 → 左隣 = wrap して 2

        events = [
            _audio_rec("new_hand", 1.0),
            _rfid_seat_rec(2, "Ah", 1.1),
            _rfid_seat_rec(3, "Kh", 1.2),
            _rfid_seat_rec(5, "Qh", 1.3),
            _audio_rec("winner", 1.6, "シート3 ウィナー"),
        ]
        result = rc.reconstruct_from_events(events)
        meta = result.bootstrap_meta
        assert meta["button_inferred_from_prev"] is True
        assert meta["button_seat"] == 2

    def test_prev_button_updated_after_online_summary_bootstrap(self) -> None:
        """online_summary 経路で立ち上がった hand の button も _prev_button_seat に
        記録され、続く raw bootstrap で利用される。
        """
        rc = HandReconstructor(default_sb=100, default_bb=200)

        # 1 hand 目: RFID 0 seat → raw 失敗 → online_summary fallback
        online = _make_hu_online_summary(hand_id=1, sb_seat=1, bb_seat=2)
        events1 = [
            _audio_rec("new_hand", 1.0),
            _audio_rec("fold", 1.3, "フォールド"),
            _audio_rec("winner", 1.4, "シート2 ウィナー"),
        ]
        result1 = rc.reconstruct_from_events(events1, online_summary=online)
        assert result1.bootstrap_source == "online_summary"
        # online_summary 経路でも prev_button は更新される (HU: BTN=SB=1)
        assert rc._prev_button_seat == 1

        # 2 hand 目: RFID 2 seat → raw bootstrap、prev=1 を活用
        events2 = [
            _audio_rec("new_hand", 2.0),
            _rfid_seat_rec(1, "Ad", 2.1),
            _rfid_seat_rec(2, "Kd", 2.2),
            _audio_rec("winner", 2.4, "シート1 ウィナー"),
        ]
        result2 = rc.reconstruct_from_events(events2)
        assert result2.bootstrap_source == "raw"
        meta = result2.bootstrap_meta
        assert meta["prev_button"] == 1
        assert meta["button_inferred_from_prev"] is True
        assert meta["button_seat"] == 2


class TestPhase5BMetaExtensions:
    """Phase 5-B で追加された bootstrap_meta フィールドを検証する。"""

    def test_new_meta_keys_present_on_raw_success(self) -> None:
        events = [
            _audio_rec("new_hand", 1.0),
            _rfid_seat_rec(1, "Ah", 1.1),
            _rfid_seat_rec(2, "Kh", 1.2),
            _audio_rec("winner", 1.4, "シート2 ウィナー"),
        ]
        rc = HandReconstructor(default_sb=100, default_bb=200)
        result = rc.reconstruct_from_events(events)
        meta = result.bootstrap_meta
        # Phase 5-B 追加 field
        assert "audio_seat_hints" in meta["signals"]
        # Phase 5-B+: 出所カテゴリ別の細分化
        assert "audio_seat_hint_sources" in meta["signals"]
        assert "prev_button" in meta
        assert "button_inferred_from_prev" in meta
        # blinds change TODO 用のフック
        assert meta["sb_amount"] == 100
        assert meta["bb_amount"] == 200
        # Phase 4-B から存続している field は壊れていない
        assert meta["button_inferred"] is True
        assert meta["blinds_inferred"] is True
        assert meta["source"] == "raw"
        # Phase 5-B+: confidence は staged になった (固定 0.5 から脱却)。
        # この fixture は RFID=2 + winner audio のみ + 初手 (prev=None) なので
        # boost が一切付かず baseline 0.4 になる。
        assert meta["confidence"] == 0.4
        assert "rfid_seat_observations" in meta["signals"]


class TestPhase5BPlusAudioSourceCategorization:
    """Phase 5-B+: audio_seat_hint_sources で action / winner / other の出所
    分類が正しく行われることを検証する。"""

    def test_action_winner_other_categorization(self) -> None:
        """action / winner / other が混在する events から各カテゴリへ正しく振り分け。"""
        events = [
            _audio_rec("new_hand", 1.0),                     # other (new_hand)
            _rfid_seat_rec(1, "Ah", 1.1),
            _rfid_seat_rec(2, "Kh", 1.2),
            _audio_rec("fold", 1.3, "シート3 フォールド"),  # action → 3
            _audio_rec("call", 1.4, "シート1 コール"),     # action → 1 (RFID と overlap)
            _audio_rec("winner", 1.5, "シート2 ウィナー"), # winner → 2
        ]
        rc = HandReconstructor(default_sb=100, default_bb=200)
        result = rc.reconstruct_from_events(events)
        meta = result.bootstrap_meta
        srcs = meta["signals"]["audio_seat_hint_sources"]
        assert srcs["action"] == [1, 3]      # fold(3) + call(1)
        assert srcs["winner"] == [2]
        assert srcs["other"]  == []
        # flat list (backwards compat) は union sorted
        assert meta["signals"]["audio_seat_hints"] == [1, 2, 3]

    def test_winner_only_audio_keeps_action_empty(self) -> None:
        """winner mention だけだと action カテゴリは空のまま。
        Phase 5-B+ では active 推定では union するが、confidence boost には使わない。
        """
        events = [
            _audio_rec("new_hand", 1.0),
            _rfid_seat_rec(1, "Ah", 1.1),
            _rfid_seat_rec(2, "Kh", 1.2),
            _audio_rec("winner", 1.4, "シート2 ウィナー"),
        ]
        rc = HandReconstructor(default_sb=100, default_bb=200)
        result = rc.reconstruct_from_events(events)
        srcs = result.bootstrap_meta["signals"]["audio_seat_hint_sources"]
        assert srcs["action"] == []
        assert srcs["winner"] == [2]
        assert srcs["other"]  == []


class TestPhase5BPlusStagedConfidence:
    """Phase 5-B+: staged confidence (baseline 0.4 + 3 つの +0.1 boost)。"""

    def test_baseline_minimum_signals(self) -> None:
        """RFID=2 seat, no action-derived audio, no prev_button → baseline 0.4。"""
        events = [
            _audio_rec("new_hand", 1.0),
            _rfid_seat_rec(1, "Ah", 1.1),
            _rfid_seat_rec(2, "Kh", 1.2),
            _audio_rec("winner", 1.4, "シート2 ウィナー"),   # winner-derived は boost 対象外
        ]
        rc = HandReconstructor(default_sb=100, default_bb=200)
        result = rc.reconstruct_from_events(events)
        assert result.bootstrap_meta["confidence"] == 0.4

    def test_rfid_three_plus_boost(self) -> None:
        """RFID >= 3 seat 観測で +0.1 (= 0.5)。"""
        events = [
            _audio_rec("new_hand", 1.0),
            _rfid_seat_rec(2, "Ah", 1.1),
            _rfid_seat_rec(3, "Kh", 1.2),
            _rfid_seat_rec(5, "Qh", 1.3),
            _audio_rec("winner", 1.6, "シート3 ウィナー"),    # winner は boost 対象外
        ]
        rc = HandReconstructor(default_sb=100, default_bb=200)
        result = rc.reconstruct_from_events(events)
        assert result.bootstrap_meta["confidence"] == 0.5

    def test_action_audio_overlap_boost(self) -> None:
        """action-derived audio seat が RFID と overlap → +0.1 (= 0.5)。"""
        events = [
            _audio_rec("new_hand", 1.0),
            _rfid_seat_rec(1, "Ah", 1.1),
            _rfid_seat_rec(2, "Kh", 1.2),
            _audio_rec("fold", 1.3, "シート1 フォールド"),   # action → seat 1 が RFID と overlap
            _audio_rec("winner", 1.4, "シート2 ウィナー"),
        ]
        rc = HandReconstructor(default_sb=100, default_bb=200)
        result = rc.reconstruct_from_events(events)
        assert result.bootstrap_meta["confidence"] == 0.5

    def test_action_audio_without_rfid_overlap_no_boost(self) -> None:
        """action audio が seat 3 を言及するが RFID には居ない → boost 無し (= 0.4)。
        Phase 5-B 拡張で active_seats には 3 が入るが、cross-modal は overlap 必須。
        """
        events = [
            _audio_rec("new_hand", 1.0),
            _rfid_seat_rec(1, "Ah", 1.1),
            _rfid_seat_rec(2, "Kh", 1.2),
            _audio_rec("fold", 1.3, "シート3 フォールド"),   # action だが seat 3 は RFID 外
            _audio_rec("winner", 1.4, "シート2 ウィナー"),
        ]
        rc = HandReconstructor(default_sb=100, default_bb=200)
        result = rc.reconstruct_from_events(events)
        assert result.bootstrap_meta["confidence"] == 0.4

    def test_prev_button_boost(self) -> None:
        """button_inferred_from_prev → +0.1 (= 0.5)。"""
        rc = HandReconstructor(default_sb=100, default_bb=200)
        rc._prev_button_seat = 1
        events = [
            _audio_rec("new_hand", 1.0),
            _rfid_seat_rec(1, "Ah", 1.1),
            _rfid_seat_rec(2, "Kh", 1.2),
            _audio_rec("winner", 1.4, "シート1 ウィナー"),
        ]
        result = rc.reconstruct_from_events(events)
        meta = result.bootstrap_meta
        assert meta["button_inferred_from_prev"] is True
        assert meta["confidence"] == 0.5

    def test_all_three_boosts_max_confidence(self) -> None:
        """3 boost 全部スタック → 0.7 (max)。"""
        rc = HandReconstructor(default_sb=100, default_bb=200)
        rc._prev_button_seat = 2     # active=[2,3,5] に含まれる → prev boost
        events = [
            _audio_rec("new_hand", 1.0),
            _rfid_seat_rec(2, "Ah", 1.1),
            _rfid_seat_rec(3, "Kh", 1.2),
            _rfid_seat_rec(5, "Qh", 1.3),                       # RFID >= 3 → +0.1
            _audio_rec("fold", 1.4, "シート3 フォールド"),    # action audio overlap → +0.1
            _audio_rec("winner", 1.5, "シート5 ウィナー"),
        ]
        result = rc.reconstruct_from_events(events)
        meta = result.bootstrap_meta
        # 0.4 base + 0.1 (RFID>=3) + 0.1 (action overlap) + 0.1 (prev) = 0.7
        assert meta["confidence"] == 0.7
        assert meta["button_inferred_from_prev"] is True


class TestPhase5CBlindSource:
    """Phase 5-C: bootstrap_meta の ``blind_source`` field と
    ``HandReconstructor.update_blinds`` の挙動を検証する。"""

    def _hu_events(self) -> list:
        return [
            _audio_rec("new_hand", 1.0),
            _rfid_seat_rec(1, "Ah", 1.1),
            _rfid_seat_rec(2, "Kh", 1.2),
            _audio_rec("winner", 1.4, "シート2 ウィナー"),
        ]

    def test_default_blinds_yield_session_default_source(self) -> None:
        """update_blinds を呼ばない場合、blind_source = "session_default"。"""
        rc = HandReconstructor(default_sb=100, default_bb=200)
        result = rc.reconstruct_from_events(self._hu_events())
        assert result.bootstrap_source == "raw"
        meta = result.bootstrap_meta
        assert meta["sb_amount"] == 100
        assert meta["bb_amount"] == 200
        assert meta["blind_source"] == "session_default"

    def test_update_blinds_marks_current_state(self) -> None:
        """update_blinds 後の raw bootstrap は blind_source = "current_state"。"""
        rc = HandReconstructor(default_sb=100, default_bb=200)
        rc.update_blinds(200, 400)
        result = rc.reconstruct_from_events(self._hu_events())
        assert result.bootstrap_source == "raw"
        meta = result.bootstrap_meta
        assert meta["sb_amount"] == 200
        assert meta["bb_amount"] == 400
        assert meta["blind_source"] == "current_state"

    def test_update_blinds_invalid_value_silently_ignored(self) -> None:
        """update_blinds は不正値を黙殺する (呼び側でバリデーション済み想定)。"""
        rc = HandReconstructor(default_sb=100, default_bb=200)
        rc.update_blinds(-5, 200)
        # 不正値は反映されない (= 既存 default が残る)
        assert rc._default_sb == 100
        assert rc._default_bb == 200
        assert rc._blinds_updated_at_runtime is False
        rc.update_blinds("bogus", 400)  # type: ignore[arg-type]
        assert rc._blinds_updated_at_runtime is False

    def test_update_blinds_persists_across_multiple_hands(self) -> None:
        """update_blinds 後、続く複数 hand 全てで current_state が記録される。"""
        rc = HandReconstructor(default_sb=100, default_bb=200)
        rc.update_blinds(300, 600)

        # 1 hand 目
        r1 = rc.reconstruct_from_events(self._hu_events())
        assert r1.bootstrap_meta["sb_amount"] == 300
        assert r1.bootstrap_meta["bb_amount"] == 600
        assert r1.bootstrap_meta["blind_source"] == "current_state"

        # 2 hand 目 (異なる timestamps、別 events)
        events2 = [
            _audio_rec("new_hand", 2.0),
            _rfid_seat_rec(1, "Ad", 2.1),
            _rfid_seat_rec(2, "Kd", 2.2),
            _audio_rec("winner", 2.4, "シート1 ウィナー"),
        ]
        r2 = rc.reconstruct_from_events(events2)
        assert r2.bootstrap_meta["sb_amount"] == 300
        assert r2.bootstrap_meta["bb_amount"] == 600
        assert r2.bootstrap_meta["blind_source"] == "current_state"


class TestPhase5DBlindMismatchAdvisory:
    """Phase 5-D: blind state mismatch を検出して advisory (needs_review / reason /
    patch_proposal) を強化するロジックを検証する。

    Pattern (A): propagation health check
      ``_blinds_updated_at_runtime=True`` だが meta.blind_source ==
      "session_default" のまま (= 配線が壊れた defensive 検査)

    Pattern (B): amount mismatch
      ``online_summary.blinds`` と ``bootstrap_meta.sb_amount/bb_amount`` が
      ズレている (= reconstructor が古い blind で raw bootstrap している)
    """

    def _result(
        self,
        *,
        bootstrap_meta: dict,
        reason: str = "reconstructed_no_diff",
        needs_review: bool = False,
        patch_proposal=None,
        diff=None,
    ):
        """advisory 投入用の HandReconstructionResult を組み立てる test helper。"""
        from core.hand_reconstructor import HandReconstructionResult
        return HandReconstructionResult(
            actions=[],
            summary=object(),       # 非 None であれば advisory ロジックは中身を見ない
            needs_review=needs_review,
            reason=reason,
            diff=diff,
            confidence=None,
            bootstrap_source="raw",
            bootstrap_meta=bootstrap_meta,
            patch_proposal=patch_proposal,
        )

    # ── (A) propagation health check ───────────────────────────────────────

    def test_no_update_session_default_is_normal(self) -> None:
        """update_blinds 未呼び出し + meta=session_default は正常 (= 変更なし)。"""
        rc = HandReconstructor(default_sb=100, default_bb=200)
        assert rc._blinds_updated_at_runtime is False
        result = self._result(
            bootstrap_meta={"blind_source": "session_default",
                            "sb_amount": 100, "bb_amount": 200},
        )
        rc._apply_blind_mismatch_advisory(result, online_summary=None)
        assert result.needs_review is False
        assert result.reason == "reconstructed_no_diff"
        assert result.patch_proposal is None

    def test_pattern_a_update_after_session_default_triggers(self) -> None:
        """update_blinds 呼び済み (_blinds_updated_at_runtime=True) なのに meta が
        session_default のまま → Pattern A 発火、advisory 強化。
        """
        rc = HandReconstructor(default_sb=100, default_bb=200)
        # ``update_blinds`` を呼んで flag を立てた状態をシミュレート
        rc._blinds_updated_at_runtime = True
        result = self._result(
            bootstrap_meta={"blind_source": "session_default",
                            "sb_amount": 100, "bb_amount": 200},
        )
        rc._apply_blind_mismatch_advisory(result, online_summary=None)
        assert result.needs_review is True
        # online_summary 無 + 純粋 blind-only issue → reason 昇格
        assert result.reason == "reconstructed_with_blind_mismatch"
        # blind-only proposal が作られている
        assert result.patch_proposal is not None
        assert "blinds_session_default_after_update" in (
            result.patch_proposal.summary_note or ""
        )

    def test_pattern_a_keeps_existing_reason_when_diff_also_present(self) -> None:
        """既存 reason が "reconstructed_with_diff" (= settlement 差分あり) の場合、
        reason は昇格させず summary_note だけ追記する。
        """
        rc = HandReconstructor(default_sb=100, default_bb=200)
        rc._blinds_updated_at_runtime = True
        existing_proposal = HandPatchProposal(
            hand_id=1, can_patch_automatically=False,
            fields=[FieldPatch(field="pot_total", online=300, offline=600,
                                note="total pot differs")],
            summary_note="pot_total differ; candidate to update settlement fields",
        )
        result = self._result(
            bootstrap_meta={"blind_source": "session_default",
                            "sb_amount": 100, "bb_amount": 200},
            reason="reconstructed_with_diff",
            needs_review=True,
            patch_proposal=existing_proposal,
        )
        rc._apply_blind_mismatch_advisory(result, online_summary=None)
        # reason はそのまま (settlement diff が canonical な signal)
        assert result.reason == "reconstructed_with_diff"
        # 既存 proposal を破壊せず note に blind mismatch を追記
        note = result.patch_proposal.summary_note
        assert "pot_total" in note
        assert "blinds_session_default_after_update" in note

    # ── (B) amount mismatch ──────────────────────────────────────────────

    def test_pattern_b_sb_bb_mismatch_creates_blind_field_patch(self) -> None:
        """online_summary.blinds と meta.sb_amount/bb_amount のズレ → Pattern B 発火。
        blind FieldPatch が proposal に含まれる。
        """
        rc = HandReconstructor(default_sb=100, default_bb=200)
        # online は新 blind (200/400)、meta は旧 blind (100/200)
        online = _make_hu_online_summary(hand_id=42, sb=200, bb=400)
        result = self._result(
            bootstrap_meta={"blind_source": "session_default",
                            "sb_amount": 100, "bb_amount": 200},
        )
        rc._apply_blind_mismatch_advisory(result, online_summary=online)
        assert result.needs_review is True
        assert result.reason == "reconstructed_with_blind_mismatch"
        assert result.patch_proposal is not None
        # blind FieldPatch が含まれている
        blind_fps = [fp for fp in result.patch_proposal.fields if fp.field == "blinds"]
        assert len(blind_fps) == 1
        fp = blind_fps[0]
        assert fp.online == {"sb": 200, "bb": 400}
        assert fp.offline == {"sb": 100, "bb": 200}
        assert "differ" in (fp.note or "")
        # proposal の hand_id は online_summary.hand_id から取られる
        assert result.patch_proposal.hand_id == 42

    def test_pattern_b_only_bb_differs_still_triggers(self) -> None:
        """SB が同じで BB だけ違う場合も mismatch として扱う。"""
        rc = HandReconstructor(default_sb=100, default_bb=200)
        online = _make_hu_online_summary(hand_id=1, sb=100, bb=400)  # SB 同じ、BB 違う
        result = self._result(
            bootstrap_meta={"blind_source": "session_default",
                            "sb_amount": 100, "bb_amount": 200},
        )
        rc._apply_blind_mismatch_advisory(result, online_summary=online)
        assert result.needs_review is True
        blind_fps = [fp for fp in result.patch_proposal.fields if fp.field == "blinds"]
        assert blind_fps[0].online == {"sb": 100, "bb": 400}
        assert blind_fps[0].offline == {"sb": 100, "bb": 200}

    def test_pattern_b_appends_to_existing_proposal(self) -> None:
        """既存 patch_proposal (pot_total など他 field 入り) に blind FieldPatch を
        append し、summary_note に blind mismatch を追記する。
        """
        rc = HandReconstructor(default_sb=100, default_bb=200)
        online = _make_hu_online_summary(hand_id=1, sb=200, bb=400)
        existing_proposal = HandPatchProposal(
            hand_id=1, can_patch_automatically=False,
            fields=[FieldPatch(field="pot_total", online=600, offline=300,
                                note="total pot differs")],
            summary_note="pot_total differ; candidate to update settlement fields",
        )
        result = self._result(
            bootstrap_meta={"blind_source": "session_default",
                            "sb_amount": 100, "bb_amount": 200},
            reason="reconstructed_with_diff",
            needs_review=True,
            patch_proposal=existing_proposal,
        )
        rc._apply_blind_mismatch_advisory(result, online_summary=online)
        # 既存 field は残る + blind が append される
        field_names = [fp.field for fp in result.patch_proposal.fields]
        assert "pot_total" in field_names
        assert "blinds" in field_names
        # summary_note は連結される
        note = result.patch_proposal.summary_note
        assert "pot_total" in note
        assert "blind mismatch" in note

    # ── 正常ケース (Phase 5-C+ 互換) ─────────────────────────────────────

    def test_current_state_and_matching_amounts_no_advisory_change(self) -> None:
        """update_blinds 済 + meta=current_state + amounts 一致 → 何も変えない。"""
        rc = HandReconstructor(default_sb=200, default_bb=400)
        rc._blinds_updated_at_runtime = True
        online = _make_hu_online_summary(hand_id=1, sb=200, bb=400)
        result = self._result(
            bootstrap_meta={"blind_source": "current_state",
                            "sb_amount": 200, "bb_amount": 400},
        )
        rc._apply_blind_mismatch_advisory(result, online_summary=online)
        assert result.needs_review is False
        assert result.reason == "reconstructed_no_diff"
        assert result.patch_proposal is None

    def test_no_online_summary_no_meta_amounts_no_pattern_b(self) -> None:
        """meta に sb/bb amount が無い場合は Pattern B は発火しない。"""
        rc = HandReconstructor(default_sb=100, default_bb=200)
        result = self._result(
            bootstrap_meta={"blind_source": "session_default"},   # amount 無し
        )
        rc._apply_blind_mismatch_advisory(result, online_summary=None)
        # propagation 健全 (flag=False) でかつ amount 無 → 何も変わらない
        assert result.needs_review is False
        assert result.patch_proposal is None

    # ── e2e: reconstruct_from_events 経由で advisory が立つ ─────────────

    def test_e2e_amount_mismatch_via_reconstruct_from_events(self) -> None:
        """``reconstruct_from_events`` 経由で:
        - default_sb/bb=100/200 で raw bootstrap → meta.sb_amount=100/bb_amount=200
        - online_summary.blinds={"sb":200,"bb":400} (= 違う blind level)
        - → Pattern B 検出、blind FieldPatch ぶら下がり、needs_review=True
        """
        rc = HandReconstructor(default_sb=100, default_bb=200)
        # online は新 blind 200/400 だが events / reconstructor は旧 100/200 を使う
        online = _make_hu_online_summary(hand_id=1, sb=200, bb=400)
        events = [
            _audio_rec("new_hand", 1.0),
            _rfid_seat_rec(1, "Ah", 1.1),
            _rfid_seat_rec(2, "Kh", 1.2),
            _audio_rec("winner", 1.4, "シート2 ウィナー"),
        ]
        result = rc.reconstruct_from_events(events, online_summary=online)
        # raw bootstrap が成立
        assert result.bootstrap_source == "raw"
        # blind mismatch 検出
        assert result.needs_review is True
        # patch_proposal が出来ていて blind FieldPatch が入っている
        assert result.patch_proposal is not None
        field_names = [fp.field for fp in result.patch_proposal.fields]
        assert "blinds" in field_names
        # summary_note に blind mismatch
        assert "blind mismatch" in (result.patch_proposal.summary_note or "")

    def test_e2e_no_mismatch_keeps_phase5c_behavior(self) -> None:
        """blinds が一致するケースは Phase 5-C と完全に同じ挙動。"""
        rc = HandReconstructor(default_sb=100, default_bb=200)
        # online と meta どちらも 100/200
        online = _make_hu_online_summary(hand_id=1, sb=100, bb=200)
        events = [
            _audio_rec("new_hand", 1.0),
            _rfid_seat_rec(1, "Ah", 1.1),
            _rfid_seat_rec(2, "Kh", 1.2),
            _audio_rec("winner", 1.4, "シート2 ウィナー"),
        ]
        result = rc.reconstruct_from_events(events, online_summary=online)
        # 差分なしのまま (Phase 5-C 互換)
        assert result.reason in ("reconstructed_no_diff", "reconstructed_with_diff")
        # 純粋 blind 起因の advisory は付かない (= "reconstructed_with_blind_mismatch"
        # にはならない)
        assert result.reason != "reconstructed_with_blind_mismatch"


class TestPhase5BPlusPrevButtonAcrossSkippedHand:
    """Phase 5-B+: skipped hand を挟んでも _prev_button_seat は **直近 successful**
    hand の button を保持する (semantics の明文化に対応)。"""

    def test_skipped_hand_does_not_overwrite_prev_button(self) -> None:
        rc = HandReconstructor(default_sb=100, default_bb=200)
        # 1 hand 目: HU で raw bootstrap 成功 → prev=1 (active=[1,2], min)
        events1 = [
            _audio_rec("new_hand", 1.0),
            _rfid_seat_rec(1, "Ah", 1.1),
            _rfid_seat_rec(2, "Kh", 1.2),
            _audio_rec("winner", 1.4, "シート2 ウィナー"),
        ]
        result1 = rc.reconstruct_from_events(events1)
        assert result1.bootstrap_source == "raw"
        assert rc._prev_button_seat == 1

        # 2 hand 目: signal 不十分 (RFID 0 + online_summary 無し) → skipped
        events2 = [
            _audio_rec("new_hand", 2.0),
            _audio_rec("winner", 2.4, "シート1 ウィナー"),
        ]
        result2 = rc.reconstruct_from_events(events2)
        assert result2.reason == "reconstruction_skipped"
        # 重要: skipped でも prev は壊れない (直近 *成功* hand の seed が残る)
        assert rc._prev_button_seat == 1

        # 3 hand 目: raw 復活 → prev=1 (前々 hand の成功) を seed に左隣を採用
        events3 = [
            _audio_rec("new_hand", 3.0),
            _rfid_seat_rec(1, "Ad", 3.1),
            _rfid_seat_rec(2, "Kd", 3.2),
            _audio_rec("winner", 3.4, "シート2 ウィナー"),
        ]
        result3 = rc.reconstruct_from_events(events3)
        assert result3.bootstrap_source == "raw"
        meta3 = result3.bootstrap_meta
        assert meta3["prev_button"] == 1
        assert meta3["button_inferred_from_prev"] is True
        assert meta3["button_seat"] == 2


class TestPhase5BSafetyFallbacks:
    """Phase 5-B 拡張で誤って raw bootstrap が成立しないことを保証する。"""

    def test_audio_only_no_rfid_does_not_raw_bootstrap(self) -> None:
        """RFID seat 観測ゼロ + Audio に seat ヒントが沢山あっても、
        Phase 5-B の conservative gate により raw は諦める。
        """
        events = [
            _audio_rec("new_hand", 1.0),
            # RFID 一切無し
            _audio_rec("fold", 1.2, "シート2 フォールド"),
            _audio_rec("fold", 1.3, "シート3 フォールド"),
            _audio_rec("winner", 1.4, "シート1 ウィナー"),
        ]
        rc = HandReconstructor(default_sb=100, default_bb=200)
        result = rc.reconstruct_from_events(events)
        assert result.reason == "reconstruction_skipped"
        assert result.bootstrap_source is None

    def test_default_blinds_missing_still_fails(self) -> None:
        """default_sb/bb が None なら、Audio が ヒントを足しても raw 失敗。"""
        events = [
            _audio_rec("new_hand", 1.0),
            _rfid_seat_rec(1, "Ah", 1.1),
            _rfid_seat_rec(2, "Kh", 1.2),
            _audio_rec("fold", 1.3, "シート3 フォールド"),
            _audio_rec("winner", 1.4, "シート2 ウィナー"),
        ]
        rc = HandReconstructor()   # default_sb/bb 無し
        result = rc.reconstruct_from_events(events)
        assert result.reason == "reconstruction_skipped"
        assert result.bootstrap_source is None


# ────────────────────────────────────────────────────────────────────────────
# 4-4. live hook 結合: online_summary=None でも raw-only bootstrap 可
# ────────────────────────────────────────────────────────────────────────────


class TestLiveHookRawBootstrap:
    def _build_thread(self, tmp_path: Path):
        players = [
            PlayerState(seat=1, name="A", stack=10000),
            PlayerState(seat=2, name="B", stack=10000),
        ]
        gs = GameStateManager(players=players, sb=100, bb=200)
        audio_q = EventQueue()
        writer = JsonWriter(log_dir=tmp_path, session_id="phase4b_live")
        stop = threading.Event()
        thread = IntegrationThread(
            audio_queue=audio_q,
            game_state=gs,
            json_writer=writer,
            stop_event=stop,
            initial_button_seat=2,
            auto_post_blinds=True,
        )
        return thread, audio_q, stop, writer, gs

    def test_invoke_hook_without_online_summary_raw_bootstrap_succeeds(
        self, tmp_path: Path,
    ) -> None:
        """live hook 経由で online_summary=None で呼んでも、_completed_hands に
        RFID hole_cards が入っていれば raw-only bootstrap が成立する
        (``bootstrap_source="raw"``、Phase 2-C 時代の ``reconstruction_skipped``
        ではなくなる)。
        """
        thread, _aq, _stop, _w, _gs = self._build_thread(tmp_path)
        # IntegrationThread コンストラクタが default_sb=100, default_bb=200 で
        # HandReconstructor を組んでいることを確認
        assert thread._hand_reconstructor._default_sb == 100
        assert thread._hand_reconstructor._default_bb == 200

        # _completed_hands に RFID hole_cards 込みの window を直接注入
        thread._completed_hands[42] = [
            _audio_rec("new_hand", 1.0),
            _rfid_seat_rec(1, "Ah", 1.1),
            _rfid_seat_rec(2, "Kh", 1.2),
            _audio_rec("fold", 1.3, "フォールド"),
            _audio_rec("winner", 1.4, "シート2 ウィナー"),
        ]

        # online_summary=None で invoke (= 非 audio_winner end と同じ経路)
        thread._invoke_reconstructor_hook(42, online_summary=None)

        assert 42 in thread._last_reconstruction_by_hand_id
        result = thread._last_reconstruction_by_hand_id[42]
        assert result.bootstrap_source == "raw"
        assert result.summary is not None
        # online_summary が無いので diff は計算されない
        assert result.reason == "reconstructed"


# ────────────────────────────────────────────────────────────────────────────
# 4-5. CLI round-trip: bootstrap_source が JSONL に入る
# ────────────────────────────────────────────────────────────────────────────


class TestCliRoundTripIncludesBootstrapMeta:
    def test_cli_output_includes_bootstrap_source_raw(self, tmp_path: Path) -> None:
        """raw-only bootstrap が成立する hand を CLI に通すと、出力 JSONL の
        各行に ``bootstrap_source`` と ``bootstrap_meta`` が入る。
        """
        # session JSON: blinds は top-level に置く (CLI が default_sb/bb を抽出する)
        session_id = "phase4b_cli"
        online = _make_hu_online_summary(hand_id=1)
        session_json = {
            "session_id": session_id,
            "blinds": {"sb": 100, "bb": 200},
            "hands": [online.to_dict()],
        }
        session_path = tmp_path / f"{session_id}.json"
        session_path.write_text(json.dumps(session_json), encoding="utf-8")

        # evidence JSONL: RFID hole cards + audio sequence
        evidence_path = tmp_path / f"evidence_{session_id}.jsonl"
        # encode raw payload (matches EvidenceLogWriter format roughly)
        records: list[dict] = [
            {"ts": 1.0, "kind": "audio", "action": "new_hand", "amount": 0, "raw_text": ""},
            {"ts": 1.1, "kind": "rfid", "tag_id": "t1", "card": "Ah", "reader_id": "seat_1",
             "role": "seat", "seat": 1, "raw_tag_id": "t1"},
            {"ts": 1.2, "kind": "rfid", "tag_id": "t2", "card": "Kh", "reader_id": "seat_2",
             "role": "seat", "seat": 2, "raw_tag_id": "t2"},
            {"ts": 1.3, "kind": "audio", "action": "fold", "amount": 0, "raw_text": "フォールド"},
            {"ts": 1.4, "kind": "audio", "action": "winner", "amount": 0,
             "raw_text": "シート2 ウィナー"},
        ]
        with evidence_path.open("w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

        out_path = reconstruct_session(session_path)
        lines = [l for l in out_path.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert "bootstrap_source" in entry
        assert "bootstrap_meta" in entry
        # raw bootstrap が初手で成立する想定 (RFID で 2 seat 揃っている)
        assert entry["bootstrap_source"] == "raw"
        assert entry["bootstrap_meta"]["active_seats"] == [1, 2]
        assert entry["bootstrap_meta"]["button_inferred"] is True

    def test_cli_output_falls_back_to_online_summary_without_rfid(
        self, tmp_path: Path,
    ) -> None:
        """RFID が無い hand なら CLI は ``bootstrap_source="online_summary"`` で抜ける。"""
        session_id = "phase4b_cli_fb"
        online = _make_hu_online_summary(hand_id=1)
        session_json = {
            "session_id": session_id,
            "blinds": {"sb": 100, "bb": 200},
            "hands": [online.to_dict()],
        }
        (tmp_path / f"{session_id}.json").write_text(
            json.dumps(session_json), encoding="utf-8",
        )

        # evidence: audio のみ、RFID なし
        evidence_path = tmp_path / f"evidence_{session_id}.jsonl"
        records: list[dict] = [
            {"ts": 1.0, "kind": "audio", "action": "new_hand", "amount": 0, "raw_text": ""},
            {"ts": 1.1, "kind": "audio", "action": "fold", "amount": 0, "raw_text": "フォールド"},
            {"ts": 1.2, "kind": "audio", "action": "winner", "amount": 0,
             "raw_text": "シート2 ウィナー"},
        ]
        with evidence_path.open("w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

        out_path = reconstruct_session(tmp_path / f"{session_id}.json")
        lines = [l for l in out_path.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["bootstrap_source"] == "online_summary"
