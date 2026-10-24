"""聞き流した発話の音声（`off_<ms>.wav`）の書き起こし（`tools/offplay_audio.py`, 2026-10-24）。"""
from __future__ import annotations

import json
import wave
from pathlib import Path

from audio.recorder import Transcript
from core.events import AudioEvent
from core.offplay import OFFPLAY_SUFFIX, merge_offplay, offplay_start
from tools.offplay_audio import transcribe_session
from tools.rescore_audio import find_sessions

SID = "2026-10-24_200000_abcd"


def _wav(path: Path, sec: float = 0.5) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(16000)
        f.writeframes(b"\x00\x00" * int(16000 * sec))


def _logs(tmp_path: Path) -> Path:
    (tmp_path / f"{SID}.transcripts.jsonl").write_text(
        json.dumps({"utterance_start_ts": 1.0, "text": "コール", "audio_file": "1000.wav"}) + "\n", encoding="utf-8")
    for ms in (1791000040000, 1791000045000):
        _wav(tmp_path / "audio" / SID / f"off_{ms}.wav")
    _wav(tmp_path / "audio" / SID / "1000.wav")           # 認識に回した発話（書き起こし済み）は対象外
    return tmp_path


def _fake(texts: dict[float, str]):
    calls = []

    def process(audio_bytes: bytes, start: float) -> list:
        calls.append(start)
        text = texts.get(start, "")
        if not text:
            return []
        event = AudioEvent(action="bet", amount=2000, timestamp=start, utterance_start_ts=start, raw_text=text)
        return [Transcript(text=text, confidence=0.5, events=(event,), audio_sec=0.5, infer_sec=0.1,
                           utterance_start_ts=start, heard_at=start + 900.0)]

    return process, calls


class TestNames:
    def test_the_start_comes_from_the_file_name(self):
        assert offplay_start("off_1791000040000.wav") == 1791000040.0
        assert offplay_start("1791000040000.wav") is None
        assert offplay_start("off_x.wav") is None


class TestTranscribe:
    def test_it_writes_offplay_rows_in_the_transcript_shape_and_resumes(self, tmp_path):
        session = find_sessions(_logs(tmp_path))[0]
        process, calls = _fake({1791000040.0: "ベット 2000"})
        written, failed = transcribe_session(session, process, log=lambda _m: None)
        assert (written, failed) == (2, 0) and calls == [1791000040.0, 1791000045.0]
        rows = [json.loads(line) for line in (tmp_path / f"{SID}{OFFPLAY_SUFFIX}").read_text().splitlines()]
        assert [(r["utterance_start_ts"], r["text"], r["audio_file"], r["offplay"]) for r in rows] == [
            (1791000040.0, "ベット 2000", "off_1791000040000.wav", True),
            (1791000045.0, "", "off_1791000045000.wav", True)]
        assert rows[0]["events"][0]["action"] == "bet" and rows[1]["no_speech"] is True
        # 2 回目は書き起こし済みを飛ばす（声の無かった音も）
        process, calls = _fake({})
        assert transcribe_session(session, process, log=lambda _m: None) == (0, 0) and calls == []

    def test_one_broken_file_does_not_stop_the_rest(self, tmp_path):
        session = find_sessions(_logs(tmp_path))[0]

        def process(audio_bytes, start):
            if start == 1791000040.0:
                raise RuntimeError("boom")
            return []

        assert transcribe_session(session, process, log=lambda _m: None) == (1, 1)


class TestMerge:
    def test_offplay_rows_are_added_in_time_order_and_live_rows_win(self):
        live = [{"utterance_start_ts": 10.0, "text": "コール"}, {"utterance_start_ts": 30.0, "text": "チェック"}]
        off = [{"utterance_start_ts": 20.0, "text": "ベット", "offplay": True},
               {"utterance_start_ts": 30.0, "text": "別の読み", "offplay": True}]
        assert [r["text"] for r in merge_offplay(live, off)] == ["コール", "ベット", "チェック"]
        assert merge_offplay(live, []) is live

    def test_the_estimator_input_includes_offplay_rows(self, tmp_path):
        from tools.estimate import session_transcripts
        from tools.eval_store import SessionFiles

        logs = _logs(tmp_path)
        row = {"utterance_start_ts": 1791000040.0, "text": "ベット 2000", "offplay": True}
        (logs / f"{SID}{OFFPLAY_SUFFIX}").write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
        rows = session_transcripts(SessionFiles(SID, logs))
        assert [r["text"] for r in rows] == ["コール", "ベット 2000"]


class TestLiveProcessor:
    def test_it_reads_offplay_audio_through_the_live_path(self, monkeypatch, tmp_path):
        """ライブと同じ組み立て（`main._make_audio_thread` → `_process_chunk`）で読む = 額・アクションの読みがライブと同じ。"""
        import audio.recorder as recorder
        from audio.recognizer import Recognition
        from tools.offplay_audio import live_processor

        class FakeWhisper:
            ready = True
            load_error = None

            def __init__(self, **_kw):
                pass

            def recognize(self, audio_bytes):
                return Recognition(text="ベット 2000", confidence=0.6)

        monkeypatch.setattr(recorder, "WhisperTranscriber", FakeWhisper)
        process, error = live_processor({"audio": {"second_ear": {"enabled": False}}})
        assert error is None
        out = process(b"\x00\x00" * 8000, 1791000040.0)
        assert [(t.text, [(e.action, e.amount) for e in t.events], t.utterance_start_ts) for t in out] == [
            ("ベット 2000", [("bet", 2000)], 1791000040.0)]

    def test_it_says_why_when_whisper_is_missing(self, monkeypatch):
        import audio.recorder as recorder
        from tools.offplay_audio import live_processor

        class Missing:
            ready = False
            load_error = "faster-whisper が入っていません"

            def __init__(self, **_kw):
                pass

        monkeypatch.setattr(recorder, "WhisperTranscriber", Missing)
        assert live_processor({"audio": {"second_ear": {"enabled": False}}}) == (None, "faster-whisper が入っていません")


class _Thread:
    """`AudioThread` の代わり（`transcribe_offline` と `backlog` だけ）。"""

    asr_ready = True

    def __init__(self, busy: int = 0):
        self.busy = busy
        self.calls = []

    def backlog(self) -> int:
        self.busy = max(0, self.busy - 1)
        return self.busy

    def transcribe_offline(self, audio_bytes, start):
        self.calls.append(start)
        return Transcript(text="コール", confidence=0.5, events=(), audio_sec=0.5, infer_sec=0.1,
                          utterance_start_ts=start, heard_at=start)


class TestAutomatic:
    def test_after_the_session_it_transcribes_with_the_loaded_model(self, tmp_path):
        from tools.offplay_audio import after_session

        logs = _logs(tmp_path)
        thread = _Thread(busy=3)
        said = []
        assert after_session(thread, logs, SID, {}, say=said.append, sleep=lambda _s: None) == 2
        assert thread.calls == [1791000040.0, 1791000045.0]
        assert (logs / f"{SID}{OFFPLAY_SUFFIX}").exists() and "2 件" in said[0]
        # 2 回目は何もしない（書き起こし済み）
        assert after_session(thread, logs, SID, {}, say=said.append) == 0

    def test_it_can_be_turned_off_and_waits_for_the_worker(self, tmp_path):
        from tools.offplay_audio import after_session

        logs = _logs(tmp_path)
        thread = _Thread()
        assert after_session(thread, logs, SID, {"audio": {"transcribe_offplay": False}}) == 0 and thread.calls == []
        now = [0.0]

        def sleep(sec):
            now[0] += sec

        busy = _Thread(busy=10 ** 9)                 # 聞き取りの作業スレッドが止まらない
        said = []
        assert after_session(busy, logs, SID, {}, say=said.append, clock=lambda: now[0], sleep=sleep) == 0
        assert busy.calls == [] and "ログをまとめる" in said[-1]

    def test_catch_up_loads_whisper_only_when_needed(self, tmp_path):
        from tools.offplay_audio import catch_up, processor_for

        logs = _logs(tmp_path)
        loads = []

        def loader(config):
            loads.append(config)
            return processor_for(_Thread()), None

        assert catch_up(logs, ["other"], {}, say=lambda _m: None, loader=loader) == 0 and loads == []
        assert catch_up(logs, [SID], {}, say=lambda _m: None, loader=loader) == 2 and len(loads) == 1
        assert catch_up(logs, [SID], {}, say=lambda _m: None, loader=loader) == 0 and len(loads) == 1

    def test_catch_up_does_nothing_when_whisper_is_missing(self, tmp_path):
        from tools.offplay_audio import catch_up

        said = []
        assert catch_up(_logs(tmp_path), [SID], {}, say=said.append,
                        loader=lambda _c: (None, "faster-whisper が入っていません")) == 0
        assert "音声だけ入れます" in said[-1]


class TestTranscribeOffline:
    def test_it_does_not_save_report_or_send_actions(self, tmp_path):
        """ロガーの終わりに使う `AudioThread.transcribe_offline`: 読み方はライブと同じで、音声の保存・記録・送出をしない。"""
        import queue
        import threading

        from audio.recognizer import Recognition
        from audio.recorder import AudioThread

        class Fake:
            ready = True

            def recognize(self, audio_bytes):
                return Recognition(text="レイズ 3000", confidence=0.7)

        heard, q = [], queue.Queue()
        thread = AudioThread(audio_queue=q, stop_event=threading.Event(), transcriber=Fake(),
                             on_transcript=heard.append, audio_dir=tmp_path)
        t = thread.transcribe_offline(b"\x00\x00" * 8000, 12.0)
        assert t.text == "レイズ 3000" and [(e.action, e.amount) for e in t.events] == [("raise", 3000)]
        assert heard == [] and q.empty() and not list(tmp_path.glob("*.wav"))
        thread._process_chunk(b"\x00\x00" * 8000, utterance_start_ts=13.0)   # noqa: SLF001 — ライブは今までどおり
        assert len(heard) == 1 and q.qsize() == 1 and len(list(tmp_path.glob("*.wav"))) == 1
