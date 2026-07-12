"""tests/test_thread_health.py

録音系の死活表示（dashboard 用 health ステータス）のユニットテスト。

- AudioThread.health: 初期値 / pyaudio 不在時の unavailable（import を強制失敗させて決定的に）
- RFIDThread.health: 接続成功で running + connected 数 / 全滅で no_readers / 停止で stopped

実ハードは使わない（RFID は既存テストと同じ fake bridge 注入）。
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

from audio.recorder import AudioThread
from core.event_queue import make_audio_queue, make_rfid_queue
from rfid.card_master import CardMaster
from rfid.reader_thread import RFIDThread


class _FakeBridge:
    def __init__(self, reader_name: str, ok: bool) -> None:
        self._ok = ok

    def connect(self) -> bool:
        return self._ok

    def read_uid(self) -> "str | None":
        return None

    def close(self) -> None:
        pass


def test_audio_thread_health_initial_and_unavailable(monkeypatch):
    # `import pyaudio` を決定的に失敗させる（None を仕込むと ImportError になる）。
    monkeypatch.setitem(sys.modules, "pyaudio", None)
    thread = AudioThread(make_audio_queue(), model_size="tiny")
    assert thread.health["state"] == "starting"
    thread.run()  # start せず直接実行（pyaudio 不在で即 return する経路）
    assert thread.health == {"state": "unavailable", "level": 0.0, "last_chunk_at": None}


def _run_rfid(tmp_path: Path, configs: list[dict], ok_by_name: dict[str, bool]) -> RFIDThread:
    stop = threading.Event()
    thread = RFIDThread(
        rfid_queue=make_rfid_queue(),
        card_master=CardMaster(tmp_path / "cards.json"),
        reader_configs=configs,
        poll_interval_ms=10,
        stop_event=stop,
        bridge_factory=lambda name: _FakeBridge(name, ok_by_name.get(name, False)),
    )
    thread.start()
    time.sleep(0.1)
    state_while_running = dict(thread.health)
    stop.set()
    thread.join(timeout=2)
    thread.health_while_running = state_while_running  # type: ignore[attr-defined]
    return thread


def test_rfid_thread_health_running_counts_connected(tmp_path: Path):
    configs = [
        {"name": "A", "role": "seat", "seat": 1},
        {"name": "B", "role": "board"},
        {"name": "C", "role": "seat", "seat": 2},
    ]
    thread = _run_rfid(tmp_path, configs, {"A": True, "B": True, "C": False})
    running = thread.health_while_running  # type: ignore[attr-defined]
    assert running["state"] == "running"
    assert running["connected"] == 2
    assert running["configured"] == 3
    # 停止後は stopped（connected 等は保持）。
    assert thread.health["state"] == "stopped"
    assert thread.health["connected"] == 2


def test_rfid_thread_health_no_readers(tmp_path: Path):
    configs = [{"name": "A", "role": "seat", "seat": 1}]
    thread = RFIDThread(
        rfid_queue=make_rfid_queue(),
        card_master=CardMaster(tmp_path / "cards.json"),
        reader_configs=configs,
        stop_event=threading.Event(),
        bridge_factory=lambda name: _FakeBridge(name, ok=False),
    )
    assert thread.health["state"] == "starting"
    thread.run()  # 全 reader 接続失敗 → 即 return
    assert thread.health["state"] == "no_readers"
    assert thread.health["connected"] == 0
    assert thread.health["configured"] == 1
