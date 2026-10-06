"""テストで共有する部品（テストのダイエット, docs/worklog/2026-10-06-test-diet.md）。

テストのファイルどうしで import しないよう、共有する部品はここに置く（監査 1 D6）。
"""
from __future__ import annotations

import threading
import time


class HandledAudio:
    """`IntegrationThread` が処理し終えた音声のイベントを数える。

    スレッドを動かすテストは、決まった時間だけ寝て待たずに、入れたイベントが処理されるのを待つ（監査 1 D4）。
    走りのループは `self._handle_audio_event(event)` を呼ぶので、インスタンスの属性で包む。
    """

    def __init__(self, thread) -> None:
        self.count = 0
        real = thread._handle_audio_event

        def handle(event) -> None:
            try:
                real(event)
            finally:
                self.count += 1

        thread._handle_audio_event = handle

    def wait(self, n: int, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while self.count < n:
            if time.monotonic() > deadline:
                raise AssertionError(f"音声のイベント {n} 件のうち {self.count} 件しか処理されませんでした")
            time.sleep(0.002)


def stop_thread(thread: threading.Thread, stop: threading.Event, timeout: float = 3.0) -> None:
    """止める合図を出して待ち、止まったことを確かめる（止まらないスレッドを黙って通さない）。"""
    stop.set()
    thread.join(timeout=timeout)
    assert not thread.is_alive(), f"{thread.name} が {timeout} 秒で止まりませんでした"
