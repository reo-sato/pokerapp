"""マイクのミュート（店舗 2026-10-29）: ヘッドセットのミュートのボタンが押されたまま、4 回・計 2 時間近く何も録れていな
かった。入力が 0 のまま 5 秒続いたら知らせ（CLI の「●」行）、ミュートのあいだは 30 秒ごとに繰り返し、解けたら知らせる。
記録には `mic_state` の印（1 = ミュート / 0 = 解けた）を残し、ゲームの状態は変えない。"""
from __future__ import annotations

import queue
import struct
import threading

import audio.recorder as recorder
from audio.recorder import AudioThread
from core.events import AudioEvent

CHUNK = 1600                                          # 0.1 秒
ZERO = b"\x00\x00" * CHUNK
QUIET = struct.pack(f"{CHUNK}h", *([18, -18] * (CHUNK // 2)))   # 口元のヘッドセットの静かなとき（RMS 18）


class _Fake:
    ready = True

    def transcribe_with_confidence(self, audio_bytes: bytes):
        return "", 0.0


def _run(monkeypatch, chunks: list[bytes]):
    clock = {"t": 1000.0}

    def now() -> float:
        return clock["t"]

    monkeypatch.setattr(recorder.time, "time", now)
    out: queue.Queue = queue.Queue()
    notes: list[str] = []
    thread = AudioThread(audio_queue=out, stop_event=threading.Event(), transcriber=_Fake(), on_status=notes.append)
    items = list(chunks) + [b""]

    def read() -> bytes:
        clock["t"] += 0.1
        return items.pop(0)

    thread._capture_loop(read, CHUNK)   # noqa: SLF001
    events = []
    while not out.empty():
        events.append(out.get_nowait())
    return notes, [e for e in events if isinstance(e, AudioEvent) and e.action == "mic_state"], thread


def test_five_seconds_of_zeros_is_a_mute(monkeypatch):
    notes, marks, thread = _run(monkeypatch, [QUIET] * 20 + [ZERO] * 60 + [QUIET] * 10)
    assert notes == ["マイクがミュートになっています — ヘッドセットのミュートを外してください",
                     "マイクのミュートが解けました"]
    assert [(m.amount, round(m.timestamp, 1)) for m in marks] == [(1, 1002.1), (0, 1008.1)]
    assert thread.health["state"] == "running"


def test_a_short_gap_is_not_a_mute(monkeypatch):
    notes, marks, _ = _run(monkeypatch, [QUIET] * 10 + [ZERO] * 40 + [QUIET] * 10)
    assert notes == [] and marks == []


def test_the_notice_repeats_while_muted(monkeypatch):
    notes, marks, thread = _run(monkeypatch, [ZERO] * 700)          # 70 秒
    assert notes.count("マイクがミュートになっています — ヘッドセットのミュートを外してください") == 3   # 5・35・65 秒
    assert [m.amount for m in marks] == [1]
    assert thread.health["state"] == "muted"


def test_the_engine_keeps_the_mark_without_changing_the_game(tmp_path):
    import pytest

    pytest.importorskip("pokerkit")
    from tests.test_rfid_folds import _Table

    tb = _Table(tmp_path)
    tb.deal()
    before = (tb.played(), tb.gs.legal_context().actor_seat, len(tb.actions))
    tb.t._handle_audio_event(AudioEvent(action="mic_state", amount=1, timestamp=tb.now, raw_text=""))   # noqa: SLF001
    assert tb.t._mic_muted                                                                             # noqa: SLF001
    assert (tb.played(), tb.gs.legal_context().actor_seat, len(tb.actions)) == before
    tb.t._handle_audio_event(AudioEvent(action="mic_state", amount=0, timestamp=tb.now, raw_text=""))   # noqa: SLF001
    assert not tb.t._mic_muted                                                                         # noqa: SLF001
