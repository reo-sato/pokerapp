"""tests/test_tools_second_ear.py

保存した発話の音声を第 2 の耳（ReazonSpeech）と Whisper の別のやり方で聞き直すツール（`tools/second_ear.py`）。

本物のモデルは使わない: 第 2 の耳は決まった結果を返す偽物、Whisper は `tests/test_tools_rescore_audio.py` の偽物
（正解の文のトークンを強く予測する）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pytest

from audio import second_ear as se
from tests.test_tools_rescore_audio import FakeTokenizer, FakeWhisper, _write_wav
from tools import rescore_audio as ra
from tools import second_ear as tse


class FakeEar:
    """音声の長さ（サンプル数）ごとに決まった結果を返す第 2 の耳。"""

    def __init__(self, results: dict[int, se.EarResult], fail: tuple[int, ...] = ()) -> None:
        self.results = results
        self.fail = fail
        self.heard: list[int] = []

    def hear(self, samples: np.ndarray) -> se.EarResult:
        self.heard.append(len(samples))
        if len(samples) in self.fail:
            raise RuntimeError("decode failed")
        return self.results[len(samples)]


def _session(tmp_path: Path, rows: list[dict], sid: str = "s1") -> ra.SessionAudio:
    (tmp_path / f"{sid}.transcripts.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    (session,) = ra.find_sessions(tmp_path, [sid])
    return session


def _rows(tmp_path: Path) -> list[dict]:
    """0.5 秒の「コール」・1 秒の「のっぴょく」（Whisper が読めない）・声でない音・音声の無い発話。"""
    _write_wav(tmp_path / "audio" / "s1" / "1000.wav", seconds=0.5)
    _write_wav(tmp_path / "audio" / "s1" / "2000.wav", seconds=1.0)
    _write_wav(tmp_path / "audio" / "s1" / "2500.wav", seconds=0.3)
    return [
        {"utterance_start_ts": 1.0, "heard_at": 2.0, "text": "コール", "audio_file": "1000.wav", "audio_sec": 0.5},
        {"utterance_start_ts": 2.0, "heard_at": 3.0, "text": "のっぴょく", "audio_file": "2000.wav", "audio_sec": 1.0},
        {"utterance_start_ts": 2.5, "text": "", "audio_file": "2500.wav", "no_speech": True},
        {"utterance_start_ts": 3.0, "heard_at": 4.0, "text": "チェック", "audio_file": "3000.wav"},   # 音声が無い
    ]


RESULTS = {
    8000: se.EarResult("コール", -0.4, [("コール", -0.4), ("コールです", -3.0)]),
    16000: se.EarResult("六百", -1.2, [("六百", -1.2), ("六百点", -2.5), ("百", -9.0)]),
}


class TestRunEar:
    def test_each_utterance_once_and_from_where_it_stopped(self, tmp_path):
        session = _session(tmp_path, _rows(tmp_path))
        ear = FakeEar(RESULTS)
        lines: list[str] = []
        assert len(tse.run_ear(session, ear, limit=1, log=lines.append)) == 1
        assert len(tse.run_ear(session, ear, log=lines.append)) == 1                    # 続きから
        assert tse.run_ear(session, ear, log=lines.append) == []                          # 聞き終えた
        rows = ra.read_jsonl(tse.ear_path(session))
        assert [r["audio_file"] for r in rows] == ["1000.wav", "2000.wav"]   # 声でない音・音声の無い発話は飛ばす
        assert rows[1]["whisper_text"] == "のっぴょく" and rows[1]["model"] == se.MODEL_NAME
        assert rows[1]["ear"] == {"text": "六百", "logp": -1.2,
                                  "candidates": [{"text": "六百", "logp": -1.2}, {"text": "六百点", "logp": -2.5},
                                                 {"text": "百", "logp": -9.0}]}
        assert rows[1]["utterance_start_ts"] == 2.0 and rows[1]["heard_at"] == 3.0 and rows[1]["sec"] >= 0

    def test_a_failing_utterance_does_not_stop_the_session(self, tmp_path):
        session = _session(tmp_path, _rows(tmp_path))
        tse.run_ear(session, FakeEar(RESULTS, fail=(8000,)), log=lambda _: None)
        rows = ra.read_jsonl(tse.ear_path(session))
        assert rows[0]["error"] == "RuntimeError: decode failed" and rows[1]["ear"]["text"] == "六百"

    def test_rescued_lists_what_whisper_could_not_read(self, tmp_path):
        session = _session(tmp_path, _rows(tmp_path))
        tse.run_ear(session, FakeEar(RESULTS), log=lambda _: None)
        # 「コール」は Whisper が読めた。「のっぴょく」は読めず、第 2 の耳は「六百」と聞いた
        assert tse.rescued(session) == [("2000.wav", "のっぴょく", "六百", 0.0)]
        with tse.ear_path(session).open("a", encoding="utf-8") as f:     # 何も聞こえなかった発話は出さない
            f.write(json.dumps({"audio_file": "2000.wav", "ear": {"text": "", "logp": -0.1,
                                                                  "candidates": [{"text": "百", "logp": -2.0}]}}) + "\n")
        assert len(tse.rescued(session)) == 1


class TestRunWhisper:
    def test_the_other_ways_and_the_candidates(self, tmp_path):
        session = _session(tmp_path, _rows(tmp_path))
        tse.run_ear(session, FakeEar(RESULTS), log=lambda _: None)
        tok = FakeTokenizer()
        fake = FakeWhisper(tok, "600", ["ご視聴ありがとうございました。"])
        plain = ra.WhisperRescorer(fake, tok, prompt=None)
        prompted = ra.WhisperRescorer(fake, tok, prompt="ポーカー")
        times = tse.run_whisper(session, plain, prompted, log=lambda _: None)
        assert set(times) == {"noprompt", "short", "short_noprompt", "scores"} and len(times["short"]) == 2
        rows = {r["audio_file"]: r for r in ra.read_jsonl(tse.whisper_path(session))}
        row = rows["2000.wav"]
        assert row["window_frames"] == tse.SHORT_WINDOW
        assert row["noprompt"]["text"] == row["short"]["text"] == row["short_noprompt"]["text"] == "600"
        scores = {s["text"]: s["logp"] for s in row["scores"]}
        assert list(scores) == ["六百", "六百点", "百"]                                  # 第 2 の耳の上位の候補
        assert scores["六百"] > scores["百"]                         # 「600」と書いたときの確からしさで比べる
        assert 3000 in fake.windows and tse.SHORT_WINDOW in fake.windows
        assert tse.run_whisper(session, plain, prompted, log=lambda _: None)["short"] == []   # 聞き終えた


class TestRenderings:
    @pytest.mark.parametrize("value, text", [(2500, "2千5百"), (12000, "1万2千"), (10000, "1万"), (1000, "1千")])
    def test_mixed(self, value, text):
        assert tse._mixed(value) == text                                      # noqa: SLF001

    def test_whisper_writes_numbers_in_digits(self):
        assert tse.whisper_renderings("レイズ 二千五百") == ["レイズ 二千五百", "レイズ 2500", "レイズ 2千5百"]
        assert tse.whisper_renderings("六百点") == ["六百点", "600点"]
        assert tse.whisper_renderings("コール") == ["コール"]


class TestPickSessions:
    def test_sessions_with_ground_truth_first(self, tmp_path):
        for sid in ("aa", "bb", "cc"):
            _session(tmp_path, [{"utterance_start_ts": 1.0, "text": "コール"}], sid)
        (tmp_path / "bb.ground_truth.json").write_text("{}", encoding="utf-8")
        assert [s.session_id for s in tse.pick_sessions(tmp_path, None, False)] == ["bb"]
        assert [s.session_id for s in tse.pick_sessions(tmp_path, None, True)] == ["aa", "bb", "cc"]
        assert [s.session_id for s in tse.pick_sessions(tmp_path, ["c"], False)] == ["cc"]

    def test_otherwise_the_newest(self, tmp_path):
        for i, sid in enumerate(("zz", "aa")):          # ID の順ではなく、書いた時刻で選ぶ
            session = _session(tmp_path, [{"utterance_start_ts": 1.0, "text": "コール"}], sid)
            os.utime(session.transcripts, (1000 + i, 1000 + i))
        assert [s.session_id for s in tse.pick_sessions(tmp_path, None, False)] == ["aa"]
        assert tse.pick_sessions(tmp_path / "none", None, False) == []


class TestModel:
    def test_the_model_is_fetched_once(self, tmp_path, monkeypatch):
        calls: list[Path] = []

        def fake_download(folder: Path, report=None) -> None:
            calls.append(folder)
            folder.mkdir(parents=True)
            for name in se.MODEL_FILES:
                (folder / name).write_text("x", encoding="utf-8")
            report(60 * 1024 * 1024)

        monkeypatch.setattr(se, "download_model", fake_download)
        lines: list[str] = []
        assert tse.ensure_model(tmp_path / "m", log=lines.append) and tse.ensure_model(tmp_path / "m", log=lines.append)
        assert calls == [tmp_path / "m"] and "  50 MB" in lines

    def test_a_failed_download_is_reported(self, tmp_path, monkeypatch):
        def fail(folder: Path, report=None) -> None:
            raise OSError("network is unreachable")

        monkeypatch.setattr(se, "download_model", fail)
        lines: list[str] = []
        assert not tse.ensure_model(tmp_path / "m", log=lines.append)
        assert "取得できませんでした" in lines[-1] and "network is unreachable" in lines[-1]


def test_no_sessions(tmp_path, capsys):
    assert tse.main([str(tmp_path)]) == 1
    assert "transcripts.jsonl" in capsys.readouterr().out
