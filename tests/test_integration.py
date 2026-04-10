"""tests/test_integration.py

Phase 3: IntegrationThread の ±2秒マッチングと confidence スコアのテスト。
"""
from __future__ import annotations

import queue
import threading
import time
from pathlib import Path

import pytest

from core.event_queue import make_audio_queue, make_camera_queue
from core.events import AudioEvent, CameraEvent
from core.game_state import GameStateManager, PlayerState
from core.hand_log import ActionRecord
from integration.engine import MATCH_WINDOW, IntegrationThread, _CONF_AUDIO_CAMERA, _CONF_AUDIO_ONLY
from output.json_writer import JsonWriter


# ――― フィクスチャ ―――

def _make_game() -> GameStateManager:
    players = [
        PlayerState(seat=1, name="Alice", stack=10000),
        PlayerState(seat=2, name="Bob",   stack=10000),
    ]
    gs = GameStateManager(players=players, sb=100, bb=200)
    gs.new_hand()
    return gs


def _run_thread(thread: IntegrationThread, stop: threading.Event, join_timeout: float = 2.0) -> None:
    thread.start()
    time.sleep(0.3)  # イベント処理を待つ
    stop.set()
    thread.join(timeout=join_timeout)


# ――― テストケース ―――

class TestAudioOnlyConfidence:
    """camera_queue なし（または同席カメライベントなし）の場合、confidence = _CONF_AUDIO_ONLY。"""

    def test_audio_only_no_camera_queue(self, tmp_path: Path) -> None:
        gs = _make_game()
        audio_q = make_audio_queue()
        writer = JsonWriter(log_dir=tmp_path, session_id="s1")
        stop = threading.Event()
        captured: list[ActionRecord] = []

        thread = IntegrationThread(
            audio_queue=audio_q,
            game_state=gs,
            json_writer=writer,
            camera_queue=None,
            on_action=captured.append,
            stop_event=stop,
        )

        audio_q.put(AudioEvent(action="bet", amount=500, timestamp=time.time(), raw_text="ベット500"))
        _run_thread(thread, stop)

        assert len(captured) == 1
        rec = captured[0]
        assert rec.source == {"camera": False, "audio": True, "rfid": False}
        assert rec.confidence == _CONF_AUDIO_ONLY

    def test_audio_only_with_camera_queue_but_no_events(self, tmp_path: Path) -> None:
        gs = _make_game()
        audio_q = make_audio_queue()
        camera_q = make_camera_queue()
        writer = JsonWriter(log_dir=tmp_path, session_id="s2")
        stop = threading.Event()
        captured: list[ActionRecord] = []

        thread = IntegrationThread(
            audio_queue=audio_q,
            game_state=gs,
            json_writer=writer,
            camera_queue=camera_q,
            on_action=captured.append,
            stop_event=stop,
        )

        audio_q.put(AudioEvent(action="check", amount=0, timestamp=time.time(), raw_text="チェック"))
        _run_thread(thread, stop)

        assert len(captured) == 1
        assert captured[0].confidence == _CONF_AUDIO_ONLY
        assert captured[0].source["camera"] is False


@pytest.mark.skip(reason="camera deprecated in spec v4.0")
class TestCameraCorroboration:
    """同席・±MATCH_WINDOW 秒以内の CameraEvent がある場合、confidence = _CONF_AUDIO_CAMERA。"""

    def test_camera_before_audio_within_window(self, tmp_path: Path) -> None:
        gs = _make_game()
        audio_q = make_audio_queue()
        camera_q = make_camera_queue()
        writer = JsonWriter(log_dir=tmp_path, session_id="s3")
        stop = threading.Event()
        captured: list[ActionRecord] = []

        thread = IntegrationThread(
            audio_queue=audio_q,
            game_state=gs,
            json_writer=writer,
            camera_queue=camera_q,
            on_action=captured.append,
            stop_event=stop,
        )

        now = time.time()
        # カメライベントを 1秒前のタイムスタンプで投入（ウィンドウ内）
        camera_q.put(CameraEvent(seat=1, timestamp=now - 1.0))
        audio_q.put(AudioEvent(action="raise", amount=800, timestamp=now, raw_text="レイズ800"))

        _run_thread(thread, stop)

        assert len(captured) == 1
        rec = captured[0]
        assert rec.source == {"camera": True, "audio": True, "rfid": False}
        assert rec.confidence == _CONF_AUDIO_CAMERA

    def test_camera_after_audio_within_window(self, tmp_path: Path) -> None:
        """AudioEvent より後のカメライベントも ±MATCH_WINDOW 内なのでマッチする。"""
        gs = _make_game()
        audio_q = make_audio_queue()
        camera_q = make_camera_queue()
        writer = JsonWriter(log_dir=tmp_path, session_id="s4")
        stop = threading.Event()
        captured: list[ActionRecord] = []

        thread = IntegrationThread(
            audio_queue=audio_q,
            game_state=gs,
            json_writer=writer,
            camera_queue=camera_q,
            on_action=captured.append,
            stop_event=stop,
        )

        now = time.time()
        # カメライベントを 1秒後のタイムスタンプで投入（ウィンドウ内）
        camera_q.put(CameraEvent(seat=1, timestamp=now + 1.0))
        audio_q.put(AudioEvent(action="call", amount=300, timestamp=now, raw_text="コール300"))

        _run_thread(thread, stop)

        assert len(captured) == 1
        rec = captured[0]
        assert rec.source["camera"] is True
        assert rec.confidence == _CONF_AUDIO_CAMERA


@pytest.mark.skip(reason="camera deprecated in spec v4.0")
class TestCameraWindowBoundary:
    """ウィンドウ外のカメライベントはマッチしない。"""

    def test_camera_outside_window_no_match(self, tmp_path: Path) -> None:
        gs = _make_game()
        audio_q = make_audio_queue()
        camera_q = make_camera_queue()
        writer = JsonWriter(log_dir=tmp_path, session_id="s5")
        stop = threading.Event()
        captured: list[ActionRecord] = []

        thread = IntegrationThread(
            audio_queue=audio_q,
            game_state=gs,
            json_writer=writer,
            camera_queue=camera_q,
            on_action=captured.append,
            stop_event=stop,
        )

        now = time.time()
        # ウィンドウ外（MATCH_WINDOW + 0.1 秒前）
        camera_q.put(CameraEvent(seat=1, timestamp=now - MATCH_WINDOW - 0.1))
        audio_q.put(AudioEvent(action="bet", amount=200, timestamp=now, raw_text="ベット200"))

        _run_thread(thread, stop)

        assert len(captured) == 1
        rec = captured[0]
        assert rec.source["camera"] is False
        assert rec.confidence == _CONF_AUDIO_ONLY

    def test_wrong_seat_camera_event_no_match(self, tmp_path: Path) -> None:
        """席番号が異なるカメライベントはマッチしない。"""
        gs = _make_game()
        audio_q = make_audio_queue()
        camera_q = make_camera_queue()
        writer = JsonWriter(log_dir=tmp_path, session_id="s6")
        stop = threading.Event()
        captured: list[ActionRecord] = []

        thread = IntegrationThread(
            audio_queue=audio_q,
            game_state=gs,
            json_writer=writer,
            camera_queue=camera_q,
            on_action=captured.append,
            stop_event=stop,
        )

        now = time.time()
        # 席2のカメライベントだが、現在のターンは席1
        camera_q.put(CameraEvent(seat=2, timestamp=now))
        audio_q.put(AudioEvent(action="fold", amount=0, timestamp=now, raw_text="フォールド"))

        _run_thread(thread, stop)

        assert len(captured) == 1
        assert captured[0].source["camera"] is False
        assert captured[0].confidence == _CONF_AUDIO_ONLY


@pytest.mark.skip(reason="camera deprecated in spec v4.0")
class TestCameraBufferExpiry:
    """古いカメライベントはバッファから破棄される。"""

    def test_expired_camera_event_is_not_matched(self, tmp_path: Path) -> None:
        from integration.engine import CAMERA_BUFFER_TTL
        gs = _make_game()
        audio_q = make_audio_queue()
        camera_q = make_camera_queue()
        writer = JsonWriter(log_dir=tmp_path, session_id="s7")
        stop = threading.Event()
        captured: list[ActionRecord] = []

        thread = IntegrationThread(
            audio_queue=audio_q,
            game_state=gs,
            json_writer=writer,
            camera_queue=camera_q,
            on_action=captured.append,
            stop_event=stop,
        )

        now = time.time()
        # CAMERA_BUFFER_TTL より古いタイムスタンプ → expire で削除される
        camera_q.put(CameraEvent(seat=1, timestamp=now - CAMERA_BUFFER_TTL - 0.1))
        audio_q.put(AudioEvent(action="check", amount=0, timestamp=now, raw_text="チェック"))

        _run_thread(thread, stop)

        assert len(captured) == 1
        assert captured[0].source["camera"] is False


class TestMultipleActions:
    """複数アクション: カメライベントが使い捨てになることを確認。"""

    @pytest.mark.skip(reason="camera deprecated in spec v4.0")
    def test_camera_event_consumed_once(self, tmp_path: Path) -> None:
        """1つのカメライベントは1つのアクションにしか使われない。"""
        gs = _make_game()
        audio_q = make_audio_queue()
        camera_q = make_camera_queue()
        writer = JsonWriter(log_dir=tmp_path, session_id="s8")
        stop = threading.Event()
        captured: list[ActionRecord] = []

        thread = IntegrationThread(
            audio_queue=audio_q,
            game_state=gs,
            json_writer=writer,
            camera_queue=camera_q,
            on_action=captured.append,
            stop_event=stop,
        )

        now = time.time()
        camera_q.put(CameraEvent(seat=1, timestamp=now))
        # 2つのアクション、カメライベントは1つだけ
        audio_q.put(AudioEvent(action="bet", amount=400, timestamp=now, raw_text="ベット400"))
        audio_q.put(AudioEvent(action="call", amount=400, timestamp=now, raw_text="コール400"))

        _run_thread(thread, stop, join_timeout=3.0)

        assert len(captured) == 2
        # 最初のアクションのみカメラマッチ
        assert captured[0].source["camera"] is True
        assert captured[0].confidence == _CONF_AUDIO_CAMERA
        # 2番目はカメラなし
        assert captured[1].source["camera"] is False
        assert captured[1].confidence == _CONF_AUDIO_ONLY
