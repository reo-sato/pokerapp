"""短い語の分類器でコール / フォールドを読む（`second_ear.short_word_events`, 店舗 2026-10-26 の聞き取り）。

ディーラーが言っていたのに Whisper が雑談・幻聴・空に書き換えた語を、第 2 の耳の短い語の分類器（`audio/short_words.py`）が
確率 0.95 以上でコール / フォールドとしたときだけ読む（要確認 = `short_word`）。Whisper が幻聴か空に書いた発話だけ
（短い雑談の文にも使うと開発データで悪くなるハンドが出た）。
"""
from __future__ import annotations

import pytest

from audio import second_ear as se
from audio import short_words


class _Model:
    def __init__(self, probs):
        self.probs = probs

    def predict(self, ear):
        return dict(self.probs)


EAR = {"text": "これだ", "logp": -1.0, "candidates": []}


@pytest.fixture
def model(monkeypatch):
    def use(**probs):
        full = {"none": 0.0, "call": 0.0, "check": 0.0, "fold": 0.0, "amount": 0.0, **probs}
        monkeypatch.setattr(short_words, "load", lambda path=None: _Model(full))
    return use


def _read(text, ear=EAR):
    events, used = se.apply_ear([], text, ear, utterance_start_ts=10.0)
    return [(e.action, e.parse_flags) for e in events], used


@pytest.mark.parametrize("text", ["ご視聴ありがとうございました。", "",
                                  "シート3 レイズ 2千、コール、チョップ などの言葉が含まれます。"])
def test_a_hallucination_or_an_empty_text_is_read_by_the_classifier(model, text):
    model(fold=0.97, none=0.03)
    assert _read(text) == ([("fold", ("short_word",))], "フォールド")


def test_the_classifier_must_be_sure(model):
    model(call=0.93, none=0.07)
    assert _read("ご視聴ありがとうございました。") == ([], None)


@pytest.mark.parametrize("text", ["これだ。", "仕事楽しそうっすね、それ。まあ、お遊びっすよ。"])
def test_a_text_whisper_wrote_is_not_read(model, text):
    model(fold=0.98, none=0.02)
    assert _read(text) == ([], None)


def test_only_calls_and_folds(model):
    model(check=0.99, none=0.01)
    assert _read("ご視聴ありがとうございました。") == ([], None)


def test_without_the_model_nothing_changes(monkeypatch):
    monkeypatch.setattr(short_words, "load", lambda path=None: None)
    assert _read("ご視聴ありがとうございました。") == ([], None)


def test_a_reading_by_whisper_is_kept(model):
    model(fold=0.99, none=0.01)
    events, _used = se.apply_ear(se_parse("コール"), "コール", EAR, utterance_start_ts=10.0)
    assert [e.action for e in events] == ["call"]


def se_parse(text):
    from audio.recognizer import parse_actions

    return parse_actions(text, utterance_start_ts=10.0)
