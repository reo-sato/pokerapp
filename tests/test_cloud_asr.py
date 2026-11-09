"""クラウドの音声認識 API と比べる道具（`tools/cloud_asr.py`, 台帳 H の H1, オフライン）。API は呼ばない。"""
from __future__ import annotations

import json
import wave
from pathlib import Path

import pytest

pytest.importorskip("pokerkit")

from tests.test_tools_eval_store import SID, _transcripts, _two_hands  # noqa: E402
from tools import cloud_asr  # noqa: E402
from tools.cloud_asr import PROVIDERS, Job, compare_session, format_summary, jobs_for, run_jobs, summarize  # noqa: E402
from tools.eval_store import find_sessions  # noqa: E402


class TestBilling:
    def test_units_and_minimums(self):
        assert PROVIDERS["openai-4o"].billed_sec(1.2) == 2.0                   # 秒単位で切り上げ
        assert PROVIDERS["groq-turbo"].billed_sec(1.2) == 10.0                 # 1 回の最低 10 秒
        assert PROVIDERS["google-chirp3"].billed_sec(16.0) == 30.0             # 15 秒単位
        assert PROVIDERS["openai-4o"].cost_usd(60.0) == pytest.approx(0.006)


class TestRequests:
    def test_openai_sends_japanese_and_the_live_prompt(self, monkeypatch):
        seen = {}

        def post(url, body, headers):
            seen.update(url=url, body=body, headers=headers)
            return {"text": "レイズ 600"}

        monkeypatch.delenv("POKERAPP_OPENAI_API_KEY", raising=False)   # 環境に本物の鍵があっても偽の鍵を使う
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        monkeypatch.setattr(cloud_asr, "_post", post)
        assert cloud_asr.transcribe(PROVIDERS["openai-4o"], b"RIFFxx", "プロンプト") == "レイズ 600"
        assert seen["url"] == "https://api.openai.com/v1/audio/transcriptions"
        assert seen["headers"]["Authorization"] == "Bearer k"
        body = seen["body"].decode("utf-8", "replace")
        for part in ('name="model"\r\n\r\ngpt-4o-transcribe', 'name="language"\r\n\r\nja', "プロンプト", "RIFFxx"):
            assert part in body

    def test_deepgram_and_google_answers(self, monkeypatch):
        monkeypatch.setenv("DEEPGRAM_API_KEY", "d")
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "p")
        monkeypatch.setenv("GOOGLE_ACCESS_TOKEN", "t")
        answers = {
            "deepgram": {"results": {"channels": [{"alternatives": [{"transcript": "コール"}]}]}},
            "speech.googleapis.com": {"results": [{"alternatives": [{"transcript": "ベット"}]},
                                                  {"alternatives": [{"transcript": " 500"}]}]},
        }
        monkeypatch.setattr(cloud_asr, "_post",
                            lambda url, body, headers: next(v for k, v in answers.items() if k in url))
        assert cloud_asr.transcribe(PROVIDERS["deepgram-nova3"], b"x", None) == "コール"
        assert cloud_asr.transcribe(PROVIDERS["google-chirp3"], b"x", None) == "ベット 500"

    def test_missing_keys_are_named(self, monkeypatch):
        for name in ("OPENAI_API_KEY", "POKERAPP_OPENAI_API_KEY", "GROQ_API_KEY"):
            monkeypatch.delenv(name, raising=False)
        assert "OPENAI_API_KEY" in cloud_asr.missing_key(PROVIDERS["openai-4o"])
        monkeypatch.setenv("GROQ_API_KEY", "g")
        assert cloud_asr.missing_key(PROVIDERS["groq-turbo"]) is None


def _session_with_audio(tmp_path: Path) -> list[dict]:
    """2 ハンドのセッション。記録した Whisper は「レイズ 600」を「れいぞう 600」と聞き違えて読めなかった。"""
    tb = _two_hands(tmp_path)
    rows = _transcripts(tb, tmp_path, heard={"レイズ 600": "れいぞう 600"}, drop=("レイズ 600",))
    truth = [{"hand_id": h.hand_id, "board": list(h.board), "winner_seat": h.winner_seat,
              "actions": [{"street": a.street, "seat": a.seat, "action": a.action, "amount": a.amount}
                          for a in h.actions]} for h in tb.hands]
    (tmp_path / f"{SID}.ground_truth.json").write_text(json.dumps({"hands": truth}), encoding="utf-8")
    audio = tmp_path / "audio" / SID
    audio.mkdir(parents=True)
    for i, r in enumerate(rows):
        with wave.open(str(audio / r["audio_file"]), "wb") as f:
            f.setnchannels(1)
            f.setsampwidth(2)
            f.setframerate(16000)
            f.writeframes(i.to_bytes(2, "little") * 16000)      # 発話ごとに中身を変える（偽の API が見分ける）
    return rows


class TestRunAndCompare:
    def test_the_api_text_is_measured_like_the_live_whisper(self, tmp_path):
        rows = _session_with_audio(tmp_path)
        provider = PROVIDERS["openai-4o"]
        jobs = jobs_for(tmp_path, tmp_path, provider)
        assert len(jobs) == len(rows) and all(isinstance(j, Job) for j in jobs)

        # API の代わり: 記録した Whisper が聞き違えた発話も正しく聞こえた
        heard = {j.path.read_bytes(): ("レイズ 600" if j.row["text"] == "れいぞう 600" else j.row["text"]) for j in jobs}
        written, failed = run_jobs(jobs, provider, tmp_path, workers=2, say=lambda _m: None,
                                   call=lambda _p, wav, _prompt: heard[wav])
        assert (written, failed) == (len(rows), 0)
        assert jobs_for(tmp_path, tmp_path, provider) == []          # 2 回目は送らない（続きから）

        (files,) = find_sessions(tmp_path)
        out = compare_session(files, {}, tmp_path, ["openai-4o"])
        by = {(r["source"], r["ear"]): r for r in out}
        live, api = by[("live", False)], by[("openai-4o", False)]
        # フォールドは札の離脱で決めて声に出していないので、数える行より読める行は少ない。聞き違えたレイズの 1 行ぶん多い
        assert api["actions"] == live["actions"] and api["read"] == live["read"] + 1
        assert (api["amounts_read"], api["amounts"]) == (1, 1) and live["amounts_read"] == 0
        assert api["hands_correct"] == 2 and live["hands_correct"] < 2
        assert api["covered"] == len(rows) and api["cost_usd"] == pytest.approx(len(rows) * 0.006 / 60)
        lines = format_summary(summarize(out))
        assert any(line.startswith("OpenAI gpt-4o-transcribe | なし | ") for line in lines)

    def test_compare_command(self, tmp_path, capsys):
        rows = _session_with_audio(tmp_path)
        provider = PROVIDERS["openai-4o"]
        run_jobs(jobs_for(tmp_path, tmp_path, provider), provider, tmp_path, say=lambda _m: None,
                 call=lambda _p, _wav, _prompt: "コール")
        assert cloud_asr.main(["compare", str(tmp_path), "--json", str(tmp_path / "out.json")]) == 0
        out = capsys.readouterr().out
        assert "いまの Whisper（店舗 PC） | なし | " in out and "OpenAI gpt-4o-transcribe | あり | " in out
        assert len(json.loads((tmp_path / "out.json").read_text())) == 4 and rows

    def test_failed_calls_are_kept_but_sent_again(self, tmp_path):
        _session_with_audio(tmp_path)
        provider = PROVIDERS["groq-turbo"]
        jobs = jobs_for(tmp_path, tmp_path, provider)

        def boom(_p, _wav, _prompt):
            raise cloud_asr.AsrError("HTTP 429: rate limit")

        written, failed = run_jobs(jobs, provider, tmp_path, say=lambda _m: None, call=boom)
        assert written == failed == len(jobs)
        assert len(jobs_for(tmp_path, tmp_path, provider)) == len(jobs)
        (files,) = find_sessions(tmp_path)
        assert {r["source"] for r in compare_session(files, {}, tmp_path, ["groq-turbo"])} == {"live"}

    def test_dry_run_needs_no_key(self, tmp_path, capsys, monkeypatch):
        _session_with_audio(tmp_path)
        monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
        assert cloud_asr.main(["run", str(tmp_path), "--provider", "deepgram-nova3", "--dry-run"]) == 0
        assert "課金の目安" in capsys.readouterr().out
        assert cloud_asr.main(["run", str(tmp_path), "--provider", "deepgram-nova3"]) == 2
