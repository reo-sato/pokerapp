"""tests/test_second_ear.py — 第 2 の耳（audio/second_ear.py）。本物のモデル（約 170 MB）は使わない。"""
from __future__ import annotations

import io
import math
import tarfile
from pathlib import Path

import numpy as np
import pytest

from audio import second_ear as se
from audio.recognizer import parse_actions

VOCAB = ["<blk>", "コ", "ー", "ル", "チ", "ェ", "ッ", "ク", "フ", "ォ", "ド", "百", "千", "二", "六"]


class FakeModel:
    """小さな乱数の RNN-T（joiner = tanh(enc + dec) @ W）。"""

    def __init__(self, frames: int = 5, seed: int = 0, dim: int = 6) -> None:
        rng = np.random.default_rng(seed)
        self.tokens = list(VOCAB)
        self.context_size = 2
        self._enc = rng.standard_normal((frames, dim)).astype(np.float64)
        self._emb = rng.standard_normal((len(VOCAB), dim)).astype(np.float64)
        self._w = rng.standard_normal((dim, len(VOCAB))).astype(np.float64)

    def encode(self, samples: np.ndarray) -> np.ndarray:
        return self._enc

    def decoder_out(self, contexts: np.ndarray) -> np.ndarray:
        return self._emb[np.asarray(contexts)].sum(axis=1)

    def joiner_logits(self, enc: np.ndarray, dec: np.ndarray) -> np.ndarray:
        return np.tanh(enc + dec) @ self._w


def _ids(text: str) -> list[int]:
    return [VOCAB.index(ch) for ch in text]


def _brute_force(model: FakeModel, ids: list[int]) -> float:
    """全アラインメントを数え上げた log P(ids | 音声)（前向きアルゴリズムの確かめ）。"""
    enc = model.encode(np.zeros(1))
    frames = len(enc)

    def logprobs(context: tuple[int, ...], t: int) -> np.ndarray:
        logits = model.joiner_logits(enc[t:t + 1], model.decoder_out(np.array([context])))[0]
        return logits - np.log(np.exp(logits).sum())

    contexts = [(0, 0)]
    for token in ids:
        contexts.append((contexts[-1] + (token,))[-2:])
    total = []

    def walk(t: int, u: int, acc: float) -> None:
        lp = logprobs(contexts[u], t)
        if u < len(ids):
            walk(t, u + 1, acc + lp[ids[u]])            # 文字を出す（同じフレームのまま）
        if t < frames - 1:
            walk(t + 1, u, acc + lp[0])                  # blank で次のフレームへ
        elif u == len(ids):
            total.append(acc + lp[0])                    # 最後の blank で終わる

    walk(0, 0, 0.0)
    return float(np.logaddexp.reduce(total))


class TestForward:
    def test_the_score_is_the_sum_over_all_alignments(self):
        model = FakeModel(frames=3)
        heard = se.Heard(model, model.encode(np.zeros(1)))
        for text in ("コ", "コー", "コール", ""):
            assert heard.score(_ids(text), 2) == pytest.approx(_brute_force(model, _ids(text)), abs=1e-5)   # float32

    def test_the_trie_gives_the_same_scores_as_one_by_one(self, monkeypatch):
        monkeypatch.setattr(se, "_JOINER_ROWS", 7)             # joiner を何回かに分けても同じ
        model = FakeModel(frames=6, seed=3)
        candidates = [se.Candidate(s, s) for s in ("コール", "コールコール", "チェック", "フォールド", "百", "二百",
                                                   "六百", "千", "千六百", "二千")]
        trie = se.CandidateTrie(candidates, model.tokens, 2)
        heard = se.Heard(model, model.encode(np.zeros(1)))
        scores = heard.score_trie(trie)
        for candidate, score in zip(trie.candidates, scores):
            assert score == pytest.approx(heard.score(_ids(candidate.spoken), 2), abs=1e-9)
        # 同じ出だしは分け合う（「コール」と「コールコール」、「百」で終わる額）
        assert len(trie.nodes) < 1 + sum(len(c.spoken) for c in candidates)

    def test_letters_the_model_does_not_have_are_skipped(self):
        model = FakeModel()
        trie = se.CandidateTrie([se.Candidate("コール", "コール"), se.Candidate("レイズ", "レイズ")], model.tokens, 2)
        assert [c.text for c in trie.candidates] == ["コール"]


class TestGreedy:
    def test_one_letter_per_frame(self):
        model = FakeModel(frames=4)
        targets = _ids("コー") + [0] + _ids("ル")           # フレームごとに出す文字（0 = blank）
        eye = np.eye(len(VOCAB))
        model._enc = np.stack([eye[t] * 50.0 for t in targets])
        model._emb = np.zeros((len(VOCAB), len(VOCAB)))
        model._w = np.eye(len(VOCAB))
        heard = se.Heard(model, model.encode(np.zeros(1)))
        assert heard.greedy(2) == _ids("コール")

    def test_hear_reports_the_text_and_the_best_candidates(self):
        model = FakeModel(frames=5, seed=1)
        ear = se.SecondEar(model, [se.Candidate(s, s + "!") for s in ("コール", "チェック", "フォールド")])
        result = ear.hear(np.zeros(16000), top=2)
        assert len(result.candidates) == 2 and all(t.endswith("!") for t, _ in result.candidates)
        assert result.candidates[0][1] >= result.candidates[1][1]
        assert set(result.text) <= set("".join(VOCAB[1:]))
        row = result.to_dict()
        assert set(row) == {"text", "logp", "candidates", "words", "frames"} and row["frames"] == 5
        assert row["candidates"][0]["text"].endswith("!")

    def test_short_words_are_scored_on_their_own(self):
        """短い語（`SHORT_WORDS`）は全部の発話で採点して記録する（2026-10-07）。読み取りの候補とは別の木なので、
        候補の上位・額の表は変わらない。モデルの文字に無い語は採点しない。"""
        model = FakeModel(frames=5, seed=2)
        candidates = [se.Candidate(s, s) for s in ("コール", "チェック", "六百", "千")]
        plain = se.SecondEar(model, candidates, words=()).hear(np.zeros(16000))
        heard = se.SecondEar(model, candidates).hear(np.zeros(16000))
        assert plain.words == {} and "words" not in plain.to_dict()
        assert (heard.text, heard.logp, heard.candidates, heard.amounts) == (
            plain.text, plain.logp, plain.candidates, plain.amounts)
        written = [w for w in se.SHORT_WORDS if set(w) <= set(VOCAB[1:])]
        assert set(heard.words) == set(written) and {"コール", "コル", "コー", "チェック"} <= set(written)
        h = se.Heard(model, model.encode(np.zeros(1)))
        for word in written:
            assert heard.words[word] == pytest.approx(h.score(_ids(word), 2), abs=1e-6)
        assert heard.to_dict()["words"]["コール"] == round(heard.words["コール"], 3)

    def test_every_short_word_can_be_written_by_the_model(self):
        """短い語はどれも句読点・記号を含まない（モデルの文字に無いと黙って採点されない）。"""
        for word in se.SHORT_WORDS:
            assert word and not set(word) & set("、。！？～ ・")
        assert len(set(se.SHORT_WORDS)) == len(se.SHORT_WORDS)


class TestCandidates:
    @pytest.mark.parametrize("n, text", [(100, "百"), (600, "六百"), (1000, "千"), (1100, "千百"), (1400, "千四百"),
                                         (2500, "二千五百"), (10000, "一万"), (13800, "一万三千八百"),
                                         (25000, "二万五千")])
    def test_kanji_numbers_are_written_like_the_model(self, n, text):
        assert se.kanji_number(n) == text

    def test_every_candidate_reads_as_an_action(self):
        candidates = se.build_candidates()
        assert all(parse_actions(c.text) for c in candidates)
        by_text = {c.text: c for c in candidates}
        assert [(e.action, e.amount) for e in parse_actions(by_text["レイズ 二千五百"].text)] == [("raise", 2500)]
        assert [(e.action, e.amount) for e in parse_actions(by_text["二千、コール"].text)] == [("bet", 2000),
                                                                                             ("call", 0)]
        assert [e.action for e in parse_actions(by_text["フォールド、コール"].text)] == ["fold", "call"]
        assert by_text["フォールド、コール"].spoken == "フォールドコール"       # 音のモデルには句読点が無い


class TestDownload:
    def _archive(self, names: list[str]) -> bytes:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:bz2") as tar:
            for name in names:
                data = f"contents of {name}".encode()
                info = tarfile.TarInfo(f"sherpa-onnx-zipformer-ja/{name}")
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        return buffer.getvalue()

    def test_only_the_needed_files_are_taken(self, tmp_path):
        blob = self._archive(["README.md", "encoder-epoch-99-avg-1.onnx", *se.MODEL_FILES, "test_wavs/1.wav"])
        seen: list[int] = []
        se.download_model(tmp_path / "m", "http://example", report=seen.append,
                          opener=lambda url: io.BytesIO(blob))
        assert sorted(p.name for p in (tmp_path / "m").iterdir()) == sorted(se.MODEL_FILES)
        assert se.model_ready(tmp_path / "m") and seen and seen[-1] <= len(blob)
        assert (tmp_path / "m" / se.TOKENS).read_text() == "contents of tokens.txt"

    def test_a_missing_file_is_an_error(self, tmp_path):
        blob = self._archive([se.ENCODER, se.DECODER])
        with pytest.raises(RuntimeError, match="joiner"):
            se.download_model(tmp_path / "m", "http://example", opener=lambda url: io.BytesIO(blob))
        assert not se.model_ready(tmp_path / "m")


def test_tokens_file(tmp_path: Path):
    path = tmp_path / "tokens.txt"
    path.write_text("<blk> 0\n/ 1\nコ\t2\n 3\n", encoding="utf-8")
    assert se.read_tokens(path) == ["<blk>", "/", "コ", " "]


def test_the_log_softmax_columns_are_normalized():
    logits = np.array([[1.0, 2.0, 3.0], [0.0, 0.0, 0.0]])
    cols = se._log_softmax_columns(logits, np.arange(3))
    assert np.allclose(np.exp(cols).sum(axis=1), 1.0)
    assert cols[1, 0] == pytest.approx(-math.log(3))
