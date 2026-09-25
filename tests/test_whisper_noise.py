"""tests/test_whisper_noise.py

雑音への幻聴（店舗の 5 回目の通しテスト, セッション c9e150f3）: Whisper がプロンプトの語を 448 トークンまで
繰り返し（「シート4 レイズ 2千、コール、チェック、フォールド、オールイン、ショーダウン、ウィナー、ハンド、…」）、
1 回に 15〜18 秒かかって、そのあとの発話と札の離脱の反映が 50 秒遅れた。CLI にも数百文字の文が出た。

- 声が無い音（VAD）は Whisper にかけない（記録にだけ残す。CLI には出さない）。
- 書き起こしのトークン数に、音の長さに応じた上限を付ける（幻聴のループを早く打ち切る）。
- 発話の長さでは言えない量の書き起こしは雑音にする（打ち切ったループもここで捨てる）。
- CLI では雑音の文の頭だけを出す（全文は transcripts.jsonl）。
"""
from __future__ import annotations

import json
import queue
import sys
import threading
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from audio.recognizer import Recognition, WhisperTranscriber, is_implausibly_long
from audio.recorder import AudioThread, Transcript
from output.transcript_log import TranscriptLog

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import audio_check  # noqa: E402

# セッション c9e150f3 の幻聴（3.648 秒の音）
ECHO = ("シート4 レイズ 2千、コール、チェック、フォールド、オールイン、ショーダウン、ウィナー、ハンド、"
        + "フォールド、オールイン、ショーダウン、ウィナー、ハンド、チェック、" * 6 + "フォールド、オ")
_SECOND = 16000 * 2   # PCM16 の 1 秒のバイト数


class TestImplausiblyLong:
    def test_the_store_echo_is_too_long_for_its_audio(self):
        assert is_implausibly_long(ECHO, 3.648)
        assert is_implausibly_long(ECHO[:60], 1.216)

    @pytest.mark.parametrize("text, seconds", [
        ("フォールド、フォールド、フォールド、コール", 1.0),
        ("シート3 レイズ 2500点、シート5 コール、シート7 フォールド", 4.5),
        ("コール", 0.2),
        ("あ、そうだ。ハンドが配らないんだろう。", 1.728),
    ])
    def test_real_speech_is_not(self, text, seconds):
        assert not is_implausibly_long(text, seconds)


class _Model:
    def __init__(self, text: str = "コール") -> None:
        self.text = text
        self.calls: list[dict] = []

    def transcribe(self, audio, **kwargs):
        self.calls.append({"samples": len(audio), **kwargs})
        return [SimpleNamespace(text=self.text, avg_logprob=-0.1, no_speech_prob=0.0)], None


def _transcriber(monkeypatch, speech: bool | Exception, text: str = "コール",
                 vad_threshold: float = 0.5) -> tuple[WhisperTranscriber, _Model, list]:
    vad_calls: list = []

    def get_speech_timestamps(audio, options):
        vad_calls.append(options)
        if isinstance(speech, Exception):
            raise speech
        return [{"start": 0, "end": len(audio)}] if speech else []

    vad = types.SimpleNamespace(VadOptions=lambda **kw: SimpleNamespace(**kw),
                                get_speech_timestamps=get_speech_timestamps)
    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(vad=vad))
    monkeypatch.setitem(sys.modules, "faster_whisper.vad", vad)
    t = WhisperTranscriber.__new__(WhisperTranscriber)
    model = _Model(text)
    t._model, t._language, t._beam_size, t._temperature = model, "ja", 5, 0.0
    t._vad_threshold = vad_threshold
    return t, model, vad_calls


class TestTranscriber:
    def test_a_sound_without_a_voice_is_not_transcribed(self, monkeypatch):
        t, model, vad_calls = _transcriber(monkeypatch, speech=False)
        assert t.recognize(b"\x10\x00" * 16000) == Recognition("", None, no_speech=True)
        assert model.calls == [] and vad_calls[0].threshold == 0.5

    def test_speech_is_transcribed(self, monkeypatch):
        t, model, _ = _transcriber(monkeypatch, speech=True)
        result = t.recognize(b"\x10\x00" * 16000)
        assert result.text == "コール" and not result.no_speech and len(model.calls) == 1

    def test_the_decode_is_bounded_by_the_length_of_the_audio(self, monkeypatch):
        t, model, _ = _transcriber(monkeypatch, speech=True)
        t.recognize(b"\x10\x00" * 16000)            # 1 秒
        t.recognize(b"\x10\x00" * 16000 * 5)        # 5 秒（切り出しの上限）
        assert [c["max_new_tokens"] for c in model.calls] == [60, 140]

    def test_vad_off(self, monkeypatch):
        t, model, vad_calls = _transcriber(monkeypatch, speech=False, vad_threshold=0.0)
        assert t.recognize(b"\x10\x00" * 16000).text == "コール"
        assert vad_calls == []

    def test_a_broken_vad_falls_back_to_transcribing(self, monkeypatch):
        t, model, vad_calls = _transcriber(monkeypatch, speech=RuntimeError("onnxruntime"))
        assert t.recognize(b"\x10\x00" * 16000).text == "コール"
        assert t.recognize(b"\x10\x00" * 16000).text == "コール"
        assert len(vad_calls) == 1                  # 壊れていたら以後は使わない

    def test_the_old_interface_still_works(self, monkeypatch):
        t, _, _ = _transcriber(monkeypatch, speech=False)
        assert t.transcribe_with_confidence(b"\x10\x00" * 16000) == ("", None)


class _Recognizer:
    ready = True

    def __init__(self, result: Recognition) -> None:
        self.result = result

    def recognize(self, audio_bytes: bytes) -> Recognition:
        return self.result


def _process(result: Recognition, seconds: float = 1.0) -> tuple[queue.Queue, list[Transcript]]:
    q: queue.Queue = queue.Queue()
    seen: list[Transcript] = []
    thread = AudioThread(audio_queue=q, stop_event=threading.Event(), transcriber=_Recognizer(result),
                         on_transcript=seen.append)
    thread._process_chunk(b"\x10\x00" * int(16000 * seconds), utterance_start_ts=5.0)   # noqa: SLF001
    return q, seen


class TestAudioThread:
    def test_a_sound_without_a_voice_is_recorded_but_not_read(self):
        q, seen = _process(Recognition("", None, no_speech=True))
        (t,) = seen
        assert q.empty() and t.no_speech and t.noise and t.text == "" and t.events == ()

    def test_the_store_echo_is_noise(self):
        q, seen = _process(Recognition(ECHO, 0.34), seconds=3.648)
        assert q.empty() and seen[0].noise and seen[0].events == ()

    def test_a_cut_off_loop_without_the_word_run_is_noise(self):
        # 上限で打ち切ったループが語の並びを含まなくても、音の長さに対して長すぎれば雑音
        looped = "シート4 レイズ 2千、コール、チェック、" + "ハンド、ウィナー、" * 4
        q, seen = _process(Recognition(looped, 0.3), seconds=1.0)
        assert q.empty() and seen[0].noise

    def test_speech_is_read(self):
        q, seen = _process(Recognition("シート3 レイズ 2千", 0.8), seconds=1.5)
        assert q.get_nowait().action == "raise" and not seen[0].noise


class TestShown:
    def test_the_cli_shows_only_the_head_of_a_noise(self, capsys):
        from main import _print_transcript

        _print_transcript(Transcript(text=ECHO, confidence=0.34, events=(), audio_sec=3.6, infer_sec=6.0,
                                     utterance_start_ts=1.0, heard_at=8.0, noise=True))
        out = capsys.readouterr().out
        assert "雑音（聞き違い）として無視" in out and ECHO[:20] + "…" in out and ECHO[:21] not in out

    def test_the_cli_does_not_show_a_sound_without_a_voice(self, capsys):
        from main import _print_transcript

        _print_transcript(Transcript(text="", confidence=None, events=(), audio_sec=2.0, infer_sec=0.01,
                                     utterance_start_ts=1.0, heard_at=3.0, noise=True, no_speech=True))
        assert capsys.readouterr().out == ""

    def test_the_log_keeps_it(self, tmp_path):
        log = TranscriptLog(tmp_path / "s.transcripts.jsonl")
        log.write(Transcript(text="", confidence=None, events=(), audio_sec=2.0, infer_sec=0.01,
                             utterance_start_ts=1.0, heard_at=3.0, noise=True, no_speech=True))
        (line,) = [json.loads(x) for x in log.path.read_text(encoding="utf-8").splitlines()]
        assert line["no_speech"] is True and line["text"] == ""

    def test_audio_check_listen_shows_it(self):
        t = Transcript(text="", confidence=None, events=(), audio_sec=2.0, infer_sec=0.01,
                       utterance_start_ts=1.0, heard_at=3.0, noise=True, no_speech=True)
        assert "声ではない音 2.0 秒" in audio_check.format_transcript(t)
        stats = audio_check.ListenStats(no_speech=1)
        assert "声ではない音 1 件" in stats.summary()
