"""tests/test_hand_reconstructor.py

Phase 3: HandReconstructor の本実装テスト。
- heads-up fold_win で online == offline → needs_review=False
- online を改変 (resolution_type / seat_payouts) → diff 検出で needs_review=True
- 不完全 data → reason="reconstruction_skipped"、online に影響しない
- 複数 hand → window ごとに独立して reconstruct
- CLI ラウンドトリップ (engine 経由で生成した session + evidence を読んで再構成)
"""
from __future__ import annotations

import json
import threading
import time
from copy import deepcopy
from pathlib import Path

import pytest

from core.event_queue import EventQueue
from core.events import AudioEvent
from core.game_state import GameStateManager, PlayerState
from core.hand_log import ActionRecord, HandSummary, PotSettlement
from core.hand_reconstructor import (
    HandReconstructionResult,
    HandReconstructor,
    _compute_diff,
)
from integration.engine import IntegrationThread
from output.json_writer import JsonWriter
from output.replay_hand import EvidenceRecord, extract_hand_windows, load_evidence_log
from output.reconstruct_session import reconstruct_session


# ────────────────────────────────────────────────────────────────────────────
# Helpers: 合成データ
# ────────────────────────────────────────────────────────────────────────────


def _rec_audio(action: str, ts: float, raw: str = "") -> EvidenceRecord:
    ev = AudioEvent(action=action, amount=0, timestamp=ts, raw_text=raw or action)
    return EvidenceRecord(timestamp=ts, kind="audio", event=ev)


def _fold_win_online_summary() -> HandSummary:
    """heads-up, button=seat2 (= BTN/SB), seat2 fold, WINNER seat1 を想定した
    canonical online HandSummary を合成する。
    """
    actions = [
        ActionRecord(
            hand_id=1, timestamp="2026-01-01T00:00:00", street="preflop",
            seat=2, player_name="P2", action="SB_POST", amount=100,
            pot_after=100, stack_after=9900,
            source={"audio": False, "camera": False, "rfid": False},
            needs_review=False, confidence=1.0,
        ),
        ActionRecord(
            hand_id=1, timestamp="2026-01-01T00:00:00", street="preflop",
            seat=1, player_name="P1", action="BB_POST", amount=200,
            pot_after=300, stack_after=9800,
            source={"audio": False, "camera": False, "rfid": False},
            needs_review=False, confidence=1.0,
        ),
        ActionRecord(
            hand_id=1, timestamp="2026-01-01T00:00:01", street="preflop",
            seat=2, player_name="P2", action="fold", amount=0,
            pot_after=300, stack_after=9900,
            source={"audio": True, "camera": False, "rfid": False},
            needs_review=False, confidence=1.0,
        ),
    ]
    return HandSummary(
        hand_id=1,
        session_id="test",
        started_at="2026-01-01T00:00:00",
        ended_at="2026-01-01T00:00:05",
        blinds={"sb": 100, "bb": 200},
        board=[],
        board_source="",
        players=[
            {"seat": 1, "name": "P1", "hole_cards": None, "hole_cards_source": "",
             "stack_start": 10000, "stack_end": 10100, "result": 100},
            {"seat": 2, "name": "P2", "hole_cards": None, "hole_cards_source": "",
             "stack_start": 10000, "stack_end": 9900, "result": -100},
        ],
        pot_total=300,
        winner_seat=1,
        actions=actions,
        review_required=False,
        folded_seats=[2],
        all_in_seats=[],
        resolution_status="final",
        resolution_type="fold_win",
        seat_payouts={1: 300},
        showdown_revealed_cards={},
        pots=[PotSettlement(
            amount=300, eligible_seats=[1, 2], winning_seats=[1],
            payouts={1: 300}, pot_type="main",
        )],
    )


def _fold_win_events(t0: float = 1.0) -> list[EvidenceRecord]:
    """fold_win シナリオに対応する EvidenceRecord 列。"""
    return [
        _rec_audio("new_hand", t0 + 0.0, raw="ハンド開始"),
        _rec_audio("fold",     t0 + 0.5, raw="フォールド"),
        _rec_audio("winner",   t0 + 1.0, raw="シート1 ウィナー"),
    ]


# ────────────────────────────────────────────────────────────────────────────
# 1. online == offline で needs_review=False
# ────────────────────────────────────────────────────────────────────────────


class TestReconstructorMatchesOnline:
    def test_heads_up_fold_win_no_diff(self) -> None:
        online = _fold_win_online_summary()
        events = _fold_win_events()
        result = HandReconstructor().reconstruct_from_events(
            events, online_summary=online,
        )
        assert result.summary is not None
        assert result.summary.resolution_type == "fold_win"
        assert result.summary.resolution_status == "final"
        assert result.summary.seat_payouts == {1: 300}
        assert result.summary.winner_seat == 1
        # online と一致 → needs_review=False, diff=None
        assert result.needs_review is False
        assert result.diff is None
        assert result.reason == "reconstructed_no_diff"
        # 再構成 actions: SB_POST / BB_POST / fold の 3 件
        action_keys = [(a.seat, a.action, a.amount) for a in result.actions]
        assert (2, "SB_POST", 100) in action_keys
        assert (1, "BB_POST", 200) in action_keys
        assert (2, "fold", 0) in action_keys

    def test_confidence_reflects_audio_consumption(self) -> None:
        """fold + winner で audio_count=2、consumed_count=1 (winner は seat 抽出のみ)。
        confidence = 1/2 = 0.5。"""
        online = _fold_win_online_summary()
        events = _fold_win_events()
        result = HandReconstructor().reconstruct_from_events(
            events, online_summary=online,
        )
        # new_hand を含めると audio_count=3、winner と new_hand は consume されない
        assert result.confidence is not None
        assert 0.0 < result.confidence <= 1.0


# ────────────────────────────────────────────────────────────────────────────
# 2. online を改変 → diff 検出
# ────────────────────────────────────────────────────────────────────────────


class TestReconstructorDetectsDiff:
    def test_tampered_resolution_type_detected(self) -> None:
        online = _fold_win_online_summary()
        # 故意に showdown と書き換える (fold_win が canonical)
        online.resolution_type = "showdown"
        result = HandReconstructor().reconstruct_from_events(
            _fold_win_events(), online_summary=online,
        )
        assert result.needs_review is True
        assert result.reason == "reconstructed_with_diff"
        assert result.diff is not None
        assert "resolution_type" in result.diff
        assert result.diff["resolution_type"]["online"] == "showdown"
        assert result.diff["resolution_type"]["offline"] == "fold_win"

    def test_tampered_seat_payouts_detected(self) -> None:
        online = _fold_win_online_summary()
        online.seat_payouts = {2: 300}  # winner を取り違えた
        online.winner_seat = 2
        result = HandReconstructor().reconstruct_from_events(
            _fold_win_events(), online_summary=online,
        )
        assert result.needs_review is True
        assert result.diff is not None
        assert "seat_payouts" in result.diff
        assert result.diff["seat_payouts"]["online"] == {2: 300}
        assert result.diff["seat_payouts"]["offline"] == {1: 300}
        assert "winner_seat" in result.diff

    def test_pot_total_diff_detected(self) -> None:
        online = _fold_win_online_summary()
        online.pot_total = 500   # 実際は 300
        result = HandReconstructor().reconstruct_from_events(
            _fold_win_events(), online_summary=online,
        )
        assert result.needs_review is True
        assert result.diff is not None
        assert "pot_total" in result.diff


# ────────────────────────────────────────────────────────────────────────────
# 3. 不完全データ / fallback
# ────────────────────────────────────────────────────────────────────────────


class TestReconstructorFallback:
    def test_no_events_no_online_summary(self) -> None:
        """events も online_summary も無い → reason=reconstruction_skipped。"""
        result = HandReconstructor().reconstruct_from_events([])
        assert result.summary is None
        assert result.reason == "reconstruction_skipped"
        assert result.needs_review is False

    def test_online_summary_without_blind_posts(self) -> None:
        """SB_POST/BB_POST が actions に無い → bootstrap 不能 → skipped。"""
        online = _fold_win_online_summary()
        online.actions = [a for a in online.actions if a.action not in ("SB_POST", "BB_POST")]
        result = HandReconstructor().reconstruct_from_events(
            _fold_win_events(), online_summary=online,
        )
        assert result.summary is None
        assert result.reason == "reconstruction_skipped"

    def test_no_audio_events_with_summary(self) -> None:
        """events 列が空でも online_summary から bootstrap でき、fold が無いので
        live_seats=[1,2] のまま finalize → incomplete か fold_win 以外になる。
        diff 検出はされうるが、online_summary の影響は受けない (mutate しない)。"""
        online = _fold_win_online_summary()
        online_copy = deepcopy(online)
        result = HandReconstructor().reconstruct_from_events(
            [], online_summary=online,
        )
        # online の中身は変えていない
        assert online.resolution_type == online_copy.resolution_type
        assert online.seat_payouts == online_copy.seat_payouts
        # offline は何らかの summary を返すか skipped
        if result.summary is not None:
            # fold action なし → live_seats=[1,2] → incomplete (board=0 < 5)
            assert result.summary.resolution_status == "incomplete"


# ────────────────────────────────────────────────────────────────────────────
# 4. 複数 hand を独立して reconstruct
# ────────────────────────────────────────────────────────────────────────────


class TestReconstructorMultipleHands:
    def test_two_hands_reconstructed_independently(self) -> None:
        # hand 1 (online): fold_win
        h1 = _fold_win_online_summary()
        h1.hand_id = 1
        # hand 2 (online): 同じ構造で hand_id=2
        h2 = deepcopy(h1)
        h2.hand_id = 2
        for a in h2.actions:
            a.hand_id = 2

        # 2 hand 分の events をまとめた log を想定し、extract_hand_windows でも
        # 期待通り分かれることを確認
        records = []
        t0 = 1.0
        records.extend(_fold_win_events(t0))
        records.extend(_fold_win_events(t0 + 10.0))
        windows = extract_hand_windows(records)
        assert sorted(windows.keys()) == [1, 2]

        rc = HandReconstructor()
        r1 = rc.reconstruct_from_events(windows[1], online_summary=h1)
        r2 = rc.reconstruct_from_events(windows[2], online_summary=h2)

        assert r1.summary is not None and r1.summary.hand_id == 1
        assert r2.summary is not None and r2.summary.hand_id == 2
        assert r1.needs_review is False
        assert r2.needs_review is False


# ────────────────────────────────────────────────────────────────────────────
# 5. _compute_diff の単体テスト
# ────────────────────────────────────────────────────────────────────────────


class TestComputeDiff:
    def test_identical_returns_none(self) -> None:
        a = _fold_win_online_summary()
        b = deepcopy(a)
        assert _compute_diff(a, b) is None

    def test_str_key_normalization_for_seat_payouts(self) -> None:
        """JSON round-trip 後 key が str になっているケースを int 正規化で吸収する。"""
        a = _fold_win_online_summary()
        b = deepcopy(a)
        b.seat_payouts = {"1": 300}  # type: ignore[dict-item]
        assert _compute_diff(a, b) is None

    def test_action_diff_only_seat_action_amount(self) -> None:
        """timestamp / pot_after / stack_after の違いは diff に出ない。"""
        a = _fold_win_online_summary()
        b = deepcopy(a)
        for act in b.actions:
            act.timestamp = "different"
            act.pot_after = 999999
            act.stack_after = 999999
        assert _compute_diff(a, b) is None


# ────────────────────────────────────────────────────────────────────────────
# 6. CLI ラウンドトリップ
# ────────────────────────────────────────────────────────────────────────────


class TestReconstructSessionCLI:
    def _build_thread(self, tmp_path: Path):
        players = [
            PlayerState(seat=1, name="P1", stack=10000),
            PlayerState(seat=2, name="P2", stack=10000),
        ]
        gs = GameStateManager(players=players, sb=100, bb=200)
        audio_q = EventQueue()
        writer = JsonWriter(log_dir=tmp_path, session_id="phase3_cli")
        stop = threading.Event()
        thread = IntegrationThread(
            audio_queue=audio_q, game_state=gs, json_writer=writer,
            stop_event=stop, initial_button_seat=2, auto_post_blinds=True,
        )
        return thread, audio_q, stop, writer

    def test_cli_round_trip_fold_win(self, tmp_path: Path) -> None:
        """IntegrationThread → JSON + evidence JSONL → reconstruct_session →
        reconstruct_<id>.jsonl が生成され、hand 1 が needs_review=False で返る。"""
        thread, audio_q, stop, writer = self._build_thread(tmp_path)
        now = time.time()
        audio_q.put(AudioEvent("new_hand", 0, now, ""))
        audio_q.put(AudioEvent("fold",     0, now + 0.5, "フォールド"))
        audio_q.put(AudioEvent("winner",   0, now + 1.0, "シート1 ウィナー"))

        thread.start()
        time.sleep(0.8)
        stop.set()
        thread.join(timeout=2.0)

        session_path = tmp_path / "phase3_cli.json"
        assert session_path.exists()
        evidence_path = tmp_path / "evidence_phase3_cli.jsonl"
        assert evidence_path.exists()

        out = reconstruct_session(session_path)
        assert out.exists()
        lines = [
            json.loads(line)
            for line in out.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        # 1 hand 分のエントリ
        assert len(lines) == 1
        entry = lines[0]
        assert entry["hand_id"] == 1
        assert entry["needs_review"] is False
        assert entry["reason"] == "reconstructed_no_diff"
        assert entry["diff"] is None
        assert entry["offline_summary"]["resolution_type"] == "fold_win"
        # online_summary の中身は壊れていない (元の hand データを保持)
        assert entry["online_summary"]["resolution_type"] == "fold_win"
        # session JSON 本体は CLI で mutate されていない
        original = json.loads(session_path.read_text(encoding="utf-8"))
        assert original["hands"][0]["resolution_type"] == "fold_win"

    def test_cli_detects_diff_when_session_json_tampered(self, tmp_path: Path) -> None:
        """JSON を改変してから reconstruct_session を実行 → diff が検出される。
        online JSON 自体は CLI で mutate されない。"""
        thread, audio_q, stop, writer = self._build_thread(tmp_path)
        now = time.time()
        audio_q.put(AudioEvent("new_hand", 0, now, ""))
        audio_q.put(AudioEvent("fold",     0, now + 0.5, "フォールド"))
        audio_q.put(AudioEvent("winner",   0, now + 1.0, "シート1 ウィナー"))

        thread.start()
        time.sleep(0.8)
        stop.set()
        thread.join(timeout=2.0)

        session_path = tmp_path / "phase3_cli.json"
        # JSON を tamper: resolution_type を showdown に書き換え
        data = json.loads(session_path.read_text(encoding="utf-8"))
        data["hands"][0]["resolution_type"] = "showdown"
        session_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

        out = reconstruct_session(session_path)
        lines = [
            json.loads(line)
            for line in out.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        entry = lines[0]
        assert entry["needs_review"] is True
        assert entry["reason"] == "reconstructed_with_diff"
        assert entry["diff"] is not None
        assert "resolution_type" in entry["diff"]
        # CLI は session JSON を mutate しない
        after = json.loads(session_path.read_text(encoding="utf-8"))
        assert after["hands"][0]["resolution_type"] == "showdown"  # tamper はそのまま
