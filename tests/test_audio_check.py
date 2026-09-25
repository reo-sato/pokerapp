"""tests/test_audio_check.py

ADR-0060: 店舗 PC での音声テストの前に、マイクと音声認識を確かめる。

- `AudioThread` は文字になった発話を（アクションとして読めなくても）`on_transcript` に渡し、
  INFO で残す。マイクの名前・開けなかった理由を死活表示に載せる。
- 音声認識モデルを読み込めなくても起動は止めない（`WhisperTranscriber.ready` / `load_error`）。
- `tools/audio_check.py` の list / level / listen の表示。
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from audio.recognizer import WhisperTranscriber
from audio.recorder import AudioThread, Transcript, describe_event
from core.event_queue import make_audio_queue

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import audio_check  # noqa: E402


class _FakeTranscriber:
    ready = True

    def __init__(self, text: str, confidence: float | None = 0.8) -> None:
        self.text = text
        self.confidence = confidence

    def transcribe_with_confidence(self, audio_bytes: bytes):
        return self.text, self.confidence


def _thread(text: str, on_transcript=None, confidence: float | None = 0.8):
    q = make_audio_queue()
    return q, AudioThread(audio_queue=q, transcriber=_FakeTranscriber(text, confidence),
                          on_transcript=on_transcript)


_ONE_SECOND = b"\x00\x01" * 16000


class TestTranscriptHook:
    def test_an_action_is_reported_then_queued(self):
        seen: list[Transcript] = []
        q, thread = _thread("シート3 レイズ 600", lambda t: seen.append((t, q.qsize())))
        thread._process_chunk(_ONE_SECOND, utterance_start_ts=100.0)   # noqa: SLF001
        [(t, queued_when_called)] = seen
        assert queued_when_called == 0          # 表示が先（アクションの行より前に出る）
        assert t.text == "シート3 レイズ 600" and t.confidence == 0.8
        assert (t.event.action, t.event.amount, t.event.seat) == ("raise", 600, 3)
        assert t.audio_sec == pytest.approx(1.0) and t.utterance_start_ts == 100.0
        assert q.get_nowait().action == "raise"

    def test_speech_that_is_not_an_action_is_still_reported(self, caplog):
        seen: list[Transcript] = []
        q, thread = _thread("えーと、ちょっと待って", seen.append)
        with caplog.at_level("INFO", logger="audio.recorder"):
            thread._process_chunk(_ONE_SECOND)   # noqa: SLF001
        assert [t.event for t in seen] == [None]
        assert q.empty()
        assert any("えーと" in r.getMessage() and "読めず" in r.getMessage() for r in caplog.records)

    def test_silence_is_not_reported(self):
        seen: list[Transcript] = []
        _, thread = _thread("", seen.append)
        thread._process_chunk(_ONE_SECOND)   # noqa: SLF001
        assert seen == []

    def test_a_failing_callback_does_not_drop_the_action(self):
        def boom(t):
            raise RuntimeError("display failed")

        q, thread = _thread("コール", boom)
        thread._process_chunk(_ONE_SECOND)   # noqa: SLF001
        assert q.get_nowait().action == "call"


class TestModelLoading:
    def test_a_download_failure_does_not_crash(self, monkeypatch):
        def fail(*args, **kwargs):
            raise OSError("network unreachable")

        monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=fail))
        t = WhisperTranscriber("medium")
        assert not t.ready and "network unreachable" in t.load_error
        assert t.transcribe_with_confidence(b"\x00\x00") == ("", None)

    def test_thread_reports_whether_the_model_is_ready(self):
        _, thread = _thread("x")
        assert thread.asr_ready
        thread._transcriber = SimpleNamespace(ready=False)   # noqa: SLF001
        assert not thread.asr_ready


def test_describe_event():
    from audio.recognizer import parse_action

    assert describe_event(None) == "アクションとして読めず"
    assert describe_event(parse_action("シート2 ベット 500")) == "bet 500 席2"
    assert describe_event(parse_action("BTN コール")) == "call BTN"


# ――― tools/audio_check.py ―――

_DEVICES = [
    {"name": "Microsoft サウンド マッパー - Input", "maxInputChannels": 2, "hostApi": 0},
    {"name": "マイク (USB Audio Device)", "maxInputChannels": 1, "hostApi": 0},
    {"name": "スピーカー (Realtek)", "maxInputChannels": 0, "hostApi": 0},
    {"name": "マイク (USB Audio Device)", "maxInputChannels": 1, "hostApi": 1},
]


class _FakePA:
    def get_default_input_device_info(self):
        return {"index": 1}

    def get_device_count(self):
        return len(_DEVICES)

    def get_device_info_by_index(self, i):
        return _DEVICES[i]

    def get_host_api_info_by_index(self, i):
        return {"name": ["MME", "Windows WASAPI"][i]}

    def is_format_supported(self, rate, input_device, input_channels, input_format):
        if _DEVICES[input_device]["hostApi"] == 1:
            raise ValueError("Invalid sample rate")   # WASAPI は 16 kHz で開けない
        return True

    def terminate(self):
        pass


_FAKE_PYAUDIO = SimpleNamespace(PyAudio=_FakePA, paInt16=8)


class TestList:
    def test_only_input_devices_with_their_16k_support(self):
        devices = audio_check.list_input_devices(_FakePA(), _FAKE_PYAUDIO, 16000)
        assert [(d.index, d.api, d.rate_ok, d.default) for d in devices] == [
            (0, "MME", True, False), (1, "MME", True, True), (3, "Windows WASAPI", False, False),
        ]

    def _run(self, tmp_path, monkeypatch, capsys, device_id: int) -> tuple[int, str]:
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"audio": {"device_id": device_id}}), encoding="utf-8")
        monkeypatch.setitem(sys.modules, "pyaudio", _FAKE_PYAUDIO)
        code = audio_check.main(["--config", str(cfg), "list"])
        return code, capsys.readouterr().out

    def test_marks_the_configured_device(self, tmp_path, monkeypatch, capsys):
        code, out = self._run(tmp_path, monkeypatch, capsys, 1)
        assert code == 0
        [line] = [l for l in out.splitlines() if "← config" in l]
        assert line.split()[0] == "1" and "既定" in line
        assert "audio.device_id = 1（マイク (USB Audio Device)）を使います" in out

    def test_a_device_that_cannot_open_16k_is_flagged(self, tmp_path, monkeypatch, capsys):
        code, out = self._run(tmp_path, monkeypatch, capsys, 3)
        assert code == 1 and "16 kHz で開けません" in out

    def test_no_devices_points_at_rdp(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(_FakePA, "get_device_count", lambda self: 0)
        code, out = self._run(tmp_path, monkeypatch, capsys, 0)
        assert code == 1 and "RDP" in out


class TestLevel:
    def _chunks(self, rms_values: list[int]):
        frames = [((v).to_bytes(2, "little", signed=True)) * 1024 for v in rms_values]
        return iter(frames)

    def test_counts_speech_above_the_threshold(self):
        chunks = self._chunks([0] * 8 + [3000] * 8)
        lines: list[str] = []
        stats = audio_check.measure_level(lambda: next(chunks, b""), 16000, 1024, 1.0, 300.0,
                                          lines.append, line_every_sec=0.25)
        assert stats.chunks == 15 and stats.voiced == 7 and stats.max_rms == 3000
        assert "発話" not in lines[0] and "発話" in lines[-1]

    def test_silence_hints_at_privacy_and_rdp(self):
        summary = audio_check.level_summary(audio_check.LevelStats(chunks=10), 300.0)
        assert "プライバシー" in summary[-1] and "RDP" in summary[-1]

    def test_quiet_voice_hints_at_mic_position(self):
        stats = audio_check.LevelStats(chunks=10, voiced=0, max_rms=120)
        assert "口元" in audio_check.level_summary(stats, 300.0)[-1]


class TestListen:
    def test_transcript_line(self, monkeypatch):
        from audio.recognizer import parse_action

        t = Transcript(text="シート3 レイズ 600", confidence=0.82,
                       event=parse_action("シート3 レイズ 600"), audio_sec=1.6, infer_sec=0.9,
                       utterance_start_ts=1000.0, heard_at=1003.0)
        line = audio_check.format_transcript(t)
        assert "「シート3 レイズ 600」→ raise 600 席3" in line
        assert "信頼度 0.82" in line and "認識 0.9 秒" in line and "話し始めから 3.0 秒" in line

    def test_summary(self):
        stats = audio_check.ListenStats(heard=3, actions=2, infer_secs=[0.5, 1.0, 1.5])
        assert stats.summary() == "発話 3 件（アクション 2 件 / 読めず 1 件）・認識 平均 1.0 秒 / 最大 1.5 秒"
        assert "ありませんでした" in audio_check.ListenStats().summary()
