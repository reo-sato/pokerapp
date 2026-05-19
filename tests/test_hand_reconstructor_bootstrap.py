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
