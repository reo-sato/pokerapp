"""tests/test_garbled_amount.py

意味のない単発の語を額と読む（オーナー 2026-10-01「会話として意味のない単語（ゼニューク）などを単発で宣言したとき、
額の発声を疑うようにできますか」）:

- Whisper は額の読み（セン・ヒャク・テン）を崩して仮名の語にする（店舗「センニハク」= 1200・「よっしゃんてん」= 4000・
  「ゼニューク」= 1200）。発話全体が仮名だけの 1 語で、アクションとして読めず、笑い声・繰り返しでないときだけ額を疑う。
- 第 2 の耳があれば、その額の候補（確からしさが自由に聞いた文に近いもの）を音の近さと合わせて並べる。無ければ音の近さだけ
  （距離 0.4 以下で、アクションの語のほうが近くないとき）。
- engine は候補のうち、いまの手番でベット・レイズに使える最初の額にする（要確認 `phonetic_amount`）。使える額が無ければ
  記録しない。はっきり聞こえた額の言い直しにはしない。
"""
from __future__ import annotations

import json
import queue
import threading
from pathlib import Path

import pytest

from audio import second_ear as se
from audio.recognizer import garbled_word, parse_garbled_amount, phonetic_amount_event
from audio.recorder import AudioThread, Transcript
from integration.world_replay import read_utterance
from output.event_recorder import event_to_envelope
from integration.replay import event_from_envelope


def _ear(free: str, logp: float, cands: list[tuple[str, float]]) -> dict:
    return se.EarResult(free, logp, cands).to_dict()


# 店舗 2026-10-01 b0a27270 ハンド 2: 第 2 の耳は何も聞かず、額の候補は 三百・千二百・二百 …（正解は 1200）
ZENYUKU = _ear("", -3.539, [("三百", -4.018), ("千二百", -4.216), ("二百", -5.123), ("千六百", -5.29),
                            ("千百", -5.989), ("百", -6.953), ("千五百", -7.808), ("千三百", -8.151)])
# 店舗 2026-10-01 42f7b964 ハンド 2: 自由に聞いた文は「先天」、候補は 千点・四千点・二千点（正解は 4000）
YOSSHANTEN = _ear("先天", -3.646, [("千点", -1.944), ("四千点", -3.306), ("二千点", -4.147)])
# 意味のある語: 自由に聞いた文がその語で、額の候補はずっと低い
NICE = _ear("ナイス", -1.2, [("百", -12.5), ("千", -13.1)])


class TestGarbledWord:
    @pytest.mark.parametrize("text, word", [
        ("センニハク", "センニハク"), ("にせんたん", "ニセンタン"), ("ゼニューク", "ゼニューク"),
        ("センペンです。", "センペン"), ("よっしゃんてん", "ヨッシャンテン"),
    ])
    def test_a_single_kana_word(self, text, word):
        assert garbled_word(text) == word

    @pytest.mark.parametrize("text", [
        "はい", "でん",                                  # 短すぎる（額と区別できない）
        "ハハハハ", "アハハハ", "ハッハッハッハ", "ごめんごめん",   # 笑い声・繰り返し
        "千二百", "1200", "コール600", "よろしくお願いします", "",
    ])
    def test_not_a_single_kana_word(self, text):
        assert garbled_word(text) is None


class TestBySoundOnly:
    """第 2 の耳の無いとき（音の近さだけ）。"""

    @pytest.mark.parametrize("text, amount", [
        ("センニハク", 1200), ("にせんたん", 2000), ("さんびょくてん", 300), ("センペンです", 1000),
        ("どっぺくてん", 600), ("はぴょく", 800), ("ロックセンテン", 6000), ("センサンビュアック", 1300),
        ("ローク", 600),
    ])
    def test_an_amount_said_out_of_shape(self, text, amount):
        event = parse_garbled_amount(text, confidence=0.4, utterance_start_ts=3.0)
        assert (event.action, event.amount, event.amount_options) == ("bet", amount, (amount,))
        assert event.parse_flags == ("amount_only", "phonetic_amount")
        assert event.raw_text == text and event.confidence == 0.4 and event.utterance_start_ts == 3.0

    @pytest.mark.parametrize("text, options", [("よっしゃんてん", (40000, 4000)), ("ニモン", (20000, 2000))])
    def test_close_amounts_are_kept_for_the_engine(self, text, options):
        assert parse_garbled_amount(text).amount_options == options

    @pytest.mark.parametrize("text", [
        "ゼニューク",                                   # 音だけでは遠い（第 2 の耳が要る）
        "はいっ", "ナイス", "ナイスです", "これぞ", "ラストカード", "アクションです", "よっせー", "オッケー",
        "どうぞ", "すみません", "ありがとうございます",       # 意味のある語
        "ハハハハ", "ごめんごめん",                      # 笑い声・繰り返し
        "コール", "600", "ポット1200です", "センニハク？",  # 読める発話・ポットの読み上げ・確認の問い
    ])
    def test_not_read(self, text):
        assert parse_garbled_amount(text) is None


class TestWithTheSecondEar:
    def test_zenyuku_is_1200(self):
        """b0a27270 ハンド 2: いちばん確からしい 三百 は音が遠い。音の近さを合わせると 千二百。"""
        (event,), used = se.apply_ear([], "ゼニューク", ZENYUKU, utterance_start_ts=3.0)
        assert (event.action, event.amount, event.amount_options) == ("bet", 1200, (1200, 1600, 1100))
        assert event.parse_flags == ("amount_only", "phonetic_amount", se.EAR_FLAG)
        assert event.confidence == se.EAR_CONFIDENCE and event.utterance_start_ts == 3.0
        assert used == "千二百"

    def test_yosshanten_is_4000(self):
        """42f7b964 ハンド 2: いちばん確からしい 千点 より、音の近い 四千点。"""
        (event,), used = se.apply_ear([], "よっしゃんてん", YOSSHANTEN)
        assert event.amount_options == (4000, 1000, 2000) and used == "四千点"

    def test_a_meaningful_word_is_not_read(self):
        assert se.apply_ear([], "ナイス", NICE) == ([], None)

    def test_the_second_ear_decides(self):
        """第 2 の耳が額を聞いていなければ、音だけでは読まない（第 2 の耳のほうが確か）。"""
        assert se.apply_ear([], "センニハク", NICE) == ([], None)

    def test_without_the_second_ear_sound_alone(self):
        (event,), used = se.apply_ear([], "センニハク", None, confidence=0.4)
        assert event.amount == 1200 and se.EAR_FLAG not in event.parse_flags and used is None

    def test_a_question_is_not_read(self):
        assert se.apply_ear([], "センニハク？", None, question=True) == ([], None)

    def test_a_rescue_comes_first(self):
        """第 2 の耳の自由に聞いた文と候補が合えば、従来どおりその読み（額の音の近さは使わない）。"""
        six = _ear("六百", -1.0, [("六百", -1.0), ("六百点", -2.5)])
        (event,), used = se.apply_ear([], "のっぴょく", six)
        assert event.amount == 600 and "phonetic_amount" not in event.parse_flags and used == "六百"


class TestRecord:
    def test_the_options_are_recorded_and_replayed(self):
        jsonschema = pytest.importorskip("jsonschema")
        schema = json.loads((Path(__file__).resolve().parents[1]
                             / "docs/contracts/schemas/reconstruction_event.schema.json").read_text(encoding="utf-8"))
        (event,), _ = se.apply_ear([], "ゼニューク", ZENYUKU, utterance_start_ts=3.0)
        envelope = event_to_envelope(event)
        jsonschema.validate(envelope, schema)
        assert envelope["amount_options"] == [1200, 1600, 1100]
        back = event_from_envelope(json.loads(json.dumps(envelope)))
        assert back.amount_options == (1200, 1600, 1100) and back.parse_flags == event.parse_flags

    def test_an_ordinary_event_has_no_options(self):
        from audio.recognizer import parse_actions

        (event,) = parse_actions("レイズ 600")
        assert "amount_options" not in event_to_envelope(event)

    def test_reading_the_record_again(self):
        """推定器・評価は書き起こしをいまの規則で読み直す（第 2 の耳の無い行も音の近さで）。"""
        rows = [{"utterance_start_ts": 1.0, "text": "センニハク", "confidence": 0.4},
                {"utterance_start_ts": 2.0, "text": "ゼニューク", "ear": ZENYUKU},
                {"utterance_start_ts": 3.0, "text": "ゼニューク"},
                {"utterance_start_ts": 4.0, "text": "ナイス"}]
        assert [[e.amount for e in read_utterance(row)] for row in rows] == [[1200], [1200], [], []]
        assert read_utterance(rows[0], text="コール")[0].action == "call"    # 文を置き換えた読み直しには重ねない

    def test_the_v0_estimator_takes_it_as_the_default(self):
        from tools.estimate import utterance_options

        first = utterance_options({"utterance_start_ts": 1.0, "text": "センニハク", "confidence": 0.4})[0]
        assert first.source == "rescue" and first.keys == (("bet", 1200, None, None),)


class FakeWhisper:
    ready = True

    def __init__(self, text: str) -> None:
        self.text = text

    def transcribe_with_confidence(self, audio_bytes: bytes):
        return self.text, 0.3


class FakeEar:
    def __init__(self, result: dict) -> None:
        self.result = result

    def hear(self, samples):
        return se.EarResult(self.result["text"], self.result["logp"],
                            [(c["text"], c["logp"]) for c in self.result["candidates"]])


def _run(text: str, ear) -> tuple[list, list[Transcript]]:
    audio_q: queue.Queue = queue.Queue()
    seen: list[Transcript] = []
    thread = AudioThread(audio_queue=audio_q, stop_event=threading.Event(), transcriber=FakeWhisper(text),
                         on_transcript=seen.append, second_ear=ear)
    thread._process_chunk(b"\x10\x00" * 1600, utterance_start_ts=5.0)   # noqa: SLF001
    events = []
    while not audio_q.empty():
        events.append(audio_q.get_nowait())
    return events, seen


class TestLive:
    def test_without_the_second_ear(self, capsys):
        import main

        events, (transcript,) = _run("センニハク", None)
        assert [(e.action, e.amount) for e in events] == [("bet", 1200)]
        assert events[0].parse_flags == ("amount_only", "phonetic_amount") and transcript.ear_text is None
        main._print_transcript(transcript)                                   # noqa: SLF001
        assert "（数字だけ・意味のない語を額の音で読んだ）" in capsys.readouterr().out

    def test_with_the_second_ear(self):
        events, (transcript,) = _run("ゼニューク", FakeEar(ZENYUKU))
        assert [(e.action, e.amount) for e in events] == [("bet", 1200)]
        assert transcript.ear_text == "千二百" and transcript.events == tuple(events)

    def test_a_meaningful_word(self):
        events, (transcript,) = _run("ナイス", FakeEar(NICE))
        assert events == [] and transcript.ear_text is None


# ――― engine ―――

pokerkit = pytest.importorskip("pokerkit")

from tests.test_rfid_folds import _Table  # noqa: E402
from tests.test_store_2026_09_27 import STORE_HOLES  # noqa: E402
from tests.test_store_2026_09_29 import _acts, _replayed_open  # noqa: E402
from tests.test_store_2026_10_01 import _rebuilt  # noqa: E402


def _hear(tb: _Table, text: str, ear: dict | None = None) -> None:
    """ライブと同じ読み（第 2 の耳の結果があれば重ねる）で engine に入れる。"""
    events, _ = se.apply_ear([], text, ear, utterance_start_ts=tb.now, confidence=0.4)
    for event in events:
        event.timestamp = tb.now
        tb.recorder.record(event)
        tb.t._handle_audio_event(event)    # noqa: SLF001


def _table(tmp_path, *said: tuple[str, float]) -> _Table:
    """ボタン 席6（最初の手番）/ SB 席4 / BB 席5。持ち点はどの席も 10000、ブラインド 100/200。"""
    tb = _Table(tmp_path)
    tb.deal(STORE_HOLES)
    for text, wait in said:
        tb.say(text)
        tb.tick(tb.now + wait)
    return tb


class TestEngine:
    def test_the_usable_amount_is_taken(self, tmp_path):
        """「よっしゃんてん」の候補 4 万・4000 のうち、持ち点 10000 で使えるのは 4000。"""
        tb = _table(tmp_path)
        _hear(tb, "よっしゃんてん")
        assert _acts(tb) == [(6, "raise", 4000)]
        record = tb.t._current_actions[0]     # noqa: SLF001
        assert record.needs_review and "phonetic_amount" in record.reason.split("+")
        assert "ambiguous_amount" not in record.reason.split("+") and record.raw_text == "よっしゃんてん"

    def test_a_raise_after_a_raise(self, tmp_path):
        tb = _table(tmp_path, ("600", 1.0))
        _hear(tb, "センニハク")
        assert _acts(tb) == [(6, "raise", 600), (4, "raise", 1200)]

    def test_with_the_second_ear(self, tmp_path):
        tb = _table(tmp_path, ("600", 1.0))
        _hear(tb, "ゼニューク", ZENYUKU)
        assert _acts(tb) == [(6, "raise", 600), (4, "raise", 1200)]
        reasons = tb.t._current_actions[1].reason.split("+")    # noqa: SLF001
        assert {"phonetic_amount", "second_ear", "ambiguous_amount"} <= set(reasons)   # 1600・1100 も使える額

    def test_it_never_restates_a_clear_amount(self, tmp_path):
        """はっきり聞こえた 1800 のあとの「センニハク」(1200) は、言い直しにも次の人のレイズにもならない。"""
        tb = _table(tmp_path, ("600", 1.0), ("1800", 1.0))
        _hear(tb, "センニハク")
        assert _acts(tb) == [(6, "raise", 600), (4, "raise", 1800)]
        assert any("センニハク" in n and "使えない額" in n for n in tb.notices)
        assert not any("言い直し" in n for n in tb.notices)

    def test_the_first_usable_option(self, tmp_path):
        tb = _table(tmp_path)
        event = phonetic_amount_event("テスト", (50000, 300, 1300), 0.4, tb.now)
        event.timestamp = tb.now
        tb.t._handle_audio_event(event)    # noqa: SLF001
        # 持ち点を超える 5 万・最小レイズ 400 に満たない 300 は使えない → 1300（使える候補は 1 つ = 曖昧ではない）
        assert _acts(tb) == [(6, "raise", 1300)]
        assert "ambiguous_amount" not in tb.t._current_actions[0].reason.split("+")   # noqa: SLF001

    def test_replay_and_rebuild_give_the_same_record(self, tmp_path):
        tb = _table(tmp_path, ("600", 1.0))
        _hear(tb, "ゼニューク", ZENYUKU)
        tb.tick(tb.now + 1.0)
        tb.say("コール")
        live = _acts(tb)
        assert live == [(6, "raise", 600), (4, "raise", 1200), (5, "call", 1000)]
        (hand,) = _replayed_open(tb, tmp_path)
        assert [(a.seat, a.action, a.amount) for a in hand.actions][:len(live)] == live
        assert _rebuilt(tb) == live
