"""tests/test_phase_a_hardening.py

Phase A (コア堅牢化) の回帰テスト。

A1: RFID カードがマスター未解決（card 空）のままハンドが進んだ場合、
    そのハンドの HandSummary.review_required が True になることを検証する。
    従来は logger.warning のみで、オペレーターが検出失敗に気付けなかった。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from core.event_queue import make_audio_queue, make_rfid_queue
from core.events import AudioEvent, RFIDEvent
from core.game_state import GameStateManager, PlayerState
from integration.engine import IntegrationThread
from output.json_writer import JsonWriter


def _make_game() -> GameStateManager:
    players = [
        PlayerState(seat=1, name="Alice", stack=10000),
        PlayerState(seat=2, name="Bob",   stack=10000),
    ]
    gs = GameStateManager(players=players, sb=100, bb=200)
    gs.new_hand()
    return gs


def _run_thread(thread: IntegrationThread, stop: threading.Event, sleep: float = 0.3) -> None:
    thread.start()
    time.sleep(sleep)
    stop.set()
    thread.join(timeout=2.0)


def _written_hand(writer: JsonWriter) -> dict:
    data = json.loads(writer.path.read_text(encoding="utf-8"))
    return data["hands"][-1]


class TestRFIDUnresolvedCardFlagsReview:
    def test_board_rfid_no_card_sets_review_required(self, tmp_path: Path) -> None:
        """board RFID が card 未解決 → ハンドが review_required=True になる。"""
        gs = _make_game()
        audio_q = make_audio_queue()
        rfid_q = make_rfid_queue()
        writer = JsonWriter(log_dir=tmp_path, session_id="s_board_nocard")
        stop = threading.Event()

        now = time.time()
        # カードマスター未登録 → card は空文字
        rfid_q.put(RFIDEvent(
            tag_id="04:UNKNOWN", card="", reader_id="board_1",
            role="board", seat=None, timestamp=now, raw_tag_id="04:UNKNOWN",
            board_index=1,
        ))
        audio_q.put(AudioEvent(action="winner", amount=0, timestamp=now + 0.01,
                               raw_text="シート1 ウィナー"))

        thread = IntegrationThread(
            audio_queue=audio_q, game_state=gs, json_writer=writer,
            rfid_queue=rfid_q, stop_event=stop,
        )
        _run_thread(thread, stop)

        assert _written_hand(writer)["review_required"] is True

    def test_seat_rfid_no_card_sets_review_required(self, tmp_path: Path) -> None:
        """seat RFID が card 未解決 → ハンドが review_required=True になる。"""
        gs = _make_game()
        audio_q = make_audio_queue()
        rfid_q = make_rfid_queue()
        writer = JsonWriter(log_dir=tmp_path, session_id="s_seat_nocard")
        stop = threading.Event()

        now = time.time()
        rfid_q.put(RFIDEvent(
            tag_id="04:UNKNOWN", card="", reader_id="seat_1",
            role="seat", seat=1, timestamp=now, raw_tag_id="04:UNKNOWN",
        ))
        audio_q.put(AudioEvent(action="winner", amount=0, timestamp=now + 0.01,
                               raw_text="シート1 ウィナー"))

        thread = IntegrationThread(
            audio_queue=audio_q, game_state=gs, json_writer=writer,
            rfid_queue=rfid_q, stop_event=stop,
        )
        _run_thread(thread, stop)

        assert _written_hand(writer)["review_required"] is True

    def test_resolved_card_does_not_set_review_required(self, tmp_path: Path) -> None:
        """card が解決済み（valid）なら review_required は立たない（誤検知ガード）。"""
        gs = _make_game()
        audio_q = make_audio_queue()
        rfid_q = make_rfid_queue()
        writer = JsonWriter(log_dir=tmp_path, session_id="s_valid")
        stop = threading.Event()

        now = time.time()
        rfid_q.put(RFIDEvent(
            tag_id="04:AA", card="Ah", reader_id="board_1",
            role="board", seat=None, timestamp=now, raw_tag_id="04:AA",
            board_index=1,
        ))
        audio_q.put(AudioEvent(action="winner", amount=0, timestamp=now + 0.01,
                               raw_text="シート1 ウィナー"))

        thread = IntegrationThread(
            audio_queue=audio_q, game_state=gs, json_writer=writer,
            rfid_queue=rfid_q, stop_event=stop,
        )
        _run_thread(thread, stop)

        assert _written_hand(writer)["review_required"] is False

    def test_flag_resets_on_new_hand(self, tmp_path: Path) -> None:
        """new_hand で要レビューフラグがリセットされ、後続ハンドに引きずらない。"""
        gs = _make_game()
        audio_q = make_audio_queue()
        rfid_q = make_rfid_queue()
        writer = JsonWriter(log_dir=tmp_path, session_id="s_reset")
        stop = threading.Event()

        now = time.time()
        # ハンド1: 未解決カード → review_required True
        rfid_q.put(RFIDEvent(
            tag_id="04:UNKNOWN", card="", reader_id="board_1",
            role="board", seat=None, timestamp=now, raw_tag_id="04:UNKNOWN",
            board_index=1,
        ))
        audio_q.put(AudioEvent(action="winner", amount=0, timestamp=now + 0.01,
                               raw_text="シート1 ウィナー"))
        # ハンド2: クリーン → review_required False
        audio_q.put(AudioEvent(action="new_hand", amount=0, timestamp=now + 0.02,
                               raw_text="ハンド開始"))
        audio_q.put(AudioEvent(action="winner", amount=0, timestamp=now + 0.03,
                               raw_text="シート2 ウィナー"))

        thread = IntegrationThread(
            audio_queue=audio_q, game_state=gs, json_writer=writer,
            rfid_queue=rfid_q, stop_event=stop,
        )
        _run_thread(thread, stop)

        data = json.loads(writer.path.read_text(encoding="utf-8"))
        assert data["hands"][0]["review_required"] is True
        assert data["hands"][1]["review_required"] is False

    def test_finalize_resets_review_flag(self, tmp_path: Path) -> None:
        """_finalize_hand はサマリーに反映後フラグをリセットする（_current_actions と対称）。"""
        gs = _make_game()
        writer = JsonWriter(log_dir=tmp_path, session_id="s_finalize_reset")
        thread = IntegrationThread(
            audio_queue=make_audio_queue(), game_state=gs, json_writer=writer,
            stop_event=threading.Event(),
        )

        thread._hand_needs_review = True
        thread._finalize_hand(1)

        # 確定したサマリーには True が反映され、フラグ自体はリセットされる。
        assert _written_hand(writer)["review_required"] is True
        assert thread._hand_needs_review is False
