"""tests/test_integration.py

IntegrationThread の confidence スコアのテスト（audio 単独）。

カメラ入力は sprc_v4.docx で廃止済み。現構成は RFID + 音声の 2 ソース。
RFID との ±MATCH_WINDOW 照合・confidence は tests/test_phase7.py の
TestRFIDSeatMatching を参照。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from core.event_queue import make_audio_queue
from core.events import AudioEvent
from core.game_state import GameStateManager, PlayerState
from core.hand_log import ActionRecord
from integration.engine import IntegrationThread, _CONF_AUDIO_ONLY
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
    """RFID が無い（または同席 RFID イベントなし）場合、confidence = _CONF_AUDIO_ONLY。"""

    def test_audio_only_no_rfid_queue(self, tmp_path: Path) -> None:
        gs = _make_game()
        audio_q = make_audio_queue()
        writer = JsonWriter(log_dir=tmp_path, session_id="s1")
        stop = threading.Event()
        captured: list[ActionRecord] = []

        thread = IntegrationThread(
            audio_queue=audio_q,
            game_state=gs,
            json_writer=writer,
            on_action=captured.append,
            stop_event=stop,
        )

        audio_q.put(AudioEvent(action="bet", amount=500, timestamp=time.time(), raw_text="ベット500"))
        _run_thread(thread, stop)

        assert len(captured) == 1
        rec = captured[0]
        assert rec.source == {"audio": True, "rfid": False}
        assert rec.confidence == _CONF_AUDIO_ONLY

    def test_multiple_audio_actions_all_audio_only(self, tmp_path: Path) -> None:
        """RFID なしの複数アクションはすべて audio 単独 confidence。"""
        gs = _make_game()
        audio_q = make_audio_queue()
        writer = JsonWriter(log_dir=tmp_path, session_id="s2")
        stop = threading.Event()
        captured: list[ActionRecord] = []

        thread = IntegrationThread(
            audio_queue=audio_q,
            game_state=gs,
            json_writer=writer,
            on_action=captured.append,
            stop_event=stop,
        )

        now = time.time()
        audio_q.put(AudioEvent(action="bet",  amount=400, timestamp=now, raw_text="ベット400"))
        audio_q.put(AudioEvent(action="call", amount=400, timestamp=now, raw_text="コール400"))
        _run_thread(thread, stop, join_timeout=3.0)

        assert len(captured) == 2
        for rec in captured:
            assert rec.source == {"audio": True, "rfid": False}
            assert rec.confidence == _CONF_AUDIO_ONLY
