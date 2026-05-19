"""tests/test_hand_boundary.py

Phase 2-C: HandBoundaryDetector + replay_hand.extract_hand_windows + IntegrationThread
の hand window 収集を検証する。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from core.event_queue import EventQueue
from core.events import AudioEvent, CameraEvent, RFIDEvent
from core.game_state import GameStateManager, PlayerState
from core.hand_boundary import BoundaryEvent, HandBoundaryDetector
from integration.engine import IntegrationThread
from output.json_writer import JsonWriter
from output.replay_hand import EvidenceRecord, extract_hand_windows


def _audio(action: str, ts: float = 0.0, raw: str = "") -> AudioEvent:
    return AudioEvent(action=action, amount=0, timestamp=ts, raw_text=raw)


def _rfid_board(card: str, board_index: int, ts: float) -> RFIDEvent:
    return RFIDEvent(
        tag_id=f"board_tag_{board_index}",
        card=card,
        reader_id=f"board_{board_index}",
        role="board",
        seat=None,
        timestamp=ts,
        raw_tag_id=f"board_tag_{board_index}",
        board_index=board_index,
    )


def _rfid_seat(seat: int, card: str, ts: float) -> RFIDEvent:
    return RFIDEvent(
        tag_id=f"seat_{seat}_{card}",
        card=card,
        reader_id=f"seat_{seat}",
        role="seat",
        seat=seat,
        timestamp=ts,
        raw_tag_id=f"seat_{seat}_{card}",
    )


# ────────────────────────────────────────────────────────────────────────────
# HandBoundaryDetector unit tests
# ────────────────────────────────────────────────────────────────────────────


class TestDetectorAudio:
    def test_new_hand_emits_start(self) -> None:
        det = HandBoundaryDetector()
        out = det.observe_audio_event(_audio("new_hand", ts=1.0))
        assert len(out) == 1
        assert out[0].kind == "start"
        assert out[0].hand_id == 1
        assert out[0].reason == "audio_new_hand"
        assert det.in_hand

    def test_winner_emits_end(self) -> None:
        det = HandBoundaryDetector()
        det.observe_audio_event(_audio("new_hand", ts=1.0))
        out = det.observe_audio_event(_audio("winner", ts=5.0, raw="シート1 ウィナー"))
        assert len(out) == 1
        assert out[0].kind == "end"
        assert out[0].hand_id == 1
        assert out[0].reason == "audio_winner"
        assert not det.in_hand

    def test_consecutive_new_hands_increment_id(self) -> None:
        det = HandBoundaryDetector()
        det.observe_audio_event(_audio("new_hand", ts=1.0))
        det.observe_audio_event(_audio("winner", ts=2.0))
        det.observe_audio_event(_audio("new_hand", ts=3.0))
        det.observe_audio_event(_audio("winner", ts=4.0))
        det.observe_audio_event(_audio("new_hand", ts=5.0))
        assert det.current_hand_id() == 3

    def test_new_hand_while_in_hand_emits_end_then_start(self) -> None:
        """前 hand の winner audio が無いまま次 new_hand が来た場合、
        [end, start] が一度に返る。"""
        det = HandBoundaryDetector()
        det.observe_audio_event(_audio("new_hand", ts=1.0))
        out = det.observe_audio_event(_audio("new_hand", ts=5.0))
        assert len(out) == 2
        assert out[0].kind == "end"
        assert out[0].hand_id == 1
        assert out[1].kind == "start"
        assert out[1].hand_id == 2

    def test_winner_without_in_hand_emits_nothing(self) -> None:
        det = HandBoundaryDetector()
        out = det.observe_audio_event(_audio("winner", ts=1.0))
        assert out == []

    def test_irrelevant_audio_emits_nothing(self) -> None:
        det = HandBoundaryDetector()
        det.observe_audio_event(_audio("new_hand", ts=1.0))
        out = det.observe_audio_event(_audio("bet", ts=2.0))
        assert out == []


class TestDetectorBoardCleared:
    def test_board_non_empty_then_empty_with_quiet_emits_end(self) -> None:
        """board が「空 → 非空 → 空 → BOARD_EMPTY_QUIET_SEC 静止」で end 発行。"""
        det = HandBoundaryDetector(board_empty_quiet_sec=1.5)
        det.observe_audio_event(_audio("new_hand", ts=10.0))
        # board に 3 枚出現
        det.observe_board_state(["Ah", "Kd", "Qc"], now=11.0)
        assert det.in_hand
        # board が空に
        out_at_clear = det.observe_board_state([], now=12.0)
        # 直後はまだ quiet 秒経ってないので end は出ない
        assert out_at_clear == []
        assert det.in_hand
        # 1.0 秒後でもまだ閾値未満 (12.0 + 1.5 = 13.5)
        out_short = det.observe_board_state([], now=12.5)
        assert out_short == []
        # 13.5 で閾値到達 → end
        out_quiet = det.observe_board_state([], now=13.5)
        assert len(out_quiet) == 1
        assert out_quiet[0].kind == "end"
        assert out_quiet[0].reason == "board_cleared"
        assert not det.in_hand

    def test_board_refill_cancels_pending_end(self) -> None:
        """board が空に転落した後に再び非空になれば quiet タイマーは解除される。"""
        det = HandBoundaryDetector(board_empty_quiet_sec=1.5)
        det.observe_audio_event(_audio("new_hand", ts=10.0))
        det.observe_board_state(["Ah", "Kd", "Qc"], now=11.0)
        det.observe_board_state([], now=12.0)            # 空に転落
        det.observe_board_state(["2c"], now=12.5)         # 再び非空
        out = det.observe_board_state([], now=14.5)       # 再度空、quiet タイマー再開
        # 14.5 のリセットからまだ 0 秒 → end 出ない
        assert out == []
        out = det.observe_board_state([], now=16.5)       # 14.5 + 1.5 = 16.0 を超えた
        assert len(out) == 1
        assert out[0].kind == "end"
        assert out[0].reason == "board_cleared"

    def test_tick_can_advance_time(self) -> None:
        """イベントが届かない時間帯でも tick(now) で end を出せる。"""
        det = HandBoundaryDetector(board_empty_quiet_sec=1.0)
        det.observe_audio_event(_audio("new_hand", ts=10.0))
        det.observe_board_state(["Ah"], now=11.0)
        det.observe_board_state([], now=12.0)
        out = det.tick(now=13.0)
        assert len(out) == 1
        assert out[0].kind == "end"


class TestDetectorHoleAppearance:
    def test_two_seats_with_hole_emit_start_when_idle(self) -> None:
        det = HandBoundaryDetector()
        # idle 状態かつ board が空、2 seat に hole cards が現れる
        out = det.observe_hole_state({1: ["Ah", "Kd"], 2: ["7c", "2s"]}, now=1.0)
        assert len(out) == 1
        assert out[0].kind == "start"
        assert out[0].reason == "hole_cards_appeared"

    def test_hole_one_seat_does_not_start(self) -> None:
        """seat 1 だけに hole cards が現れても start にはならない。"""
        det = HandBoundaryDetector()
        out = det.observe_hole_state({1: ["Ah", "Kd"]}, now=1.0)
        assert out == []

    def test_hole_during_in_hand_does_not_start_again(self) -> None:
        """既に in_hand のときに hole 観測しても start は再発行されない。"""
        det = HandBoundaryDetector()
        det.observe_audio_event(_audio("new_hand", ts=1.0))
        out = det.observe_hole_state({1: ["Ah", "Kd"], 2: ["7c", "2s"]}, now=2.0)
        assert out == []


# ────────────────────────────────────────────────────────────────────────────
# replay_hand.extract_hand_windows
# ────────────────────────────────────────────────────────────────────────────


def _rec_audio(action: str, ts: float) -> EvidenceRecord:
    ev = _audio(action, ts=ts)
    return EvidenceRecord(timestamp=ts, kind="audio", event=ev)


class TestExtractHandWindows:
    def test_two_hands_split_correctly(self) -> None:
        records = [
            _rec_audio("new_hand", 1.0),
            _rec_audio("bet",      1.1),
            _rec_audio("fold",     1.2),
            _rec_audio("winner",   1.3),       # hand 1 end
            _rec_audio("new_hand", 2.0),       # hand 2 start
            _rec_audio("call",     2.1),
            _rec_audio("winner",   2.2),       # hand 2 end
        ]
        windows = extract_hand_windows(records)
        assert sorted(windows.keys()) == [1, 2]
        # hand 1: new_hand + bet + fold + winner = 4 events
        actions1 = [r.event.action for r in windows[1]]
        assert actions1 == ["new_hand", "bet", "fold", "winner"]
        # hand 2: new_hand + call + winner = 3 events
        actions2 = [r.event.action for r in windows[2]]
        assert actions2 == ["new_hand", "call", "winner"]

    def test_open_hand_without_end_is_excluded(self) -> None:
        """end 境界が出ない hand は windows に含まれない。"""
        records = [
            _rec_audio("new_hand", 1.0),
            _rec_audio("bet",      1.1),
            # winner なし
        ]
        windows = extract_hand_windows(records)
        assert windows == {}

    def test_implicit_end_via_consecutive_new_hand(self) -> None:
        """新 new_hand が来ると前 hand は trigger event を含まずに閉じる。"""
        records = [
            _rec_audio("new_hand", 1.0),
            _rec_audio("bet",      1.1),
            _rec_audio("new_hand", 2.0),   # 前 hand 暗黙終了 + 新 hand 開始
            _rec_audio("winner",   2.1),
        ]
        windows = extract_hand_windows(records)
        # hand 1: new_hand + bet (winner なしで暗黙終了)
        assert [r.event.action for r in windows[1]] == ["new_hand", "bet"]
        # hand 2: new_hand + winner (新 hand 開始 trigger 自身が含まれる)
        assert [r.event.action for r in windows[2]] == ["new_hand", "winner"]


# ────────────────────────────────────────────────────────────────────────────
# IntegrationThread 結合テスト
# ────────────────────────────────────────────────────────────────────────────


class TestIntegrationThreadWindowCollection:
    def _build_thread(self, tmp_path: Path):
        players = [
            PlayerState(seat=1, name="A", stack=10000),
            PlayerState(seat=2, name="B", stack=10000),
        ]
        gs = GameStateManager(players=players, sb=100, bb=200)
        audio_q = EventQueue()
        writer = JsonWriter(log_dir=tmp_path, session_id="phase2c_windows")
        stop = threading.Event()
        thread = IntegrationThread(
            audio_queue=audio_q,
            game_state=gs,
            json_writer=writer,
            stop_event=stop,
            initial_button_seat=2,
            auto_post_blinds=True,
        )
        return thread, audio_q, stop, writer

    def test_completed_hands_filled_after_full_hand(self, tmp_path: Path) -> None:
        """new_hand → fold → winner で _completed_hands に 1 件溜まる。"""
        thread, audio_q, stop, _writer = self._build_thread(tmp_path)
        now = time.time()
        audio_q.put(AudioEvent("new_hand", 0, now, ""))
        audio_q.put(AudioEvent("fold",     0, now + 0.1, "フォールド"))
        audio_q.put(AudioEvent("winner",   0, now + 0.2, "シート1 ウィナー"))

        thread.start()
        time.sleep(0.8)
        stop.set()
        thread.join(timeout=2.0)

        # boundary detector の hand_id は new_hand で 1 にインクリメント
        assert thread._boundary_detector.current_hand_id() == 1
        # _completed_hands[1] に events が貯まっている
        assert 1 in thread._completed_hands
        actions = [
            r.event.action for r in thread._completed_hands[1]
            if r.kind == "audio" and r.event is not None
        ]
        # new_hand と winner は trigger なので含まれる、fold は通常 event
        assert "new_hand" in actions
        assert "fold" in actions
        assert "winner" in actions

    def test_no_boundary_no_completed_hand(self, tmp_path: Path) -> None:
        """boundary に達しない (winner 無し) 場合は _completed_hands は空。"""
        thread, audio_q, stop, _writer = self._build_thread(tmp_path)
        now = time.time()
        audio_q.put(AudioEvent("new_hand", 0, now, ""))
        audio_q.put(AudioEvent("fold",     0, now + 0.1, "フォールド"))
        # winner 無し

        thread.start()
        time.sleep(0.6)
        stop.set()
        thread.join(timeout=2.0)

        # hand が開いただけで閉じていない
        assert thread._boundary_detector.in_hand is True
        assert thread._completed_hands == {}
        # current buffer には events が溜まっている
        assert len(thread._current_hand_events) >= 1

    def test_reconstructor_hook_invoked_on_end(self, tmp_path: Path) -> None:
        """end boundary + _finalize_hand 経由で HandReconstructor が advisory 実行される。

        Phase 4-A: ``audio_winner`` の end boundary では ``_apply_boundaries`` 内では
        invoke を遅延し、その直後の ``_finalize_hand`` から online_summary 付きで
        呼ばれる。結果 ``reason`` は ``"reconstructed_no_diff"`` または
        ``"reconstructed_with_diff"`` (Phase 2-C skeleton 時代の
        ``"reconstruction_skipped"`` ではない)。
        """
        thread, audio_q, stop, _writer = self._build_thread(tmp_path)
        now = time.time()
        audio_q.put(AudioEvent("new_hand", 0, now, ""))
        audio_q.put(AudioEvent("fold",     0, now + 0.1, "フォールド"))
        audio_q.put(AudioEvent("winner",   0, now + 0.2, "シート1 ウィナー"))

        thread.start()
        time.sleep(0.8)
        stop.set()
        thread.join(timeout=2.0)

        # Phase 4-A: advisory reconstruct の結果が in-memory に保持されている
        assert thread._last_reconstruction is not None
        assert thread._last_reconstruction.reason in {
            "reconstructed_no_diff", "reconstructed_with_diff",
        }
        # hand_id 別 dict にも入っている
        assert 1 in thread._last_reconstruction_by_hand_id
        # _last_summary_by_hand_id にも online summary が保持されている
        assert 1 in thread._last_summary_by_hand_id


# ────────────────────────────────────────────────────────────────────────────
# replay_hand.load_evidence_log (round-trip via EvidenceLogWriter)
# ────────────────────────────────────────────────────────────────────────────


class TestLoadEvidenceLog:
    def test_round_trip_audio_rfid_camera(self, tmp_path: Path) -> None:
        """EvidenceLogWriter で書いた JSONL を load_evidence_log で読み戻し、
        event 再構築が動くこと。"""
        from output.evidence_log import EvidenceLogWriter
        from output.replay_hand import load_evidence_log

        writer = EvidenceLogWriter(log_dir=tmp_path, session_id="round_trip")
        writer.write_audio(_audio("new_hand", ts=1.0, raw="ハンド開始"))
        writer.write_audio(_audio("fold", ts=1.1, raw="フォールド"))
        writer.write_rfid(_rfid_board("Ah", 1, ts=1.2))
        writer.write_camera(CameraEvent(seat=2, timestamp=1.3))
        writer.write_audio(_audio("winner", ts=1.4, raw="シート1 ウィナー"))
        writer.close()

        records = load_evidence_log(writer.path)
        # audio:3 + rfid:1 + camera:1 = 5 件
        assert len(records) == 5
        kinds = [r.kind for r in records]
        assert kinds.count("audio") == 3
        assert kinds.count("rfid") == 1
        assert kinds.count("camera") == 1
        # extract_hand_windows をかけると hand 1 が 5 件揃って完結する
        windows = extract_hand_windows(records)
        assert list(windows.keys()) == [1]
        assert len(windows[1]) == 5
