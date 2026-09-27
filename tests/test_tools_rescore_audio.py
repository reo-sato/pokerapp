"""tests/test_tools_rescore_audio.py

保存した発話の音声を Whisper で採点し直すツール（`tools/rescore_audio.py`, ADR-0056 追記 1 の S2）。

本物の Whisper は店舗 PC でしか動かない（モデルの取得が要る）ので、同じ呼び方に答える偽のモデルで確かめる。
偽のモデルは「正解の文」のトークンを強く予測し、正解の文を言い終えたところで「終わり」を予測する。
"""
from __future__ import annotations

import json
import math
import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from tools import rescore_audio as ra

EOT, SOT_PREV, NO_TS = 900, 901, 902
SOT_SEQUENCE = [903, 904, 905]
VOCAB = 910


class FakeTokenizer:
    eot, sot_prev, no_timestamps, sot_sequence = EOT, SOT_PREV, NO_TS, SOT_SEQUENCE

    def __init__(self) -> None:
        self.ids: dict[str, int] = {}

    def encode(self, text: str) -> list[int]:
        return [self.ids.setdefault(ch, len(self.ids)) for ch in text]

    def decode(self, ids) -> str:
        chars = {v: k for k, v in self.ids.items()}
        return "".join(chars[i] for i in ids)


class FakeWhisper:
    """faster-whisper の WhisperModel のうち、採点に使う部分だけ。"""

    def __init__(self, tok: FakeTokenizer, truth: str, alternatives: list[str]) -> None:
        self.tok = tok
        self.truth = tok.encode(truth)
        self.alternatives = [tok.encode(a) for a in alternatives]
        self.model = SimpleNamespace(generate=self.generate)
        self.calls: list[dict] = []

    def feature_extractor(self, audio):
        return np.zeros((80, len(audio) // 160 + 1), dtype=np.float32)

    def encode(self, features):
        assert features.shape == (80, 3000)
        return np.zeros((1, 1500, 4), dtype=np.float32)

    def _logits(self, position: int) -> np.ndarray:
        row = np.zeros(VOCAB, dtype=np.float32)
        row[self.truth[position] if position < len(self.truth) else EOT] = 6.0
        return row

    def generate(self, features, prompts, **kw):
        self.calls.append({"batch": np.asarray(features).shape[0], "prompts": len(prompts), **kw})
        if not kw.get("return_logits_vocab"):
            assert prompts[0][-1] == NO_TS and kw["num_hypotheses"] == 5
            seqs = [self.truth, *self.alternatives][: kw["num_hypotheses"]]
            return [SimpleNamespace(sequences_ids=seqs, scores=[-0.1 * (i + 1) for i in range(len(seqs))],
                                    no_speech_prob=0.02, logits=[])]
        out = []
        for prompt in prompts:
            forced = prompt[prompt.index(NO_TS) + 1:]
            steps = [self._logits(i) for i in range(len(forced) + 1)]
            out.append(SimpleNamespace(sequences_ids=[forced], scores=[0.0], no_speech_prob=0.0, logits=[steps]))
        return out


def _write_wav(path: Path, seconds: float = 0.5, rate: int = 16000, channels: int = 1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    samples = (np.sin(np.linspace(0, 200, int(rate * seconds) * channels)) * 8000).astype(np.int16)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(samples.tobytes())


def _rescorer(truth: str = "ヘッズアップです", alternatives=("ヘッドアップです", "ヘッズアップ")):
    tok = FakeTokenizer()
    return ra.WhisperRescorer(FakeWhisper(tok, truth, list(alternatives)), tok, prompt="ポーカー"), tok


class TestSequenceLogprob:
    def test_sums_the_tokens_and_the_end(self):
        steps = [np.log([0.5, 0.25, 0.25]), np.log([0.1, 0.8, 0.1]), np.log([0.2, 0.2, 0.6])]
        assert ra.sequence_logprob(steps, [0, 1], eot=2) == pytest.approx(math.log(0.5 * 0.8 * 0.6))

    def test_raw_logits_are_normalised(self):
        steps = [np.array([2.0, 0.0]), np.array([0.0, 2.0])]
        p0 = math.exp(2) / (math.exp(2) + 1)
        assert ra.sequence_logprob(steps, [0], eot=1) == pytest.approx(2 * math.log(p0))

    def test_missing_step_after_the_text(self):
        assert ra.sequence_logprob([np.zeros(3)], [0], eot=2) is None


class TestRescore:
    def test_the_true_text_is_best(self):
        rescorer, _ = _rescorer()
        out = rescorer.rescore(np.zeros(16000, dtype=np.float32), "ここまでのヘッドゾップです")
        assert out["best"] == "ヘッズアップです"
        assert [a["text"] for a in out["nbest"]] == ["ヘッズアップです", "ヘッドアップです", "ヘッズアップ"]
        assert out["no_speech_prob"] == 0.02
        by_text = {row["text"]: row for row in out["scored"]}
        assert by_text["ここまでのヘッドゾップです"]["from"] == ["heard"]
        assert by_text["ここまでのヘッズアップです"]["from"] == ["fuzzy"]      # 音の近さの読み替え
        assert by_text["ヘッズアップ"]["from"] == ["nbest", "word"]
        assert by_text["コール"]["from"] == ["word"]
        # 正解の途中で終わる文（ヘッズアップ）は「終わり」の確率で下がる
        assert by_text["ヘッズアップです"]["logprob"] > by_text["ヘッズアップ"]["logprob"]
        assert [row["logprob"] for row in out["scored"]] == sorted((r["logprob"] for r in out["scored"]), reverse=True)

    def test_candidates_are_scored_in_one_batch_with_the_prompt(self):
        rescorer, tok = _rescorer()
        rescorer.rescore(np.zeros(16000, dtype=np.float32), "コール")
        nbest_call, score_call = rescorer.model.calls
        assert nbest_call["beam_size"] == 5
        assert score_call["batch"] == score_call["prompts"] > 5          # エンコーダの出力を候補の数だけ並べる
        assert score_call["suppress_blank"] is False and score_call["suppress_tokens"] == []
        assert rescorer.start == [SOT_PREV, *tok.encode(" ポーカー"), *SOT_SEQUENCE, NO_TS]   # ライブと同じプロンプト

    def test_without_prompt(self):
        tok = FakeTokenizer()
        rescorer = ra.WhisperRescorer(FakeWhisper(tok, "コール", []), tok, prompt=None)
        assert rescorer.start == [*SOT_SEQUENCE, NO_TS]


class TestSession:
    def _session(self, tmp_path: Path, rows: list[dict]) -> ra.SessionAudio:
        (tmp_path / "s1.transcripts.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        (session,) = ra.find_sessions(tmp_path)
        return session

    def test_rescores_each_utterance_once(self, tmp_path):
        _write_wav(tmp_path / "audio" / "s1" / "1000.wav")
        _write_wav(tmp_path / "audio" / "s1" / "2000.wav")
        session = self._session(tmp_path, [
            {"utterance_start_ts": 1.0, "heard_at": 2.0, "text": "コール", "audio_file": "1000.wav"},
            {"utterance_start_ts": 3.0, "heard_at": 4.0, "text": "", "audio_file": "1500.wav", "no_speech": True},
            {"utterance_start_ts": 5.0, "heard_at": 6.0, "text": "ヘッドアップです", "audio_file": "2000.wav"},
            {"utterance_start_ts": 7.0, "heard_at": 8.0, "text": "チェック", "audio_file": "3000.wav"},
            {"utterance_start_ts": 9.0, "heard_at": 9.5, "text": "打った文"},             # 音声の無い行
        ])
        rescorer, _ = _rescorer()
        lines: list[str] = []
        assert ra.rescore_session(session, rescorer, limit=1, log=lines.append) == (1, 0)
        assert ra.rescore_session(session, rescorer, log=lines.append) == (1, 0)    # 続きから
        rows = ra.read_jsonl(session.rescored)
        assert [r["audio_file"] for r in rows] == ["1000.wav", "2000.wav"]          # 音声の無い発話は飛ばす
        assert rows[0]["best"] == "ヘッズアップです" and rows[0]["utterance_start_ts"] == 1.0
        assert "  音声の無い発話 1 個は飛ばします" in lines
        assert ra.rescore_session(session, rescorer, log=lines.append) == (0, 0)    # 採点済み

    def test_zip_layout(self, tmp_path):
        folder = tmp_path / "s1"
        _write_wav(folder / "audio" / "1000.wav")
        (folder / "s1.transcripts.jsonl").write_text(
            json.dumps({"utterance_start_ts": 1.0, "text": "コール", "audio_file": "1000.wav"}) + "\n",
            encoding="utf-8")
        (session,) = ra.find_sessions(tmp_path)
        assert session.audio_path("1000.wav") == folder / "audio" / "1000.wav"
        assert session.rescored == folder / "s1.rescored.jsonl"

    def test_a_failing_utterance_does_not_stop_the_session(self, tmp_path):
        _write_wav(tmp_path / "audio" / "s1" / "1000.wav")
        _write_wav(tmp_path / "audio" / "s1" / "2000.wav")
        session = self._session(tmp_path, [
            {"utterance_start_ts": 1.0, "text": "コール", "audio_file": "1000.wav"},
            {"utterance_start_ts": 2.0, "text": "チェック", "audio_file": "2000.wav"},
        ])

        class Broken:
            def rescore(self, audio, heard):
                if heard == "コール":
                    raise RuntimeError("decode failed")
                return {"best": heard, "scored": [], "nbest": []}

        assert ra.rescore_session(session, Broken(), log=lambda _: None) == (2, 1)
        rows = ra.read_jsonl(session.rescored)
        assert rows[0]["error"] == "RuntimeError: decode failed" and rows[1]["best"] == "チェック"


class TestWav:
    def test_resamples_and_mixes_down(self, tmp_path):
        _write_wav(tmp_path / "a.wav", seconds=1.0, rate=8000, channels=2)
        audio = ra.load_wav(tmp_path / "a.wav")
        assert audio.dtype == np.float32 and len(audio) == 16000 and float(np.abs(audio).max()) <= 1.0


def test_candidates_include_action_words():
    texts = ra.candidate_texts("ペット600")
    assert texts["ペット600"] == ["heard"] and texts["ベット600"] == ["fuzzy"]
    assert all(texts[w] == ["word"] for w in ("コール", "チェック", "フォールド", "チェックアラウンド"))


def test_no_sessions(tmp_path, capsys):
    assert ra.main([str(tmp_path)]) == 1
    assert "transcripts.jsonl" in capsys.readouterr().out
