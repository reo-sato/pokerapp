"""tests/test_lower_digits.py

数の下の桁（オーナー 2026-10-02:「数字の後に、残りの桁などを発声できるタイミングで意味の通らない単語が入った際には、
読み直すことはできませんか」）と、第 2 の耳が何も書かなかった発話の候補:

- 「千」「万」のすぐあとに続けた片仮名の語: 仮名の数はその桁（「7千ロッピャク」= 7600）、意味の通らない語は前の数だけ
  + 要確認（「7千ドップ」= 7000 + `garbled_digits`）。チェックに付いて額が消えない（店舗 0f7705c8 ハンド 1
  「7千ドップチェック」は 7000 がチェックに付いて消え、チェックがコールに読み替えられていた）。
- 「千」「万」で終わる数の額は、第 2 の耳が下の桁まで聞いていればそれを使う（10/01 の真のアクション:
  「7千ドップチェック」= 7600・「2千、コール。」= 2500。第 2 の耳は「七千六百」「二千五百」とだけ聞いた）。
- 推定器 v1: 第 2 の耳が自由に聞いた文が空でも、その候補を選択肢にする（店舗 4c252c77 ハンド 2: SB のコールを
  Whisper は「コーナー」、第 2 の耳は空の文 −2.80・候補「コール」−4.73）。
"""
from __future__ import annotations

import queue
import threading

import numpy as np
import pytest

from audio import second_ear as se
from audio.recognizer import GARBLED_DIGITS, open_amount_range, parse_actions
from audio.recorder import AudioThread, Transcript
from integration.estimator import reading_params
from tools.estimate import PARAMS, utterance_options


def _read(text: str) -> list[tuple[str, int, bool]]:
    return [(e.action, e.amount, GARBLED_DIGITS in e.parse_flags) for e in parse_actions(text)]


class TestParse:
    @pytest.mark.parametrize("text, expected", [
        ("7千ドップチェック", [("bet", 7000, True), ("check", 0, False)]),   # 額がチェックに付いて消えない
        ("7千ドップ", [("bet", 7000, True)]),
        ("7千ドップ、チェック", [("bet", 7000, True), ("check", 0, False)]),
        ("レイズ7千ドップ", [("raise", 7000, True)]),                         # 語の額にも印を付け替える
        ("7千ロッピャク", [("bet", 7600, False)]),                            # 仮名の数はその桁
        ("七千ロッピャク", [("bet", 7600, False)]),
        ("レイズ7千ロッピャク", [("raise", 7600, False)]),
        ("1万ニセン", [("bet", 12000, False)]),
        ("1万2千ゴヒャク", [("bet", 12500, False)]),
    ])
    def test_a_word_after_a_thousand(self, text, expected):
        assert _read(text) == expected

    @pytest.mark.parametrize("text, expected", [
        ("2千になっちゃってて", []),                                          # ひらがなは言葉の続き
        ("千チップ", []),                                                     # 卓の用語そのもの
        ("5千オーバー", []),
        ("千テン", [("bet", 1000, False)]),                                   # 額の言い方に付く語
        ("2千ポイント", [("bet", 2000, False)]),
        ("4千点", [("bet", 4000, False)]),
        ("千ヘッドホップ", [("bet", 1000, False), ("heads_up", 0, False)]),   # 音でアクションの語に読める語
        ("7600", [("bet", 7600, False)]),
    ])
    def test_other_words_are_read_as_before(self, text, expected):
        assert _read(text) == expected

    @pytest.mark.parametrize("text, amount, expected", [
        ("7千ドップ", 7000, (7000, 8000)),
        ("2千、コール。", 2000, (2000, 3000)),
        ("4千点", 4000, (4000, 5000)),
        ("1万", 10000, (10000, 20000)),
        ("1万2千", 12000, (12000, 13000)),
        ("レイズ4千点", 4000, (4000, 5000)),
        ("7600", 7600, None),                 # 下の桁まで言った
        ("2千5百", 2500, None),
        ("2,000", 2000, None),                # 千・万の字の無い数
        ("4千点", 3000, None),                # 違う額
    ])
    def test_open_amount_range(self, text, amount, expected):
        assert open_amount_range(text, amount) == expected


SEVEN_SIX = se.EarResult("七千六百", -0.08, [("七千六百", -0.08), ("六千六百", -4.1)],
                         amounts=[(7600, -0.08), (6600, -4.1)])
TWO_FIVE = se.EarResult("二千五百", -0.3, [("二千五百", -0.3), ("千五百", -5.4)])
TWO_THOUSAND = se.EarResult("二千点", -0.05, [("二千点", -0.05), ("千点", -6.9)])
TWO_THOUSAND_CALL = se.EarResult("二千コール", -0.4, [("二千、コール", -0.6), ("二千", -4.0)])
SIX_SIX = se.EarResult("六千六百", -0.1, [("六千六百", -0.1), ("七千六百", -3.0)])
SEVEN_SIX_MORE = se.EarResult("七千六百チェック", -1.0, [("七千六百", -1.2), ("六千六百", -5.0)])


def _apply(text: str, ear: se.EarResult | None) -> tuple[list[tuple[str, int, tuple]], str | None]:
    used, ear_text = se.apply_ear(parse_actions(text), text, ear.to_dict() if ear else None)
    return [(e.action, e.amount, e.parse_flags) for e in used], ear_text


class TestSecondEar:
    def test_the_ear_fills_the_lower_digits(self):
        """崩れた語に続けた「チェック」も下の桁の聞き違い（第 2 の耳は「七千六百」とだけ聞いた）。"""
        read, used = _apply("7千ドップチェック", SEVEN_SIX)
        assert read == [("bet", 7600, ("amount_only", "second_ear"))] and used == "七千六百"

    def test_an_action_word_after_the_number_was_the_lower_digits(self):
        """店舗 1709932e ハンド 3: ターンの 2500 のベットを Whisper は「2千、コール。」と書いた。"""
        read, used = _apply("2千、コール。", TWO_FIVE)
        assert read == [("bet", 2500, ("amount_only", "second_ear"))] and used == "二千五百"

    @pytest.mark.parametrize("text, ear", [
        ("2千点", TWO_THOUSAND),                 # 同じ額
        ("2千、コール。", TWO_THOUSAND_CALL),     # 2000 のベットのあとのコール（第 2 の耳も「コール」を聞いた）
        ("7600", SEVEN_SIX),                     # 下の桁まで言った
    ])
    def test_the_same_number_changes_nothing(self, text, ear):
        before = [(e.action, e.amount, e.parse_flags) for e in parse_actions(text)]
        assert _apply(text, ear) == (before, None)

    def test_other_upper_digits_are_not_used(self):
        read, used = _apply("7千ドップチェック", SIX_SIX)
        assert read == [("bet", 7000, ("amount_only", GARBLED_DIGITS)), ("check", 0, ())] and used is None

    def test_only_the_amount_when_the_ear_heard_more(self):
        """第 2 の耳が自由に聞いた文に額のほかの語もあれば、額だけを入れる（Whisper のほかの語は残す）。"""
        read, used = _apply("7千ドップチェック", SEVEN_SIX_MORE)
        assert read == [("bet", 7600, ("amount_only", "second_ear")), ("check", 0, ())] and used == "七千六百"

    def test_without_the_ear_the_number_is_kept_for_review(self):
        read, used = _apply("7千ドップチェック", None)
        assert read == [("bet", 7000, ("amount_only", GARBLED_DIGITS)), ("check", 0, ())] and used is None

    @pytest.mark.parametrize("text, wanted", [
        ("2千点", True), ("7千ドップチェック", True), ("レイズ1万", True),
        ("7600", False), ("2千5百", False), ("コール", False),
    ])
    def test_which_utterances_are_heard_again(self, text, wanted):
        assert se.wants_ear(parse_actions(text), text) is wanted


class _Whisper:
    ready = True

    def __init__(self, text: str) -> None:
        self.text = text

    def transcribe_with_confidence(self, audio_bytes: bytes):
        return self.text, 0.4


class _Ear:
    def __init__(self, result: se.EarResult) -> None:
        self.result = result
        self.heard: list[np.ndarray] = []

    def hear(self, samples: np.ndarray) -> se.EarResult:
        self.heard.append(samples)
        return self.result


def test_the_live_recorder_uses_the_lower_digits():
    audio_q: queue.Queue = queue.Queue()
    seen: list[Transcript] = []
    ear = _Ear(SEVEN_SIX)
    thread = AudioThread(audio_queue=audio_q, stop_event=threading.Event(), transcriber=_Whisper("7千ドップチェック"),
                         on_transcript=seen.append, second_ear=ear)
    thread._process_chunk(b"\x10\x00" * 1600, utterance_start_ts=5.0)   # noqa: SLF001
    events = []
    while not audio_q.empty():
        events.append(audio_q.get_nowait())
    assert [(e.action, e.amount) for e in events] == [("bet", 7600)] and len(ear.heard) == 1
    assert seen[0].ear_text == "七千六百" and events[0].amount_scores[0] == (7600, -0.08)


class TestEstimatorOptions:
    CORNER = {"utterance_start_ts": 10.0, "heard_at": 11.0, "text": "コーナー", "confidence": 0.28,
              "ear": {"text": "", "logp": -2.799,
                      "candidates": [{"text": "コール", "logp": -4.728}, {"text": "コールです", "logp": -8.676},
                                     {"text": "二千", "logp": -11.563}]}}

    def test_v0_does_not_use_the_candidates_of_an_empty_ear_text(self):
        assert [o.source for o in utterance_options(self.CORNER, PARAMS)] == ["drop"]

    def test_v1_uses_them(self):
        options = utterance_options(self.CORNER, reading_params())
        assert [(o.source, o.keys) for o in options] == [("drop", ()), ("ear", (("call", 0, None, None),))]
        diff = -4.728 - -2.799
        assert options[1].logp == pytest.approx(PARAMS["ear_base"] + PARAMS["ear_diff_weight"] * diff)
        assert reading_params()["ear_empty_text"] == 1.0 and PARAMS["ear_empty_text"] == 0.0

    def test_a_reading_with_garbled_digits_is_uncertain(self):
        row = {"utterance_start_ts": 10.0, "heard_at": 11.0, "text": "7千ドップチェック", "confidence": 0.4}
        options = utterance_options(row, reading_params())
        assert options[0].source == "whisper" and options[0].logp == PARAMS["fuzzy"]
