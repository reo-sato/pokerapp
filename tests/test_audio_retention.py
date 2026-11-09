"""発話の音声を 90 日で消す（ADR-0072, 2026-11-09）。書き起こし・記録は消さない。"""
from __future__ import annotations

import os
import threading

from core.audio_retention import keep_days, prune_audio, run_forever

DAY = 86400.0
NOW = 1_800_000_000.0


def _session(tmp_path, sid: str, age_days: float):
    folder = tmp_path / "audio" / sid
    folder.mkdir(parents=True)
    wav = folder / "100000.wav"
    wav.write_bytes(b"RIFF")
    t = NOW - age_days * DAY
    os.utime(wav, (t, t))
    return folder


def test_only_old_audio_is_removed(tmp_path):
    old = _session(tmp_path, "2026-08-01_200000_aaaa", 91)
    new = _session(tmp_path, "2026-10-01_200000_bbbb", 30)
    (tmp_path / "2026-08-01_200000_aaaa.transcripts.jsonl").write_text("{}\n")
    assert prune_audio(tmp_path, 90, clock=lambda: NOW) == ["2026-08-01_200000_aaaa"]
    assert not old.exists() and new.exists()
    assert (tmp_path / "2026-08-01_200000_aaaa.transcripts.jsonl").exists()     # 文は残す
    assert prune_audio(tmp_path, 90, clock=lambda: NOW) == []


def test_zero_keeps_everything(tmp_path):
    _session(tmp_path, "s", 400)
    assert prune_audio(tmp_path, 0, clock=lambda: NOW) == [] and (tmp_path / "audio" / "s").exists()
    assert prune_audio(tmp_path / "nothing", 90) == []


def test_the_setting():
    assert keep_days({}) == 90
    assert keep_days({"audio": {"keep_days": 0}}) == 0
    assert keep_days({"audio": {"keep_days": "x"}}) == 90


def test_it_runs_at_start_and_stops(tmp_path):
    _session(tmp_path, "s", 400)
    stop = threading.Event()
    stop.set()
    run_forever(tmp_path, 90, stop)
    assert not (tmp_path / "audio" / "s").exists()


def test_the_store_server_starts_it(tmp_path, monkeypatch):
    from api import store_server

    started = []
    monkeypatch.setattr(store_server, "keep_running", lambda name, target, stop: started.append(name))
    assert store_server.start_audio_retention({"audio": {"keep_days": 0}}, tmp_path, threading.Event()) is None
    assert store_server.start_audio_retention({}, tmp_path, threading.Event()) == 90
    assert started == ["古い音声の削除"]
