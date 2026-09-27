"""tests/test_tools_pack_logs.py

テストのログを 1 つの zip にまとめる（`tools/pack_logs.py`, レビューに送るため）。
"""
from __future__ import annotations

import json
import os
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import tools.pack_logs as pack_logs
from tools.pack_logs import PackError, find_sessions, pack, redact, select_sessions

NEW = "c2cd4a53adef46488eea9c977ef1b847"          # 今日のセッション（session レイヤの UUID）
OLD = "2026-09-25_133822_session1"                 # 2 日前のセッション（時刻入りの ID）
START = datetime(2026, 9, 27, 13, 46, 4)
END = datetime(2026, 9, 27, 13, 48, 50)


def _ts(dt: datetime) -> float:
    return dt.timestamp()


def _touch(paths, dt: datetime) -> None:
    for p in paths:
        os.utime(p, (_ts(dt), _ts(dt)))


def _log_line(dt: datetime, msg: str) -> str:
    return f"{dt:%Y-%m-%d %H:%M:%S},123 [MainThread] INFO x: {msg}\n"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    root = tmp_path / "app"
    logs = root / "logs"
    (logs / "audio" / NEW).mkdir(parents=True)
    new_files = {
        f"{NEW}.json": json.dumps({"session_id": NEW, "hands": [{"hand_id": 1}, {"hand_id": 2}]}),
        f"{NEW}.events.jsonl": json.dumps({"type": "rfid", "timestamp": _ts(START + timedelta(seconds=18))}) + "\n",
        f"{NEW}.transcripts.jsonl": json.dumps({"utterance_start_ts": _ts(START + timedelta(seconds=17)),
                                                "text": "レイズ600", "audio_file": "1.wav"},
                                               ensure_ascii=False) + "\n",
        f"{NEW}.table_state.json": "{}",
        f"{NEW}.ground_truth.json": json.dumps({"session_id": NEW, "hands": []}),
        f"{NEW}.rescored.jsonl": json.dumps({"audio_file": "1.wav", "best": "レイズ600"}, ensure_ascii=False) + "\n",
    }
    for name, body in new_files.items():
        (logs / name).write_text(body, encoding="utf-8")
    for i in (1, 2):
        (logs / "audio" / NEW / f"{i}.wav").write_bytes(b"RIFF" + bytes(1000))
    _touch([logs / n for n in new_files] + list((logs / "audio" / NEW).glob("*.wav")), END)
    old_files = [logs / f"{OLD}.json", logs / f"{OLD}.events.jsonl"]
    old_files[0].write_text(json.dumps({"session_id": OLD, "hands": [{"hand_id": 1}]}), encoding="utf-8")
    old_files[1].write_text("", encoding="utf-8")
    _touch(old_files, datetime(2026, 9, 25, 13, 50))
    (logs / "notes.txt").write_text("x", encoding="utf-8")            # セッションではないファイル
    (logs / "pokerapp.log").write_text(
        _log_line(datetime(2026, 9, 25, 13, 40), "old session line")
        + _log_line(START, f"Created session {NEW} (2026-09-27_134604)")
        + _log_line(START + timedelta(seconds=30), "New hand started")
        + "Traceback (most recent call last):\n  File x\n"
        + _log_line(END + timedelta(hours=2), "much later line"),
        encoding="utf-8",
    )
    (root / "config.json").write_text(json.dumps({
        "viewer_api": {"staff_token": "secret123", "bind_port": 8788},
        "session": {"log_dir": "./logs"},
    }), encoding="utf-8-sig")
    (root / "rfid_cards.json").write_text('{"cards": {}}', encoding="utf-8")
    (root / "installer").mkdir()
    (root / "installer" / "branch.txt").write_text("claude/confident-hawking-5e4hff\n", encoding="utf-8")
    (root / "integration").mkdir()
    (root / "integration" / "engine.py").write_bytes(b"x = 1\r\n")
    return root


def _pack(root: Path, tmp_path: Path, **kw):
    return pack(root / "logs", tmp_path / "out", root=root, now=END + timedelta(hours=1), **kw)


def _names(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as zf:
        return sorted(zf.namelist())


class TestSelect:
    def test_sessions_are_grouped_by_id(self, root):
        sessions = find_sessions(root / "logs")
        assert set(sessions) == {NEW, OLD}
        assert len(sessions[NEW].files) == 6 and len(sessions[NEW].audio) == 2

    def test_recent_sessions_or_the_latest(self, root):
        sessions = find_sessions(root / "logs")
        assert [s.session_id for s in select_sessions(sessions, now=END + timedelta(hours=1))] == [NEW]
        # 12 時間より前でも、1 つも無ければいちばん新しいもの
        assert [s.session_id for s in select_sessions(sessions, now=END + timedelta(days=5))] == [NEW]
        assert [s.session_id for s in select_sessions(sessions, hours=72, now=END)] == [OLD, NEW]
        assert [s.session_id for s in select_sessions(sessions, all_sessions=True)] == [OLD, NEW]
        assert [s.session_id for s in select_sessions(sessions, ids=[OLD])] == [OLD]
        with pytest.raises(PackError):
            select_sessions(sessions, ids=["nope"])


class TestZip:
    def test_contents_of_the_latest_session(self, root, tmp_path):
        out, manifest = _pack(root, tmp_path)
        assert out.name == "pokerlogs_20260927_144850.zip"
        assert _names(out) == sorted([
            "manifest.json", "pokerapp.log", "config.json", "rfid_cards.json",
            f"{NEW}/{NEW}.json", f"{NEW}/{NEW}.events.jsonl", f"{NEW}/{NEW}.transcripts.jsonl",
            f"{NEW}/{NEW}.table_state.json", f"{NEW}/{NEW}.ground_truth.json", f"{NEW}/{NEW}.rescored.jsonl",
            f"{NEW}/audio/1.wav", f"{NEW}/audio/2.wav",
        ])
        s = manifest["sessions"][0]
        assert (s["session_id"], s["hands"], s["audio_files"]) == (NEW, 2, 2)
        assert manifest["audio_included"] is True
        assert manifest["branch"] == "claude/confident-hawking-5e4hff"
        # 改行を LF に揃えてから指紋をとる（Windows の CRLF でも同じ版と分かる）
        assert manifest["code_fingerprint"]["integration/engine.py"] == pack_logs.hashlib.sha256(
            b"x = 1\n").hexdigest()[:16]

    def test_app_log_is_cut_to_the_session(self, root, tmp_path):
        out, manifest = _pack(root, tmp_path)
        with zipfile.ZipFile(out) as zf:
            log = zf.read("pokerapp.log").decode("utf-8")
        assert f"Created session {NEW}" in log and "New hand started" in log
        assert "Traceback" in log and "File x" in log                  # 時刻の無い続きの行
        assert "old session line" not in log and "much later line" not in log
        assert manifest["app_log_lines"] == 4

    def test_config_tokens_are_hidden(self, root, tmp_path):
        out, _ = _pack(root, tmp_path)
        with zipfile.ZipFile(out) as zf:
            config = json.loads(zf.read("config.json"))
        assert config["viewer_api"] == {"staff_token": "***", "bind_port": 8788}

    def test_audio_limit_and_overrides(self, root, tmp_path, monkeypatch):
        monkeypatch.setattr(pack_logs, "AUDIO_AUTO_LIMIT", 10)
        out, manifest = _pack(root, tmp_path)
        assert manifest["audio_included"] is False and not any("/audio/" in n for n in _names(out))
        out, manifest = _pack(root, tmp_path, audio=True)
        assert manifest["audio_included"] is True and sum("/audio/" in n for n in _names(out)) == 2
        monkeypatch.setattr(pack_logs, "AUDIO_AUTO_LIMIT", 10**9)
        out, manifest = _pack(root, tmp_path, audio=False)
        assert manifest["audio_included"] is False

    def test_an_old_session_by_id(self, root, tmp_path):
        out, manifest = _pack(root, tmp_path, session_ids=[OLD])
        assert f"{OLD}/{OLD}.json" in _names(out) and not any(n.startswith(NEW) for n in _names(out))
        with zipfile.ZipFile(out) as zf:
            assert "old session line" in zf.read("pokerapp.log").decode("utf-8")

    def test_no_sessions(self, tmp_path):
        (tmp_path / "logs").mkdir()
        with pytest.raises(PackError):
            pack(tmp_path / "logs", tmp_path / "out", root=tmp_path)


class TestText:
    def test_single_text_file_without_audio(self, root, tmp_path):
        out, manifest = _pack(root, tmp_path, text=True)
        body = out.read_text(encoding="utf-8")
        assert out.suffix == ".txt" and manifest["audio_included"] is False
        assert f"===== FILE {NEW}/{NEW}.json" in body and "===== FILE manifest.json" in body
        assert "レイズ600" in body and ".wav (" not in body and body.endswith("===== END =====\n")


class TestMain:
    def test_main_writes_and_reports(self, root, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(pack_logs, "ROOT", root)
        code = pack_logs.main(["--out-dir", str(tmp_path / "out"), "--no-open", "--all"])
        out = capsys.readouterr().out
        assert code == 0
        assert "セッション 2 つ" in out and "この 1 ファイルをチャットに添付してください" in out
        assert len(list((tmp_path / "out").glob("pokerlogs_*.zip"))) == 1

    def test_main_reads_the_log_dir_from_config(self, root, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(pack_logs, "ROOT", root)
        assert pack_logs.main(["--out-dir", str(tmp_path / "out"), "--no-open", "--session", "nope"]) == 1
        assert "見つかりません" in capsys.readouterr().out


def test_redact_keeps_empty_values():
    assert redact({"staff_token": "", "a": [{"api_key": "k"}], "n": 1}) == {
        "staff_token": "", "a": [{"api_key": "***"}], "n": 1,
    }
