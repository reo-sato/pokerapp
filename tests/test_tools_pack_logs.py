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

    def test_repeated_lines_are_folded(self, root, tmp_path):
        """店舗 2026-10-01: 閉じたマイクの警告が 9 分で 3470 万行 → 最初の 1 行と「さらに N 回」に畳む。"""
        warn = "2026-09-27 13:47:{:02d},{:03d} [AudioThread] WARNING audio.recorder: Audio read error: Stream closed\n"
        lines = [_log_line(START, f"Created session {NEW} (2026-09-27_134604)")]
        lines += [warn.format(10 + i // 1000, i % 1000) for i in range(5000)]
        lines += [_log_line(START + timedelta(seconds=90), "AudioThread stopped")]
        (root / "logs" / "pokerapp.log").write_text("".join(lines), encoding="utf-8")
        out, manifest = _pack(root, tmp_path)
        with zipfile.ZipFile(out) as zf:
            log = zf.read("pokerapp.log").decode("utf-8").splitlines()
        assert len(log) == 4 and "Audio read error" in log[1]
        assert log[2] == "2026-09-27 13:47:14 …（上の行がさらに 4999 回続きました）"
        assert "AudioThread stopped" in log[3]

    def test_the_same_line_after_a_gap_is_kept(self, root, tmp_path):
        line = "{} [MainThread] INFO x: same line\n"
        (root / "logs" / "pokerapp.log").write_text(
            line.format("2026-09-27 13:46:30,000") + line.format("2026-09-27 13:46:31,000")
            + line.format("2026-09-27 16:00:00,000")              # どの時間帯にも入らない
            + line.format("2026-09-27 16:00:01,000"), encoding="utf-8")
        windows = [(START, START + timedelta(minutes=1)),
                   (datetime(2026, 9, 27, 16, 0, 1), datetime(2026, 9, 27, 16, 2))]
        text = pack_logs.slice_app_log(root / "logs" / "pokerapp.log", windows)
        assert text.splitlines() == [
            "2026-09-27 13:46:30,000 [MainThread] INFO x: same line",
            "2026-09-27 13:46:31 …（上の行がさらに 1 回続きました）",
            "2026-09-27 16:00:01,000 [MainThread] INFO x: same line",
        ]

    def test_a_long_log_is_searched_not_read_from_the_top(self, root, tmp_path, monkeypatch):
        """ログは時刻の順に追記されるので、時間帯の始まりへ二分探索で飛ぶ（同じ結果）。"""
        monkeypatch.setattr(pack_logs, "_SEEK_SLACK", 256)
        early = "".join(_log_line(datetime(2026, 9, 26, 0, 0) + timedelta(seconds=i), f"old {i}")
                        for i in range(3000))
        log = root / "logs" / "pokerapp.log"
        log.write_text(early + log.read_text(encoding="utf-8"), encoding="utf-8")
        out, manifest = _pack(root, tmp_path)
        with zipfile.ZipFile(out) as zf:
            text = zf.read("pokerapp.log").decode("utf-8")
        assert f"Created session {NEW}" in text and "old " not in text and "much later line" not in text
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

    def test_large_audio_is_split_into_parts_that_can_be_attached(self, root, tmp_path):
        """音声付き（オーナー 2026-09-29）: 1 つの zip が上限を超えたら、音声を 2 つ目以降に分ける。全部を展開すると 1 つと同じ。"""
        for i in range(3, 9):                                     # 音声を 8 個（各 1004 バイト）に
            (root / "logs" / "audio" / NEW / f"{i}.wav").write_bytes(b"RIFF" + bytes(1000))
        single, _ = _pack(root, tmp_path / "one", audio=True, part_bytes=0)
        out, manifest = _pack(root, tmp_path, audio=True, part_bytes=8000)
        parts = [out.parent / name for name in manifest["parts"]]
        assert len(parts) >= 2 and out == parts[0]
        assert [p.name for p in parts] == [f"pokerlogs_20260927_144850_{i}of{len(parts)}.zip"
                                           for i in range(1, len(parts) + 1)]
        first = _names(parts[0])
        assert "manifest.json" in first and f"{NEW}/{NEW}.json" in first and "pokerapp.log" in first
        for part in parts[1:]:
            assert all("/audio/" in n for n in _names(part))       # 2 つ目以降は音声の続きだけ
        assert sorted(n for part in parts for n in _names(part)) == _names(single)
        assert all(part.stat().st_size <= 8000 + 2048 for part in parts[1:])
        with zipfile.ZipFile(parts[0]) as zf:
            assert json.loads(zf.read("manifest.json"))["parts"] == [p.name for p in parts]

    def test_small_logs_stay_in_one_zip(self, root, tmp_path):
        out, manifest = _pack(root, tmp_path, audio=True)
        assert manifest["parts"] == [out.name] and out.name == "pokerlogs_20260927_144850.zip"

    def test_an_old_session_by_id(self, root, tmp_path):
        out, manifest = _pack(root, tmp_path, session_ids=[OLD])
        assert f"{OLD}/{OLD}.json" in _names(out) and not any(n.startswith(NEW) for n in _names(out))
        with zipfile.ZipFile(out) as zf:
            assert "old session line" in zf.read("pokerapp.log").decode("utf-8")

    def test_no_sessions(self, tmp_path):
        (tmp_path / "logs").mkdir()
        with pytest.raises(PackError):
            pack(tmp_path / "logs", tmp_path / "out", root=tmp_path)


CORPUS = "20260927_140000_owner"


def _corpus(logs: Path, name: str = CORPUS, when: datetime = END) -> Path:
    """読み上げ集（`tools/read_corpus.py`）のフォルダ。"""
    folder = logs / "corpus" / name
    folder.mkdir(parents=True)
    (folder / "meta.json").write_text(json.dumps({"speaker": "オーナー", "round": 1}, ensure_ascii=False),
                                      encoding="utf-8")
    (folder / "labels.jsonl").write_text('{"id": "act01"}\n', encoding="utf-8")
    (folder / "transcripts.jsonl").write_text('{"id": "act01"}\n', encoding="utf-8")
    (folder / "act01_t1_m1.wav").write_bytes(b"RIFF" + bytes(500))
    (folder / "full_m1.wav").write_bytes(b"RIFF" + bytes(5000))             # ずっと録った音声 = 入れない
    _touch(list(folder.iterdir()), when)
    return folder


class TestCorpus:
    def test_recent_corpus_goes_in_with_its_phrase_audio(self, root, tmp_path):
        _corpus(root / "logs")
        _corpus(root / "logs", "20260920_100000_old", datetime(2026, 9, 20, 10))
        out, manifest = _pack(root, tmp_path, audio=True)
        names = _names(out)
        assert {f"corpus/{CORPUS}/meta.json", f"corpus/{CORPUS}/labels.jsonl", f"corpus/{CORPUS}/transcripts.jsonl",
                f"corpus/{CORPUS}/act01_t1_m1.wav"} <= set(names)
        assert not any("full_m1" in n or "20260920" in n for n in names)
        assert manifest["corpora"] == [{"name": CORPUS, "speaker": "オーナー", "round": 1, "updated_at": END.isoformat(),
                                        "files": ["labels.jsonl", "meta.json", "transcripts.jsonl"], "audio_files": 1}]
        assert f"{NEW}/{NEW}.json" in names                                   # セッションも今までどおり

    def test_corpus_audio_follows_the_audio_setting(self, root, tmp_path):
        _corpus(root / "logs")
        out, manifest = _pack(root, tmp_path, audio=False)
        assert f"corpus/{CORPUS}/labels.jsonl" in _names(out) and not any(n.endswith(".wav") for n in _names(out))

    def test_a_named_session_leaves_the_corpus_out(self, root, tmp_path):
        _corpus(root / "logs")
        out, manifest = _pack(root, tmp_path, session_ids=[NEW])
        assert manifest["corpora"] == [] and not any(n.startswith("corpus/") for n in _names(out))

    def test_only_a_corpus(self, tmp_path):
        logs = tmp_path / "logs"
        _corpus(logs, when=datetime(2026, 9, 20, 10))                      # 12 時間より前でも、それしか無ければ入れる
        out, manifest = pack(logs, tmp_path / "out", root=tmp_path, now=END)
        assert manifest["sessions"] == [] and [c["name"] for c in manifest["corpora"]] == [CORPUS]
        assert f"corpus/{CORPUS}/act01_t1_m1.wav" in _names(out)

    def test_main_reports_the_corpus(self, root, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(pack_logs, "ROOT", root)
        _corpus(root / "logs", when=datetime.now())
        assert pack_logs.main(["--out-dir", str(tmp_path / "out"), "--no-open", "--audio"]) == 0
        assert f"読み上げ集 1 つ: {CORPUS}（オーナー・句の音声 1）" in capsys.readouterr().out


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

    def test_main_reports_every_part(self, root, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(pack_logs, "ROOT", root)
        for i in range(3, 9):
            (root / "logs" / "audio" / NEW / f"{i}.wav").write_bytes(b"RIFF" + bytes(100_000))
        code = pack_logs.main(["--out-dir", str(tmp_path / "out"), "--no-open", "--audio", "--part-mb", "0.3"])
        out = capsys.readouterr().out
        parts = sorted((tmp_path / "out").glob("pokerlogs_*of*.zip"))
        assert code == 0 and len(parts) >= 2
        assert f"{len(parts)} 個の zip に分けました" in out and f"{len(parts)} 個すべてをチャットに添付してください" in out

    def test_main_reads_the_log_dir_from_config(self, root, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(pack_logs, "ROOT", root)
        assert pack_logs.main(["--out-dir", str(tmp_path / "out"), "--no-open", "--session", "nope"]) == 1
        assert "見つかりません" in capsys.readouterr().out


def test_redact_keeps_empty_values():
    assert redact({"staff_token": "", "a": [{"api_key": "k"}], "n": 1}) == {
        "staff_token": "", "a": [{"api_key": "***"}], "n": 1,
    }
