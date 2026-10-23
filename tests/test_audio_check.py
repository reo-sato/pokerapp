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
        [event] = t.events
        assert (event.action, event.amount, event.seat) == ("raise", 600, 3)
        assert t.audio_sec == pytest.approx(1.0) and t.utterance_start_ts == 100.0
        assert q.get_nowait().action == "raise"

    def test_speech_that_is_not_an_action_is_still_reported(self, caplog):
        seen: list[Transcript] = []
        q, thread = _thread("えーと、ちょっと待って", seen.append)
        with caplog.at_level("INFO", logger="audio.recorder"):
            thread._process_chunk(_ONE_SECOND)   # noqa: SLF001
        assert [t.events for t in seen] == [()]
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

    def _run_names(self, tmp_path, monkeypatch, capsys, name: str, command: str = "list") -> tuple[int, str]:
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"audio": {"device_id": 0, "device_name": name}}), encoding="utf-8")
        monkeypatch.setitem(sys.modules, "pyaudio", _FAKE_PYAUDIO)
        code = audio_check.main(["--config", str(cfg), command])
        return code, capsys.readouterr().out

    def test_the_named_mic_is_marked_instead_of_the_number(self, tmp_path, monkeypatch, capsys):
        """名前（audio.device_name）があれば番号ではなく名前で選んだマイクに印（Bluetooth で番号がずれても同じ）。"""
        code, out = self._run_names(tmp_path, monkeypatch, capsys, "Yeti|USB Audio")
        assert code == 0
        [line] = [l for l in out.splitlines() if "← config" in l]
        assert line.split()[0] == "1" and "audio.device_name" in line     # WASAPI の 3 番（16kHz 不可）ではなく MME
        assert "「Yeti」 / 「USB Audio」 → 番号 1（マイク (USB Audio Device)）を使います" in out

    def test_a_missing_name_is_reported(self, tmp_path, monkeypatch, capsys):
        code, out = self._run_names(tmp_path, monkeypatch, capsys, "Wireless Mic Rx")
        assert code == 1 and "← config" not in out
        assert "「Wireless Mic Rx」 のマイクが見つかりません" in out and "Bluetooth" in out

    def test_level_does_not_open_another_mic(self, tmp_path, monkeypatch, capsys):
        code, out = self._run_names(tmp_path, monkeypatch, capsys, "Wireless Mic Rx", "level")
        assert code == 1 and "見つかりません" in out          # _FakePA に open は無い = 開こうとしていない


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
                       events=(parse_action("シート3 レイズ 600"),), audio_sec=1.6, infer_sec=0.9,
                       utterance_start_ts=1000.0, heard_at=1003.0)
        line = audio_check.format_transcript(t)
        assert "「シート3 レイズ 600」→ raise 600 席3" in line
        assert "信頼度 0.82" in line and "認識 0.9 秒" in line and "話し始めから 3.0 秒" in line

    def test_summary(self):
        stats = audio_check.ListenStats(heard=3, actions=4, unreadable=1, infer_secs=[0.5, 1.0, 1.5])
        assert stats.summary() == ("発話 3 件 → アクション 4 件（読めなかった発話 1 件）"
                                   "・認識 平均 1.0 秒 / 最大 1.5 秒")
        stats.dropped = 2
        assert stats.summary().endswith("・短すぎて認識しなかった音 2 件")
        assert "ありませんでした" in audio_check.ListenStats().summary()

    def test_dropped_line_names_the_setting(self):
        line = audio_check.format_dropped(0.12, 0.0)
        assert "短い音 0.12 秒" in line and "audio.min_speech_sec" in line


# ――― bench: Whisper のスレッド数ごとの聞き取りの時間 ―――

def _write_wav(path: Path, seconds: float = 0.5, rate: int = 16000, channels: int = 1, cycles: float = 100) -> None:
    import wave

    import numpy as np

    path.parent.mkdir(parents=True, exist_ok=True)
    samples = (np.sin(np.linspace(0, cycles, int(rate * seconds) * channels)) * 4000).astype(np.int16)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(samples.tobytes())


def _session(log_dir: Path, sid: str, n: int, *, mtime: float) -> None:
    import os

    rows = []
    for i in range(n):
        name = f"{1000 + i}.wav"
        rows.append({"utterance_start_ts": float(i), "text": f"コール{i}", "audio_file": name})
        _write_wav(log_dir / "audio" / sid / name, cycles=100 + i)          # 発話ごとに違う音
    rows.append({"utterance_start_ts": 90.0, "text": "", "audio_file": "9000.wav", "no_speech": True})
    rows.append({"utterance_start_ts": 91.0, "text": "チェック", "audio_file": "9100.wav"})   # 音声が無い
    path = log_dir / f"{sid}.transcripts.jsonl"
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    os.utime(path, (mtime, mtime))


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _TimedTranscriber:
    """スレッドが多いほど速い偽の Whisper（時計を進める）。"""

    ready = True

    def __init__(self, clock: _Clock, threads: int, beam: int) -> None:
        self.clock, self.threads, self.beam = clock, threads, beam
        clock.now += 5.0                                  # 読み込み

    def recognize(self, pcm: bytes):
        self.clock.now += 12.0 / self.threads + 0.1 * self.beam
        text = "コール" if self.beam >= 5 else "ゴール"      # ビームを下げると書き起こしが変わる
        return SimpleNamespace(text=text, confidence=0.9)


class _JoinTranscriber:
    """発話の音声 → 文の表を持つ偽の Whisper。まとめた音声は無音で区切って読み、「、」でつなぐ。"""

    ready = True
    gap = b"\x00\x00" * 16000

    def __init__(self, clock: _Clock, texts: dict[bytes, str], drop_last: bool = False) -> None:
        self.clock, self.texts, self.drop_last = clock, texts, drop_last

    def recognize(self, pcm: bytes):
        parts = pcm.split(self.gap)
        self.clock.now += 3.0 + 0.3 * (len(parts) - 1)        # まとめても 30 秒の窓は 1 回ぶん
        words = [self.texts.get(part, "?") for part in parts]
        if self.drop_last and len(words) > 1:
            words = words[:-1]
        return SimpleNamespace(text="、".join(words), confidence=0.9)


class TestBench:
    def test_utterances_come_from_the_newest_sessions_with_audio(self, tmp_path):
        _session(tmp_path, "old", 3, mtime=1000.0)
        _session(tmp_path, "new", 30, mtime=2000.0)
        label, utterances = audio_check.pick_utterances(tmp_path, None, 10)
        assert label == "new" and len(utterances) == 10
        assert utterances[0][0] == "1000.wav" and utterances[1][0] == "1003.wav"     # 全体に散らす
        assert all(path.is_file() for _, path, _ in utterances)
        label, utterances = audio_check.pick_utterances(tmp_path, "ol", 10)
        assert label == "old" and [u[0] for u in utterances] == ["1000.wav", "1001.wav", "1002.wav"]
        assert audio_check.pick_utterances(tmp_path / "none", None, 10) is None

    def test_older_sessions_fill_up_the_count(self, tmp_path):
        _session(tmp_path, "old", 4, mtime=1000.0)
        _session(tmp_path, "new", 3, mtime=2000.0)
        label, utterances = audio_check.pick_utterances(tmp_path, None, 5)
        assert label == "new ほか 1 セッション" and len(utterances) == 5
        assert [p.parent.name for _, p, _ in utterances] == ["new", "new", "new", "old", "old"]

    def test_wav_is_read_as_live_pcm(self, tmp_path):
        _write_wav(tmp_path / "a.wav", seconds=1.0)
        assert len(audio_check.read_pcm16(tmp_path / "a.wav")) == 32000
        _write_wav(tmp_path / "b.wav", seconds=1.0, rate=8000, channels=2)
        assert len(audio_check.read_pcm16(tmp_path / "b.wav")) == 32000       # 16 kHz モノラルに

    def test_faster_settings_with_the_same_text_are_recommended(self, tmp_path):
        _session(tmp_path, "s", 4, mtime=1.0)
        _, utterances = audio_check.pick_utterances(tmp_path, None, 4)
        clock = _Clock()
        lines: list[str] = []
        results = audio_check.run_bench(
            utterances, [(4, 5), (8, 5), (8, 2)],
            lambda threads, beam: _TimedTranscriber(clock, threads, beam), clock=clock, log=lines.append,
        )
        assert [(r.threads, r.beam, round(r.median, 2)) for r in results] == [(4, 5, 3.5), (8, 5, 2.0), (8, 2, 1.7)]
        # 2 周目は逆の順で測る（起動直後の遅さ・あとから重くなる影響を打ち消す）
        assert [line.split("：")[0].split(": ")[0] for line in lines] == [
            "  [1/6] スレッド 4・ビーム 5（1 周目）", "  [2/6] スレッド 8・ビーム 5（1 周目）",
            "  [3/6] スレッド 8・ビーム 2（1 周目）", "  [4/6] スレッド 8・ビーム 2（2 周目）",
            "  [5/6] スレッド 8・ビーム 5（2 周目）", "  [6/6] スレッド 4・ビーム 5（2 周目）",
        ]
        base = audio_check.base_result(results, 4, 5)
        summary = audio_check.summary_lines(results, base)
        assert "（基準）" in summary[0] and "1 周目 3.50・2 周目 3.50" in summary[0]
        assert "書き起こしは基準と 0/4 同じ" in summary[2]
        advice = audio_check.recommend(results, current_threads=4, current_beam=5)
        assert "スレッド 8・ビーム 5" in advice[1] and "57%" in advice[1]            # ビーム 2 は書き起こしが違う
        assert advice[2].endswith("set_config.py audio.cpu_threads 8") and len(advice) == 3

    def test_a_cold_start_does_not_make_the_first_setting_look_slow(self, tmp_path):
        """店舗 PC: 最初に測った設定だけが 0.6 秒ほど遅く出た（同じ設定が回によって 2.96 秒と 3.58 秒）。"""
        _session(tmp_path, "s", 4, mtime=1.0)
        _, utterances = audio_check.pick_utterances(tmp_path, None, 4)
        clock = _Clock()
        calls = {"n": 0}

        class Same:
            ready = True

            def recognize(self, pcm):
                calls["n"] += 1
                clock.now += 3.0 + (2.0 if calls["n"] <= 3 else 0.0)     # 最初の 3 回だけ遅い（起動直後）
                return SimpleNamespace(text="コール", confidence=0.9)

        results = audio_check.run_bench(utterances, [(4, 5), (8, 5)], lambda t, b: Same(), clock=clock,
                                        log=lambda _: None)
        assert [r.median for r in results] == [3.0, 3.0]
        assert audio_check.recommend(results, 4, 5)[-1].startswith("書き起こしを変えずにはっきり速くなる設定は")

    def test_a_setting_faster_in_only_one_round_is_not_recommended(self, tmp_path):
        _session(tmp_path, "s", 2, mtime=1.0)
        _, utterances = audio_check.pick_utterances(tmp_path, None, 2)
        base = audio_check.BenchResult(4, 5, rounds=[[3.0, 3.0], [3.0, 3.0]], texts=["a", "b"])
        noisy = audio_check.BenchResult(8, 5, rounds=[[1.0, 1.0], [3.5, 3.5]], texts=["a", "b"])
        assert "はっきり速くなる設定はありませんでした" in audio_check.recommend([base, noisy], 4, 5)[-1]

    def test_joined_utterances(self, tmp_path):
        _session(tmp_path, "s", 4, mtime=1.0)
        _, utterances = audio_check.pick_utterances(tmp_path, None, 4)
        texts = {audio_check.read_pcm16(path): text for _, path, text in utterances}
        clock = _Clock()
        base = audio_check.BenchResult(4, 5, rounds=[[3.0] * 4], texts=[t for _, _, t in utterances])
        joined = audio_check.run_joined(utterances, base, 2, lambda t, b: _JoinTranscriber(clock, texts),
                                        clock=clock)
        assert (joined["groups"], joined["same"]) == (2, 2)
        assert (round(joined["joined_per_utterance"], 2), joined["single_per_utterance"]) == (1.65, 3.0)
        lines = audio_check.joined_lines(joined)
        assert lines == ["まとめて 2 発話ずつ聞き取ると: 1 発話あたり 1.65 秒（1 つずつだと 3.00 秒）・"
                         "読んだアクションが 1 つずつと同じ 2/2 組"]
        dropped = audio_check.run_joined(
            utterances, base, 2, lambda t, b: _JoinTranscriber(clock, texts, drop_last=True), clock=clock)
        assert dropped["same"] == 0 and len(dropped["examples"]) == 2
        assert "違った組: 1 つずつ「コール0 ／ コール1」 → まとめて「コール0」" in audio_check.joined_lines(dropped)[1]
        assert audio_check.run_joined(utterances[:1], base, 2, lambda t, b: None) is None

    def test_command_line(self, tmp_path, capsys, monkeypatch):
        _session(tmp_path, "s", 4, mtime=1.0)
        made: list[tuple[int, int]] = []
        clock = _Clock()

        def factory(model, language, vad):
            def make(threads, beam):
                made.append((threads, beam))
                return _TimedTranscriber(clock, threads, beam)
            return make

        monkeypatch.setattr(audio_check, "_make_whisper", factory)
        config = tmp_path / "config.json"
        config.write_text(json.dumps({"audio": {"beam_size": 5, "cpu_threads": 8}}), encoding="utf-8")
        args = ["--config", str(config), "bench", "--log-dir", str(tmp_path), "--threads", "16", "--joined", "2"]
        assert audio_check.main(args) == 0
        assert made == [(8, 5), (16, 5), (16, 5), (8, 5), (8, 5)]     # いまの設定（8）も必ず測る + まとめる試し
        out = capsys.readouterr().out
        assert "計測: セッション s の発話 4 個" in out and "スレッド 16・ビーム 5: 1 発話 中央値" in out
        assert "まとめて 2 発話ずつ聞き取ると" in out

    def test_no_saved_audio(self, tmp_path, capsys):
        config = tmp_path / "config.json"
        config.write_text(json.dumps({"audio": {}}), encoding="utf-8")
        assert audio_check.main(["--config", str(config), "bench", "--log-dir", str(tmp_path)]) == 1
        assert "audio.save_audio" in capsys.readouterr().out


def test_whisper_threads_are_passed_to_faster_whisper(monkeypatch):
    seen: dict = {}

    def model(*args, **kwargs):
        seen.update(kwargs)
        return object()

    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=model))
    assert WhisperTranscriber("medium", cpu_threads=8).ready and seen["cpu_threads"] == 8
    WhisperTranscriber("medium")
    assert seen["cpu_threads"] == 0                    # 0 = faster-whisper の既定（4）のまま


# ――― gain: 保存した発話を、そのままと音量をそろえて聞き直す（店舗 2026-10-01 18:42: 声が小さく決まり文句に） ―――

def _tone_pcm(amplitude: float, seconds: float = 0.5, rate: int = 16000) -> bytes:
    import numpy as np

    t = np.arange(int(rate * seconds)) / rate
    return (np.sin(2 * np.pi * 200 * t) * amplitude).astype(np.int16).tobytes()


class TestGain:
    def test_voice_level_and_raising(self):
        quiet = _tone_pcm(1000)
        assert audio_check.voice_level(quiet) == pytest.approx(707, rel=0.02)    # 正弦波の RMS = 振幅 / √2
        raised, gain = audio_check.raise_level(quiet, 3000)
        assert gain == pytest.approx(3000 / 707, rel=0.02)
        assert audio_check.voice_level(raised) == pytest.approx(3000, rel=0.02)
        assert audio_check.raise_level(_tone_pcm(8000), 3000)[1] == 1.0            # 下げはしない
        assert audio_check.raise_level(_tone_pcm(200), 30000)[1] == pytest.approx(150, rel=0.01)   # 割れない範囲まで

    def test_heard_labels(self):
        assert audio_check._heard("ご視聴ありがとうございました。") == "決まり文句"     # noqa: SLF001
        assert audio_check._heard("えーと") == "アクションとして読めず"                 # noqa: SLF001
        assert audio_check._heard("フォールド") == "fold"                               # noqa: SLF001

    def test_command_compares_both(self, tmp_path, capsys):
        import wave

        log = tmp_path / "logs"
        sid = "abc123"
        (log / "audio" / sid).mkdir(parents=True)
        rows = []
        for i, amp in enumerate((800, 900, 6000)):
            name = f"{i}.wav"
            with wave.open(str(log / "audio" / sid / name), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
                w.writeframes(_tone_pcm(amp))
            rows.append({"utterance_start_ts": float(i), "text": "x", "audio_file": name})
        (log / f"{sid}.transcripts.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

        class Loudness:                      # 声が 2000 より小さいと決まり文句にする Whisper の代わり
            def transcribe_with_confidence(self, pcm):
                return ("フォールド" if audio_check.voice_level(pcm) >= 2000 else "ご視聴ありがとうございました"), 0.5

        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"audio": {}}), encoding="utf-8")
        args = audio_check.build_parser().parse_args(
            ["--config", str(cfg), "gain", "--log-dir", str(log), "--session", "abc"])
        assert audio_check._cmd_gain(args, make_transcriber=lambda threads, beam: Loudness()) == 0   # noqa: SLF001
        out = capsys.readouterr().out
        assert "そのまま: アクションとして読めた 1/3・決まり文句 2" in out
        assert "そろえて: アクションとして読めた 3/3・決まり文句 0" in out
