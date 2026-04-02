from __future__ import annotations

import queue
from typing import Union

from core.events import AudioEvent, CameraEvent, RFIDEvent

# 型エイリアス
EventItem = Union[CameraEvent, AudioEvent, RFIDEvent]
EventQueue = queue.Queue[EventItem]


def make_camera_queue() -> EventQueue:
    """カメライベント用 Queue を生成する。インスタンスは main.py で生成し、各 Thread に渡す。"""
    return queue.Queue()


def make_audio_queue() -> EventQueue:
    """音声イベント用 Queue を生成する。インスタンスは main.py で生成し、各 Thread に渡す。"""
    return queue.Queue()


def make_rfid_queue() -> EventQueue:
    """RFID イベント用 Queue を生成する。インスタンスは main.py で生成し、各 Thread に渡す。"""
    return queue.Queue()
