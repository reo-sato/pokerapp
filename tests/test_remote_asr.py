"""書き起こしの API（ADR-0072, 2026-11-09）: OpenAI の whisper-1 で 1 発話ずつ書き起こす。

- 店舗 PC の Whisper と同じ約束（`recognize` → 文・自信・声なし）。声でなければ API を呼ばない。
- 失敗（ネット・API）は `RemoteAsrError`。録音側は音声を保存したまま後回しの列へ回し、つながったら送り直す。
  つながらない間はその場で呼ばない（1 発話ごとにタイムアウトを待たない）。
- 書き起こしの行に方式（`asr`）を残す。
"""
from __future__ import annotations

import json
import queue
import threading
import wave
from io import BytesIO
from pathlib import Path

import pytest

from audio.recorder import AudioThread
from core.remote_asr import OPENAI_URL, OpenAITranscriber, RemoteAsrError, transcribe_openai, wav_bytes

SID = "2026-11-09_200000_abcd"
PCM = b"\x10\x00" * 16000          # 1 秒


class _Post:
    """偽の POST（要求を覚えて、決めた答えを返すか、失敗する）。"""

    def __init__(self, text: str = "コール", fail: bool = False) -> None:
        self.text = text
        self.fail = fail
        self.calls: list[tuple] = []

    def __call__(self, url, body, headers, timeout=None):
        self.calls.append((url, body, headers, timeout))
        if self.fail:
            raise RemoteAsrError("書き起こしの API につながりません: timed out")
        return {"text": self.text}


def _transcriber(post: _Post, **kw) -> OpenAITranscriber:
    return OpenAITranscriber(key="sk-test", post_fn=post, vad_threshold=0, **kw)


class TestRequest:
    def test_the_request_is_a_wav_with_the_live_prompt(self):
        post = _Post("レイズ 3200")
        assert transcribe_openai(wav_bytes(PCM), key="sk-test", post_fn=post) == "レイズ 3200"
        url, body, headers, timeout = post.calls[0]
        assert url == OPENAI_URL and headers["Authorization"] == "Bearer sk-test" and timeout == 15.0
        assert b'name="model"\r\n\r\nwhisper-1' in body and b'name="language"\r\n\r\nja' in body
        assert "ポーカーのディーラー".encode() in body                      # ライブの Whisper と同じプロンプト
        start = body.index(b"RIFF")
        with wave.open(BytesIO(body[start:]), "rb") as f:
            assert (f.getnchannels(), f.getsampwidth(), f.getframerate()) == (1, 2, 16000)

    def test_recognize_returns_text_without_confidence(self):
        result = _transcriber(_Post("チェック")).recognize(PCM)
        assert (result.text, result.confidence, result.no_speech) == ("チェック", None, False)

    def test_no_key_means_not_ready(self, monkeypatch):
        monkeypatch.delenv("POKERAPP_OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        t = OpenAITranscriber(post_fn=_Post())
        assert not t.ready and "POKERAPP_OPENAI_API_KEY" in t.load_error
        with pytest.raises(RemoteAsrError):
            t.recognize(PCM)

    def test_silence_is_not_sent(self, monkeypatch):
        post = _Post()
        t = OpenAITranscriber(key="sk-test", post_fn=post, vad_threshold=0.5)
        monkeypatch.setattr(t, "has_speech", lambda _a: False)
        assert t.recognize(PCM).no_speech and post.calls == []

    def test_a_failure_raises(self):
        with pytest.raises(RemoteAsrError):
            _transcriber(_Post(fail=True)).recognize(PCM)


class _Ear:
    def __init__(self, text: str = "こんにちは") -> None:
        self.text = text

    def hear(self, samples):
        text = self.text

        class _R:
            def to_dict(self_inner):
                return {"text": text, "logp": -1.0, "candidates": []}

        return _R()


def _thread(tmp_path: Path, post: _Post, ear: str = "こんにちは"):
    q: queue.Queue = queue.Queue()
    seen: list = []
    notices: list[str] = []
    thread = AudioThread(audio_queue=q, stop_event=threading.Event(), transcriber=_transcriber(post),
                         on_transcript=seen.append, audio_dir=tmp_path / "audio" / SID, second_ear=_Ear(ear),
                         live_transcribe=False, on_status=notices.append)
    return thread, q, seen, notices


class TestLogger:
    def test_the_voice_goes_to_the_logger_at_once(self, tmp_path):
        """API のときは後回しの設定でも、その場で書き起こしてロガーに送る（プレー中に声を戻す）。"""
        thread, q, seen, _ = _thread(tmp_path, _Post("レイズ 3200"))
        assert thread.live_transcribe or thread._remote                  # noqa: SLF001
        thread._live_transcribe = True                                   # noqa: SLF001 — main.py が true にする
        thread._process_chunk(PCM, utterance_start_ts=100.0)            # noqa: SLF001
        event = q.get_nowait()
        assert (event.action, event.amount, event.utterance_start_ts) == ("raise", 3200, 100.0)
        (row,) = seen
        assert row.asr == "openai-whisper-1" and not row.deferred and row.ear is not None
        assert (tmp_path / "audio" / SID / "100000.wav").is_file()

    def test_a_failed_utterance_is_kept_and_sent_later(self, tmp_path):
        post = _Post("コール", fail=True)
        thread, q, seen, notices = _thread(tmp_path, post, ear="ブラインド上がります SB800 BB1600")
        thread._live_transcribe = True                                   # noqa: SLF001
        thread._process_chunk(PCM, utterance_start_ts=100.0)            # noqa: SLF001
        # ブラインドの宣言は第 2 の耳でその場で送る。書き起こしは後回しの列へ
        assert q.get_nowait().action == "blind_up" and seen == []
        assert thread._deferred_queue.qsize() == 1                      # noqa: SLF001
        assert notices == ["書き起こしの API につながりません（音声は保存して、つながったら送り直します）"]
        # つながらない間は、次の発話で API を呼ばない（タイムアウトを待たない）
        calls = len(post.calls)
        thread._process_chunk(PCM, utterance_start_ts=105.0)            # noqa: SLF001
        assert len(post.calls) == calls and thread._deferred_queue.qsize() == 2   # noqa: SLF001
        assert q.get_nowait().action == "blind_up"                       # その間もブラインドの宣言は第 2 の耳で
        # つながったら送り直す: 行は deferred（ロガーには送らない）
        post.fail = False
        thread._remote_down_until = 0.0                                  # noqa: SLF001
        item = thread._deferred_queue.get_nowait()                      # noqa: SLF001
        thread._process_deferred(*item)                                  # noqa: SLF001
        (row,) = seen
        assert row.deferred and row.text == "コール" and row.audio_file == "100000.wav" and row.asr == "openai-whisper-1"
        assert q.empty()
        assert notices[-1] == "書き起こしの API にまたつながりました"

    def test_a_retry_that_fails_goes_back_to_the_queue(self, tmp_path):
        post = _Post(fail=True)
        thread, _q, seen, _ = _thread(tmp_path, post)
        thread._deferred_queue.put((PCM, 100.0, "100000.wav"))          # noqa: SLF001
        thread._process_deferred(*thread._deferred_queue.get_nowait())  # noqa: SLF001
        assert seen == [] and thread._deferred_queue.qsize() == 1       # noqa: SLF001
        assert thread._remote_backoff == 10.0                            # noqa: SLF001 — 5 → 10 秒

    def test_the_row_records_the_method(self, tmp_path):
        from output.transcript_log import TranscriptLog

        thread, _q, seen, _ = _thread(tmp_path, _Post("チェック"))
        thread._live_transcribe = True                                   # noqa: SLF001
        thread._process_chunk(PCM, utterance_start_ts=100.0)            # noqa: SLF001
        log = TranscriptLog(tmp_path / f"{SID}.transcripts.jsonl")
        log.write(seen[0])
        row = json.loads(log.path.read_text(encoding="utf-8"))
        assert row["asr"] == "openai-whisper-1" and row["confidence"] is None


class TestMain:
    def test_openai_needs_a_key_or_falls_back(self, monkeypatch):
        import main

        monkeypatch.delenv("POKERAPP_OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        assert not main._wants_remote({"transcriber": "openai"})        # noqa: SLF001
        assert main._remote_transcriber({"transcriber": "openai"}) is None   # noqa: SLF001
        monkeypatch.setenv("POKERAPP_OPENAI_API_KEY", "sk-test")
        assert main._wants_remote({"transcriber": "openai"})            # noqa: SLF001
        assert not main._wants_remote({"transcriber": "local"})         # noqa: SLF001
        t = main._remote_transcriber({"transcriber": "openai", "vad_threshold": 0.8})   # noqa: SLF001
        assert t.ready and t.asr_name == "openai-whisper-1"


class TestAfterSession:
    def test_the_rest_stops_when_the_api_is_down(self, tmp_path):
        """セッション後の書き起こしは、API につながらなければ 1 件で止める（残りはあとで続きから）。"""
        from tools.offplay_audio import mark_deferred, transcribe_deferred
        from tools.rescore_audio import find_sessions

        transcripts = tmp_path / f"{SID}.transcripts.jsonl"
        mark_deferred(tmp_path / "audio" / SID, transcripts)
        for ms in (1791000040000, 1791000045000, 1791000050000):
            path = tmp_path / "audio" / SID / f"{ms}.wav"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(wav_bytes(PCM))
        calls = []

        def process(audio, start, name):
            calls.append(name)
            raise RemoteAsrError("timed out")

        session = find_sessions(tmp_path)[0]
        assert transcribe_deferred(session, process, log=lambda _m: None) == (0, 1)
        assert len(calls) == 1
