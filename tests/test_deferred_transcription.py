"""書き起こしの後回し（`audio.live_transcribe=false`, 2026-10-28）。

テスト中は書き起こしをその場で使わない: プレー中の発話は音声を保存し、その場では第 2 の耳でブラインド上昇の宣言だけを
聞き分けてロガーに送る。Whisper は裏で順に書き起こして行だけを書き（ロガーには送らない）、残りは終了と「ログをまとめる」で
書き起こす（10/26: 全発話をその場で書き起こして聞き取りが 2〜7 分遅れた）。
"""
from __future__ import annotations

import json
import queue
import threading
import wave
from pathlib import Path

from audio.recorder import AudioThread, Transcript
from tools.offplay_audio import mark_deferred, pending, processor_for, transcribe_deferred
from tools.rescore_audio import find_sessions

SID = "2026-10-28_200000_abcd"


class _Fake:
    ready = True

    def __init__(self, text: str = "レイズ 3200") -> None:
        self.text = text
        self.calls = 0

    def transcribe_with_confidence(self, audio_bytes: bytes):
        self.calls += 1
        return self.text, 0.6


class _Ear:
    def __init__(self, text: str) -> None:
        self.text = text

    def hear(self, samples):
        text = self.text

        class _R:
            def to_dict(self_inner):
                return {"text": text, "logp": -1.0, "candidates": []}

        return _R()


def _thread(tmp_path: Path, *, ear_text: str = "こんにちは", whisper: str = "レイズ 3200"):
    q: queue.Queue = queue.Queue()
    seen: list[Transcript] = []
    thread = AudioThread(audio_queue=q, stop_event=threading.Event(), transcriber=_Fake(whisper),
                         on_transcript=seen.append, audio_dir=tmp_path / "audio" / SID,
                         second_ear=_Ear(ear_text), live_transcribe=False)
    return thread, q, seen


def test_the_live_part_only_saves_the_audio_and_sends_nothing(tmp_path):
    thread, q, seen = _thread(tmp_path)
    thread._process_chunk(b"\x10\x00" * 16000, utterance_start_ts=100.0)   # noqa: SLF001
    assert q.empty() and seen == []                                       # ロガーには何も送らない
    assert (tmp_path / "audio" / SID / "100000.wav").is_file()
    assert thread.backlog() == 0 and thread.oldest_pending_start() is None   # ロガーは後回しの分を待たない
    assert thread._deferred_queue.qsize() == 1                             # noqa: SLF001


def test_a_blind_announcement_is_sent_live_from_the_second_ear(tmp_path):
    thread, q, _seen = _thread(tmp_path, ear_text="ブラインド上がります SB800 BB1600")
    thread._process_chunk(b"\x10\x00" * 16000, utterance_start_ts=100.0)   # noqa: SLF001
    event = q.get_nowait()
    assert event.action == "blind_up" and event.utterance_start_ts == 100.0


def test_the_deferred_row_is_placed_where_it_would_have_been_heard(tmp_path):
    thread, q, seen = _thread(tmp_path)
    thread._process_chunk(b"\x10\x00" * 16000, utterance_start_ts=100.0)   # noqa: SLF001
    item = thread._deferred_queue.get_nowait()                             # noqa: SLF001
    thread._process_deferred(*item)                                        # noqa: SLF001
    (row,) = seen
    assert row.deferred and row.text == "レイズ 3200" and row.audio_file == "100000.wav"
    assert row.heard_at == 101.0 and row.transcribed_at is not None       # 話し始め + 長さ（1 秒）
    assert q.empty()                                                       # アクションはロガーに送らない


def _wav(path: Path, sec: float = 0.5) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(16000)
        f.writeframes(b"\x00\x00" * int(16000 * sec))


def test_the_rest_is_transcribed_after_the_session_and_resumes(tmp_path):
    transcripts = tmp_path / f"{SID}.transcripts.jsonl"
    mark_deferred(tmp_path / "audio" / SID, transcripts)
    for ms in (1791000040000, 1791000045000):
        _wav(tmp_path / "audio" / SID / f"{ms}.wav")
    transcripts.write_text(json.dumps({"utterance_start_ts": 1791000040.0, "audio_file": "1791000040000.wav",
                                       "text": "コール", "deferred": True}) + "\n", encoding="utf-8")
    session = find_sessions(tmp_path)[0]
    assert pending(session) == 1                                           # 裏で書き起こした 1 件は済み
    thread, _q, _seen = _thread(tmp_path)
    process = processor_for(thread)
    assert transcribe_deferred(session, process.deferred, log=lambda _m: None) == (1, 0)
    rows = [json.loads(line) for line in transcripts.read_text(encoding="utf-8").splitlines()]
    assert rows[-1]["audio_file"] == "1791000045000.wav" and rows[-1]["deferred"] is True
    assert rows[-1]["heard_at"] == 1791000045.5 and rows[-1]["text"] == "レイズ 3200"
    assert pending(session) == 0


def test_a_live_session_without_the_mark_is_not_retranscribed(tmp_path):
    """その場で書き起こすセッションは、何も聞こえなかった発話に行が無い。印が無ければ書き起こし直さない。"""
    (tmp_path / f"{SID}.transcripts.jsonl").write_text("", encoding="utf-8")
    _wav(tmp_path / "audio" / SID / "1791000040000.wav")
    assert pending(find_sessions(tmp_path)[0]) == 0


def test_the_evaluation_reads_a_blind_announcement_said_between_hands():
    """ブラインド上昇の宣言はハンドの間（聞き流した発話）に言う。評価の読み直しにはそれだけを、その場の時刻で足す。"""
    from tools.eval_store import with_offplay

    off = [{"utterance_start_ts": 10.0, "audio_sec": 2.0, "heard_at": 999.0, "text": "ブラインド上がります"},
           {"utterance_start_ts": 20.0, "audio_sec": 1.0, "heard_at": 999.0, "text": "コール"}]
    rows = with_offplay([{"utterance_start_ts": 5.0, "heard_at": 6.0, "text": "チェック"}], off)
    assert [(r["utterance_start_ts"], r["heard_at"]) for r in rows] == [(5.0, 6.0), (10.0, 12.0)]
