"""tests/test_phase7.py

Phase 7: RFID 統合 confidence スコアリングのテスト。
- calc_confidence() の全組み合わせ
- IntegrationThread の RFID マッチング
- role="board" RFIDEvent → HandSummary.board 更新
- ±MATCH_WINDOW 境界・席不一致のガード
"""
from __future__ import annotations

import threading
import time
from pathlib import Path


from core.event_queue import make_audio_queue, make_camera_queue, make_rfid_queue
from core.events import AudioEvent, CameraEvent, RFIDEvent
from core.game_state import GameStateManager, PlayerState
from core.hand_log import ActionRecord
from integration.engine import (
    MATCH_WINDOW,
    IntegrationThread,
    calc_confidence,
    _CONF_AUDIO_CAMERA,
    _CONF_AUDIO_ONLY,
    _CONF_RFID_AUDIO,
    _CONF_RFID_AUDIO_CAMERA,
    _CONF_RFID_CAMERA,
    _CONF_RFID_ONLY,
    _CONF_CAMERA_ONLY,
)
from output.json_writer import JsonWriter


# ――― calc_confidence ―――

class TestCalcConfidence:
    def test_all_three(self):
        assert calc_confidence(True, True, True) == _CONF_RFID_AUDIO_CAMERA

    def test_rfid_audio(self):
        assert calc_confidence(True, True, False) == _CONF_RFID_AUDIO

    def test_rfid_camera(self):
        assert calc_confidence(True, False, True) == _CONF_RFID_CAMERA

    def test_rfid_only(self):
        assert calc_confidence(True, False, False) == _CONF_RFID_ONLY

    def test_audio_camera(self):
        assert calc_confidence(False, True, True) == _CONF_AUDIO_CAMERA

    def test_audio_only(self):
        assert calc_confidence(False, True, False) == _CONF_AUDIO_ONLY

    def test_camera_only(self):
        assert calc_confidence(False, False, True) == _CONF_CAMERA_ONLY

    def test_none(self):
        assert calc_confidence(False, False, False) == 0.0

    def test_order_rfid_beats_audio_camera(self):
        # RFID+audio > audio+camera
        assert _CONF_RFID_AUDIO > _CONF_AUDIO_CAMERA
        # RFID+audio+camera = 1.0 (最大)
        assert _CONF_RFID_AUDIO_CAMERA == 1.0


# ――― フィクスチャ ―――

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


# ――― RFID シートマッチング ―――

class TestRFIDSeatMatching:
    def test_rfid_audio_match(self, tmp_path: Path):
        """RFID + audio → confidence = _CONF_RFID_AUDIO."""
        gs = _make_game()
        audio_q = make_audio_queue()
        rfid_q = make_rfid_queue()
        writer = JsonWriter(log_dir=tmp_path, session_id="s1")
        stop = threading.Event()
        captured: list[ActionRecord] = []

        thread = IntegrationThread(
            audio_queue=audio_q, game_state=gs, json_writer=writer,
            rfid_queue=rfid_q, on_action=captured.append, stop_event=stop,
        )

        now = time.time()
        rfid_q.put(RFIDEvent(
            tag_id="04:AA", card="Ah", reader_id="reader_0",
            role="seat", seat=1, timestamp=now - 0.5, raw_tag_id="04:AA",
        ))
        audio_q.put(AudioEvent(action="bet", amount=500, timestamp=now, raw_text="ベット500"))

        _run_thread(thread, stop)

        assert len(captured) == 1
        rec = captured[0]
        assert rec.source == {"camera": False, "audio": True, "rfid": True}
        assert rec.confidence == _CONF_RFID_AUDIO

    def test_rfid_audio_camera_match(self, tmp_path: Path):
        """RFID + audio + camera → confidence = 1.0."""
        gs = _make_game()
        audio_q = make_audio_queue()
        camera_q = make_camera_queue()
        rfid_q = make_rfid_queue()
        writer = JsonWriter(log_dir=tmp_path, session_id="s2")
        stop = threading.Event()
        captured: list[ActionRecord] = []

        thread = IntegrationThread(
            audio_queue=audio_q, game_state=gs, json_writer=writer,
            camera_queue=camera_q, rfid_queue=rfid_q,
            on_action=captured.append, stop_event=stop,
        )

        now = time.time()
        rfid_q.put(RFIDEvent(
            tag_id="04:AA", card="Kd", reader_id="reader_0",
            role="seat", seat=1, timestamp=now - 0.3, raw_tag_id="04:AA",
        ))
        camera_q.put(CameraEvent(seat=1, timestamp=now - 0.8))
        audio_q.put(AudioEvent(action="raise", amount=800, timestamp=now, raw_text="レイズ800"))

        _run_thread(thread, stop)

        assert len(captured) == 1
        rec = captured[0]
        assert rec.source == {"camera": True, "audio": True, "rfid": True}
        assert rec.confidence == _CONF_RFID_AUDIO_CAMERA

    def test_rfid_wrong_seat_no_match(self, tmp_path: Path):
        """席番号が異なる RFID イベントはマッチしない。"""
        gs = _make_game()
        audio_q = make_audio_queue()
        rfid_q = make_rfid_queue()
        writer = JsonWriter(log_dir=tmp_path, session_id="s3")
        stop = threading.Event()
        captured: list[ActionRecord] = []

        thread = IntegrationThread(
            audio_queue=audio_q, game_state=gs, json_writer=writer,
            rfid_queue=rfid_q, on_action=captured.append, stop_event=stop,
        )

        now = time.time()
        # 席2のRFIDだが現在ターンは席1
        rfid_q.put(RFIDEvent(
            tag_id="04:BB", card="Qh", reader_id="reader_1",
            role="seat", seat=2, timestamp=now, raw_tag_id="04:BB",
        ))
        audio_q.put(AudioEvent(action="check", amount=0, timestamp=now, raw_text="チェック"))

        _run_thread(thread, stop)

        assert len(captured) == 1
        assert captured[0].source["rfid"] is False
        assert captured[0].confidence == _CONF_AUDIO_ONLY

    def test_rfid_outside_match_window(self, tmp_path: Path):
        """±MATCH_WINDOW 秒を超えた RFID イベントはマッチしない。"""
        gs = _make_game()
        audio_q = make_audio_queue()
        rfid_q = make_rfid_queue()
        writer = JsonWriter(log_dir=tmp_path, session_id="s4")
        stop = threading.Event()
        captured: list[ActionRecord] = []

        thread = IntegrationThread(
            audio_queue=audio_q, game_state=gs, json_writer=writer,
            rfid_queue=rfid_q, on_action=captured.append, stop_event=stop,
        )

        now = time.time()
        rfid_q.put(RFIDEvent(
            tag_id="04:CC", card="", reader_id="reader_0",
            role="seat", seat=1, timestamp=now - MATCH_WINDOW - 0.1, raw_tag_id="04:CC",
        ))
        audio_q.put(AudioEvent(action="fold", amount=0, timestamp=now, raw_text="フォールド"))

        _run_thread(thread, stop)

        assert len(captured) == 1
        assert captured[0].source["rfid"] is False

    def test_rfid_consumed_once(self, tmp_path: Path):
        """1つの RFID イベントは1アクションにのみ使われる。"""
        gs = _make_game()
        audio_q = make_audio_queue()
        rfid_q = make_rfid_queue()
        writer = JsonWriter(log_dir=tmp_path, session_id="s5")
        stop = threading.Event()
        captured: list[ActionRecord] = []

        thread = IntegrationThread(
            audio_queue=audio_q, game_state=gs, json_writer=writer,
            rfid_queue=rfid_q, on_action=captured.append, stop_event=stop,
        )

        now = time.time()
        rfid_q.put(RFIDEvent(
            tag_id="04:DD", card="", reader_id="reader_0",
            role="seat", seat=1, timestamp=now, raw_tag_id="04:DD",
        ))
        audio_q.put(AudioEvent(action="bet",  amount=300, timestamp=now, raw_text="ベット300"))
        audio_q.put(AudioEvent(action="call", amount=300, timestamp=now, raw_text="コール300"))

        _run_thread(thread, stop, sleep=0.5)

        assert len(captured) == 2
        assert captured[0].source["rfid"] is True
        assert captured[0].confidence == _CONF_RFID_AUDIO
        assert captured[1].source["rfid"] is False
        assert captured[1].confidence == _CONF_AUDIO_ONLY


# ――― role="board" RFIDEvent ―――

class TestBoardRFIDEvents:
    def _make_summary_via_thread(
        self, tmp_path: Path, board_events: list[RFIDEvent]
    ) -> dict:
        """ハンドを完了させて JsonWriter に保存された dict を返す。"""
        gs = _make_game()
        audio_q = make_audio_queue()
        rfid_q = make_rfid_queue()
        writer = JsonWriter(log_dir=tmp_path, session_id="board_test")
        stop = threading.Event()

        thread = IntegrationThread(
            audio_queue=audio_q, game_state=gs, json_writer=writer,
            rfid_queue=rfid_q, stop_event=stop,
        )

        thread.start()
        time.sleep(0.05)

        now = time.time()
        for ev in board_events:
            rfid_q.put(ev)
        # ハンドを完了させる
        audio_q.put(AudioEvent(action="bet",    amount=500, timestamp=now, raw_text=""))
        audio_q.put(AudioEvent(action="winner", amount=0,   timestamp=now + 0.01,
                               raw_text="シート1 ウィナー"))

        time.sleep(0.3)
        stop.set()
        thread.join(timeout=2)

        import json
        data = json.loads(writer.path.read_text(encoding="utf-8"))
        return data["hands"][0]

    def test_board_rfid_cards_in_summary(self, tmp_path: Path):
        """role="board" イベントが HandSummary.board に蓄積される。"""
        board_events = [
            RFIDEvent("04:F1", "Ah", "board_reader", "board", None, time.time(), "04:F1"),
            RFIDEvent("04:F2", "Kd", "board_reader", "board", None, time.time(), "04:F2"),
            RFIDEvent("04:F3", "Qh", "board_reader", "board", None, time.time(), "04:F3"),
        ]
        hand = self._make_summary_via_thread(tmp_path, board_events)

        assert hand["board"] == ["Ah", "Kd", "Qh"]
        assert hand["board_source"] == "rfid"

    def test_empty_card_board_event_ignored(self, tmp_path: Path):
        """card="" の board イベントは board リストに追加されない。"""
        board_events = [
            RFIDEvent("04:F1", "",   "board_reader", "board", None, time.time(), "04:F1"),
            RFIDEvent("04:F2", "Ts", "board_reader", "board", None, time.time(), "04:F2"),
        ]
        hand = self._make_summary_via_thread(tmp_path, board_events)

        assert hand["board"] == ["Ts"]

    def test_no_board_events_empty_board(self, tmp_path: Path):
        """board イベントがない場合、board は空リスト、board_source は空文字。"""
        hand = self._make_summary_via_thread(tmp_path, [])

        assert hand["board"] == []
        assert hand["board_source"] == ""

    def test_new_hand_clears_board(self, tmp_path: Path):
        """new_hand イベントで board_cards がリセットされる。"""
        gs = _make_game()
        audio_q = make_audio_queue()
        rfid_q = make_rfid_queue()
        writer = JsonWriter(log_dir=tmp_path, session_id="board_reset")
        stop = threading.Event()
        thread = IntegrationThread(
            audio_queue=audio_q, game_state=gs, json_writer=writer,
            rfid_queue=rfid_q, stop_event=stop,
        )
        thread.start()
        time.sleep(0.05)

        now = time.time()
        # ハンド1: ボードカードあり
        rfid_q.put(RFIDEvent("04:X1", "Ah", "r", "board", None, now, "04:X1"))
        audio_q.put(AudioEvent("bet", 100, now, ""))
        audio_q.put(AudioEvent("winner", 0, now + 0.01, "シート1 ウィナー"))

        time.sleep(0.3)

        # ハンド2: ボードカードなし
        audio_q.put(AudioEvent("new_hand", 0, now + 0.4, ""))
        audio_q.put(AudioEvent("bet", 200, now + 0.41, ""))
        audio_q.put(AudioEvent("winner", 0, now + 0.42, "シート1 ウィナー"))

        time.sleep(0.3)
        stop.set()
        thread.join(timeout=2)

        import json
        data = json.loads(writer.path.read_text(encoding="utf-8"))
        assert data["hands"][0]["board"] == ["Ah"]
        assert data["hands"][1]["board"] == []
