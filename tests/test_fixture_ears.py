"""tests/test_fixture_ears.py — 開発データの全発話に第 2 の耳の結果を足す（tools/fixture_ears.py）。

本物のモデルは使わない（決まった結果を返す偽物）。足した耳は記録だけ（`ear_wanted` = 偽）なので、読み直しは変わらない。
"""
from __future__ import annotations

import json
import wave
from pathlib import Path

import numpy as np

from audio import second_ear as se
from tools import eval_store, fixture_ears


class FakeEar:
    def __init__(self) -> None:
        self.heard = 0

    def hear(self, samples: np.ndarray) -> se.EarResult:
        self.heard += 1
        return se.EarResult("これ", -1.5, [("コール", -2.0), ("六百", -9.0)], amounts=[(600, -9.0)],
                            words={"コール": -2.0, "これ": -1.5}, frames=12)


def _wav(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16000)
        out.writeframes(b"\x10\x00" * 1600)


def _setup(tmp_path: Path) -> tuple[Path, Path, list[dict]]:
    sid = "782c457d384b44edae3b4bf31b86fc7b"
    logs = tmp_path / "logs" / sid
    rows = [
        {"utterance_start_ts": 1791355618.85, "heard_at": 1791355621.0, "text": "コール", "audio_sec": 1.0},
        {"utterance_start_ts": 1791355630.5, "heard_at": 1791355633.0, "text": "のっぴょく", "audio_sec": 1.0,
         "ear": se.EarResult("六百", -1.0, [("六百", -1.0)]).to_dict()},
        {"utterance_start_ts": 1791355640.25, "heard_at": 1791355642.0, "text": "", "no_speech": True,
         "audio_sec": 1.0},
        {"utterance_start_ts": 1791355650.0, "heard_at": 1791355652.0, "text": "チェック", "audio_sec": 1.0},
    ]
    (logs / f"{sid}.transcripts.jsonl").parent.mkdir(parents=True)
    (logs / f"{sid}.transcripts.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    for row in rows[:3]:                                  # 4 行目の音声は無い
        _wav(logs / "audio" / fixture_ears.wav_name(row["utterance_start_ts"]))
    fixture = tmp_path / "fixtures" / "2026-10-07-782c457d"
    fixture.mkdir(parents=True)
    (fixture / "transcripts.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    return fixture, tmp_path / "logs", rows


def _rows(fixture: Path) -> list[dict]:
    return [json.loads(line) for line in (fixture / "transcripts.jsonl").read_text(encoding="utf-8").splitlines()]


def test_the_wav_name_is_the_start_time_in_milliseconds():
    assert fixture_ears.wav_name(1791355618.850) == "1791355618850.wav"
    assert fixture_ears.wav_name(1791355985.3645) == "1791355985364.wav"


def test_every_utterance_gets_the_ear_for_the_record(tmp_path):
    fixture, logs, rows = _setup(tmp_path)
    session = fixture_ears.session_audio([logs], "782c457d")
    ear = FakeEar()
    counts = fixture_ears.add_ears(fixture, session, ear, log=lambda _: None)
    assert counts == {"added": 2, "enriched": 1, "kept": 0, "no_audio": 1, "failed": 0} and ear.heard == 3
    plain, live, no_voice, no_audio = _rows(fixture)
    # 耳の無かった行（声ではない音も）: 記録だけの耳
    assert plain["ear"]["text"] == "これ" and plain["ear"]["offline"] is True and plain["ear_wanted"] is False
    assert plain["ear"]["words"] == {"コール": -2.0, "これ": -1.5} and plain["ear"]["frames"] == 12
    assert no_voice["ear_wanted"] is False and no_voice["ear"]["words"]
    # ライブの耳の行: 候補・額はライブのまま、短い語だけ足す（使った耳のまま）
    assert live["ear"]["candidates"] == [{"text": "六百", "logp": -1.0}] and "amounts" not in live["ear"]
    assert live["ear"]["words"] == {"コール": -2.0, "これ": -1.5} and "ear_wanted" not in live
    assert "ear" not in no_audio
    # 何度流しても同じ（もう足した行は聞かない）
    again = FakeEar()
    assert fixture_ears.add_ears(fixture, session, again, log=lambda _: None)["kept"] == 3 and again.heard == 0


def test_the_reading_does_not_change(tmp_path):
    """足した耳は記録だけ = 読み直しの結果は足す前と同じ（推定器・物差しの基準も変わらない）。"""
    fixture, logs, rows = _setup(tmp_path)
    before = [(e.action, e.amount, e.timestamp) for e in eval_store.reparse_events([], rows)]
    fixture_ears.add_ears(fixture, fixture_ears.session_audio([logs], "782c457d"), FakeEar(), log=lambda _: None)
    after = [(e.action, e.amount, e.timestamp) for e in eval_store.reparse_events([], _rows(fixture))]
    assert after == before and [(a, m) for a, m, _ in after] == [("call", 0), ("bet", 600), ("check", 0)]


def test_a_dry_run_counts_without_hearing(tmp_path):
    fixture, logs, _ = _setup(tmp_path)
    before = (fixture / "transcripts.jsonl").read_text(encoding="utf-8")
    counts = fixture_ears.add_ears(fixture, fixture_ears.session_audio([logs], "782c457d"), None, dry_run=True,
                                   log=lambda _: None)
    assert counts["added"] == 2 and counts["enriched"] == 1 and counts["no_audio"] == 1
    assert (fixture / "transcripts.jsonl").read_text(encoding="utf-8") == before


def test_the_copy_with_the_most_audio_is_used(tmp_path):
    fixture, logs, _ = _setup(tmp_path)
    other = tmp_path / "other" / "782c457d384b44edae3b4bf31b86fc7b"
    other.mkdir(parents=True)
    (other / "782c457d384b44edae3b4bf31b86fc7b.transcripts.jsonl").write_text("", encoding="utf-8")
    session = fixture_ears.session_audio([tmp_path / "other", logs], "782c457d")
    assert session is not None and session.transcripts.parent == logs / "782c457d384b44edae3b4bf31b86fc7b"
    assert fixture_ears.session_audio([tmp_path / "other"], "782c457d") is None      # 音声が 1 つも無い
