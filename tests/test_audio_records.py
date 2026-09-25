"""tests/test_audio_records.py

記録の完全化（設計監査 2026-09-25）: 聞き取った発話をすべて JSONL に残す / 発話の音声を WAV で保存する /
発話の前 300 ms も認識に回す / 認識待ちの一番早い話し始めを engine に渡す。
"""
from __future__ import annotations

import json
import queue
import struct
import threading
import wave

from audio.recorder import AudioThread, Transcript
from audio.recognizer import parse_actions
from output.transcript_log import TranscriptLog


class _Fake:
    ready = True

    def __init__(self, text: str = "600点") -> None:
        self.text = text
        self.heard: list[bytes] = []

    def transcribe_with_confidence(self, audio_bytes: bytes):
        self.heard.append(audio_bytes)
        return self.text, 0.6


def test_every_utterance_is_logged(tmp_path):
    log = TranscriptLog(tmp_path / "s.transcripts.jsonl")
    log.write(Transcript(text="ロープヘッグ", confidence=0.2, events=(), audio_sec=0.8, infer_sec=2.5,
                         utterance_start_ts=10.0, heard_at=13.0, audio_file="10000.wav"))
    log.write(Transcript(text="2500", confidence=0.6, events=tuple(parse_actions("2500")), audio_sec=0.7,
                         infer_sec=2.4, utterance_start_ts=20.0, heard_at=23.0))
    lines = [json.loads(line) for line in log.path.read_text(encoding="utf-8").splitlines()]
    assert lines[0]["text"] == "ロープヘッグ" and lines[0]["events"] == [] and lines[0]["audio_file"] == "10000.wav"
    assert lines[1]["events"][0]["amount"] == 2500 and lines[1]["events"][0]["parse_flags"] == ["amount_only"]


def test_the_utterance_audio_is_saved(tmp_path):
    seen: list[Transcript] = []
    thread = AudioThread(audio_queue=queue.Queue(), stop_event=threading.Event(), transcriber=_Fake(),
                         on_transcript=seen.append, audio_dir=tmp_path / "audio")
    thread._process_chunk(b"\x10\x00" * 1600, utterance_start_ts=12.5)   # noqa: SLF001
    (transcript,) = seen
    assert transcript.audio_file == "12500.wav"
    with wave.open(str(tmp_path / "audio" / "12500.wav")) as w:
        assert (w.getframerate(), w.getnframes()) == (16000, 1600)


def test_the_start_of_a_word_is_kept():
    fake = _Fake()
    thread = AudioThread(audio_queue=queue.Queue(), stop_event=threading.Event(), transcriber=fake)
    quiet_but_not_silent = struct.pack("1024h", *([100] * 1024))   # 語頭の小さい子音（ゲート未満）
    loud = struct.pack("1024h", *([5000] * 1024))
    chunks = [quiet_but_not_silent] * 6 + [loud] * 4 + [b"\x00" * 2048] * 9 + [b""]
    thread._capture_loop(lambda: chunks.pop(0), 1024)   # noqa: SLF001
    item = thread._chunk_queue.get_nowait()              # noqa: SLF001
    assert item[0].startswith(quiet_but_not_silent * 5)  # 前の 5 チャンク ≒ 0.32 秒を含む


def test_the_oldest_pending_start():
    thread = AudioThread(audio_queue=queue.Queue(), stop_event=threading.Event(), transcriber=_Fake())
    assert thread.oldest_pending_start() is None
    thread._enqueue_utterance(b"\x00\x00", 30.0)   # noqa: SLF001
    thread._enqueue_utterance(b"\x00\x00", 25.0)   # noqa: SLF001
    thread._capture_start = 40.0                    # noqa: SLF001
    assert thread.oldest_pending_start() == 25.0
