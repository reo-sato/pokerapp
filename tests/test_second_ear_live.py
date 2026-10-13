"""tests/test_second_ear_live.py

ライブの聞き直し（2026-09-29, オーナー了承）: Whisper がアクションとして読めなかった発話（幻聴・読めない文）だけを
第 2 の耳で聞き直し、第 2 の耳が自由に聞いた文そのものが候補と同じアクションに読めるときだけ、その候補を使う。
本物のモデルは使わない（決まった結果を返す偽物）。
"""
from __future__ import annotations

import json
import queue
import sys
import threading
import types

import numpy as np
import pytest

from audio import second_ear as se
from audio.recorder import AudioThread, Transcript
from output.transcript_log import TranscriptLog
from tools import eval_store


class FakeWhisper:
    ready = True

    def __init__(self, text: str) -> None:
        self.text = text

    def transcribe_with_confidence(self, audio_bytes: bytes):
        return self.text, 0.3


class FakeEar:
    def __init__(self, result: se.EarResult | None = None, fail: bool = False) -> None:
        self.result = result
        self.fail = fail
        self.heard: list[np.ndarray] = []

    def hear(self, samples: np.ndarray) -> se.EarResult:
        self.heard.append(samples)
        if self.fail:
            raise RuntimeError("onnx failed")
        return self.result


SIX_HUNDRED = se.EarResult("六百", -1.0, [("六百", -1.0), ("六百点", -2.5), ("百", -9.0)])
CHATTER = se.EarResult("お願いしま", -3.0, [("千", -3.4), ("百", -3.9)])


def _run(text: str, ear: FakeEar | None) -> tuple[list, list[Transcript]]:
    audio_q: queue.Queue = queue.Queue()
    seen: list[Transcript] = []
    thread = AudioThread(audio_queue=audio_q, stop_event=threading.Event(), transcriber=FakeWhisper(text),
                         on_transcript=seen.append, second_ear=ear)
    thread._process_chunk(b"\x10\x00" * 1600, utterance_start_ts=5.0)   # noqa: SLF001
    events = []
    while not audio_q.empty():
        events.append(audio_q.get_nowait())
    return events, seen


class TestRecorder:
    @pytest.mark.parametrize("text", ["ご視聴ありがとうございました。", "のっぴょく"])
    def test_what_whisper_could_not_read_is_heard_again(self, text):
        ear = FakeEar(SIX_HUNDRED)
        events, (transcript,) = _run(text, ear)
        assert [(e.action, e.amount) for e in events] == [("bet", 600)]
        assert events[0].parse_flags == ("amount_only", "second_ear")          # 要確認
        assert events[0].confidence == se.EAR_CONFIDENCE and events[0].utterance_start_ts == 5.0
        assert transcript.text == text and transcript.ear_text == "六百" and transcript.events == tuple(events)
        assert transcript.ear["text"] == "六百" and transcript.ear["sec"] >= 0
        assert len(ear.heard[0]) == 1600 and ear.heard[0].dtype == np.float32

    @pytest.mark.parametrize("text, expected", [("コール", [("call", 0)]), ("コールですか？", [])])
    def test_what_whisper_read_is_heard_but_not_used(self, text, expected):
        """2026-10-07 から全発話を第 2 の耳で聞いて記録する。使うのは今までどおり Whisper が読めなかった発話・額のある
        発話だけ（`ear_wanted`）。"""
        ear = FakeEar(SIX_HUNDRED)
        events, (transcript,) = _run(text, ear)
        assert [(e.action, e.amount) for e in events] == expected
        assert all("second_ear" not in e.parse_flags and not e.amount_scores for e in events)
        assert len(ear.heard) == 1 and transcript.ear["text"] == "六百" and transcript.ear["sec"] >= 0
        assert transcript.ear_wanted is False and transcript.ear_text is None

    def test_what_the_second_ear_was_used_for_is_marked(self):
        _, (transcript,) = _run("のっぴょく", FakeEar(SIX_HUNDRED))
        assert transcript.ear_wanted is True
        scored = se.EarResult("ベット六百", -0.2, [("ベット 六百", -0.2)], amounts=[(600, -0.2), (100, -9.0)])
        _, (wager,) = _run("ベット 600", FakeEar(scored))                # 額ごとの点数を付ける発話
        assert wager.ear_wanted is True and wager.events[0].amount_scores == ((600, -0.2), (100, -9.0))

    def test_sounds_that_are_not_voices_are_heard_for_the_record(self):
        """声ではない音（VAD で Whisper にかけなかった音）も第 2 の耳で聞いて記録だけする（声の検出が落とした短い語を
        あとで数えるため）。アクションにはしない。"""
        class NoVoice:
            ready = True

            def recognize(self, audio_bytes):
                return types.SimpleNamespace(text="", confidence=None, no_speech=True)

        audio_q: queue.Queue = queue.Queue()
        seen: list[Transcript] = []
        ear = FakeEar(SIX_HUNDRED)
        thread = AudioThread(audio_queue=audio_q, stop_event=threading.Event(), transcriber=NoVoice(),
                             on_transcript=seen.append, second_ear=ear)
        thread._process_chunk(b"\x10\x00" * 1600, utterance_start_ts=5.0)   # noqa: SLF001
        (transcript,) = seen
        assert audio_q.empty() and transcript.no_speech and transcript.events == ()
        assert transcript.ear["text"] == "六百" and transcript.ear_wanted is False and len(ear.heard) == 1

    def test_the_second_ear_must_agree_with_itself(self):
        # 雑談「お願いしま」の中でいちばん近い候補は「千」だが、自由に聞いた文は千と読めない = 使わない
        events, (transcript,) = _run("撮れないからね。", FakeEar(CHATTER))
        assert events == [] and transcript.ear_text is None and transcript.ear["text"] == "お願いしま"

    def test_an_empty_transcription(self):
        events, seen = _run("", FakeEar(se.EarResult("", -0.5, [("百", -3.0)])))
        assert events == [] and seen == []                                      # 何も聞こえなかった
        events, (transcript,) = _run("", FakeEar(SIX_HUNDRED))
        assert [e.amount for e in events] == [600] and transcript.text == ""

    def test_a_failing_second_ear_does_not_stop_listening(self):
        events, (transcript,) = _run("ご視聴ありがとうございました。", FakeEar(fail=True))
        assert events == [] and transcript.noise and transcript.ear is None

    def test_without_the_second_ear(self):
        events, (transcript,) = _run("ご視聴ありがとうございました。", None)
        assert events == [] and transcript.noise and transcript.ear is None


class TestRecord:
    def test_the_transcript_log_keeps_what_the_second_ear_heard(self, tmp_path):
        log = TranscriptLog(tmp_path / "s.transcripts.jsonl")
        _, (rescued,) = _run("のっぴょく", FakeEar(SIX_HUNDRED))
        _, (plain,) = _run("コール", FakeEar(SIX_HUNDRED))
        _, (no_ear,) = _run("コール", None)
        log.write(rescued)
        log.write(plain)
        log.write(no_ear)
        first, second, third = [json.loads(line) for line in log.path.read_text(encoding="utf-8").splitlines()]
        assert first["ear_text"] == "六百" and first["ear"]["candidates"][0] == {"text": "六百", "logp": -1.0}
        assert first["events"][0]["parse_flags"] == ["amount_only", "second_ear"] and first["ear_wanted"] is True
        # Whisper が読めた発話も、第 2 の耳の結果を記録だけする（ライブは使っていない）
        assert second["ear"]["text"] == "六百" and second["ear_wanted"] is False and second["ear_text"] is None
        assert "ear" not in third and "ear_wanted" not in third           # 第 2 の耳が無い

    def test_the_cli_line(self, capsys):
        import main

        _, (transcript,) = _run("ご視聴ありがとうございました。", FakeEar(SIX_HUNDRED))
        main._print_transcript(transcript)                                   # noqa: SLF001
        out = capsys.readouterr().out
        assert "「ご視聴ありがとうございました。」→ 第 2 の耳「六百」→ bet/raise 600 （数字だけ・第 2 の耳）" in out

    def test_reading_the_record_again_uses_the_same_rule(self):
        """評価（eval_store）は、ライブで聞き直した結果をいまの規則で読み直す。"""
        from core.events import AudioEvent

        rows = [
            {"utterance_start_ts": 1.0, "heard_at": 2.0, "text": "のっぴょく", "audio_file": "a.wav",
             "ear": SIX_HUNDRED.to_dict()},
            {"utterance_start_ts": 3.0, "heard_at": 4.0, "text": "撮れないからね。", "audio_file": "b.wav",
             "ear": CHATTER.to_dict()},
            {"utterance_start_ts": 5.0, "heard_at": 6.0, "text": "コールですか？", "audio_file": "c.wav",
             "ear": SIX_HUNDRED.to_dict()},
        ]
        live = [AudioEvent(action="call", amount=0, timestamp=9.0, raw_text="コール")]
        out = eval_store.reparse_events(live, rows)
        assert [(e.action, e.amount, e.timestamp) for e in out[:1]] == [("bet", 600, 2.0)]
        assert "second_ear" in out[0].parse_flags and len(out) == 2               # 雑談・確認の発話は読まない
        # 別の文で置き換えた発話には使わない（方式の比べ）
        assert eval_store.reparse_events(live, rows, {"a.wav": "えっと"}) == live

    def test_reading_again_uses_only_the_ears_the_live_used(self):
        """記録だけの耳（`ear_wanted` = 偽）は、読み直しでも使わない（ライブと同じ。読み取りの規則が変わってその発話が
        聞き直す発話になっても、ライブはその耳を使っていない）。`ear_wanted` の無い古い記録は、耳があれば使った。"""
        from core.events import AudioEvent

        agreeing = se.EarResult("六百", -1.0, [("六百", -1.0)]).to_dict()
        row = {"utterance_start_ts": 1.0, "heard_at": 2.0, "text": "撮れないからね。", "audio_file": "a.wav",
               "ear": agreeing}
        live = [AudioEvent(action="call", amount=0, timestamp=9.0, raw_text="コール")]
        used = eval_store.reparse_events(live, [row])
        assert [(e.action, e.amount) for e in used[:1]] == [("bet", 600)]
        assert eval_store.reparse_events(live, [{**row, "ear_wanted": True}])[0].amount == 600
        assert eval_store.reparse_events(live, [{**row, "ear_wanted": False}]) == live
        assert se.used_ear({**row, "ear_wanted": False}) is None and se.used_ear(row) is agreeing
        assert se.used_ear({"text": "コール"}) is None and se.used_ear(None) is None


class TestAgreement:
    @pytest.mark.parametrize("ear, expected", [
        (SIX_HUNDRED.to_dict(), "六百"),
        (CHATTER.to_dict(), None),
        ({"text": "", "logp": -0.5, "candidates": [{"text": "百", "logp": -3.0}]}, None),     # 何も聞こえていない
        ({"text": "コールします", "logp": -2.0, "candidates": [{"text": "コールです", "logp": -9.0}]}, "コールです"),
        ({"text": "三万四千四百点", "logp": -5.0, "candidates": [{"text": "四千四百点", "logp": -2.5}]}, None),
        ({"text": "六百", "logp": -1.0, "candidates": []}, None),
        (None, None),
    ])
    def test_the_free_text_must_read_as_the_candidate(self, ear, expected):
        assert se.agreed_candidate(ear) == expected

    def test_pcm16_to_16khz_float(self):
        pcm = np.array([0, 16384, -32768], dtype=np.int16).tobytes()
        assert se.pcm16_samples(pcm).tolist() == [0.0, 0.5, -1.0]
        assert len(se.pcm16_samples(b"\x00\x10" * 800, sample_rate=8000)) == 1600


class TestLoad:
    def test_turned_off(self, tmp_path):
        assert se.load_live(tmp_path, {"second_ear": {"enabled": False}}) == (
            None, "第 2 の耳は使いません（audio.second_ear.enabled=false）")

    def test_no_model_yet(self, tmp_path):
        ear, message = se.load_live(tmp_path, {})                     # 項目が無い config でも使う = モデルを探す
        assert ear is None and "モデルがありません" in message

    def _model(self, tmp_path):
        folder = se.model_dir(tmp_path)
        folder.mkdir(parents=True)
        for name in se.MODEL_FILES:
            (folder / name).write_text("x", encoding="utf-8")

    def test_missing_parts(self, tmp_path, monkeypatch):
        self._model(tmp_path)
        monkeypatch.setitem(sys.modules, "kaldi_native_fbank", None)     # import すると ImportError
        ear, message = se.load_live(tmp_path, {})
        assert ear is None and "部品がありません" in message

    def test_loaded_and_checked(self, tmp_path, monkeypatch):
        self._model(tmp_path)
        monkeypatch.setitem(sys.modules, "kaldi_native_fbank", types.ModuleType("kaldi_native_fbank"))
        fake = FakeEar(SIX_HUNDRED)
        seen: list = []
        monkeypatch.setattr(se.SecondEar, "load", classmethod(lambda cls, folder, threads=4: seen.append(threads) or fake))
        ear, message = se.load_live(tmp_path, {"second_ear": {"threads": 6}})
        assert ear is fake and seen == [6] and len(fake.heard) == 1 and "使います" in message

    def test_a_broken_model(self, tmp_path, monkeypatch):
        self._model(tmp_path)
        monkeypatch.setitem(sys.modules, "kaldi_native_fbank", types.ModuleType("kaldi_native_fbank"))
        monkeypatch.setattr(se.SecondEar, "load", classmethod(lambda cls, folder, threads=4: FakeEar(fail=True)))
        ear, message = se.load_live(tmp_path, {})
        assert ear is None and "読み込めませんでした" in message and "onnx failed" in message


# ――― 読み上げ集 2026-09-30: 額の補い・読み上げ・役の名前 ―――

BET_300 = se.EarResult("ベッド三百", -0.1, [("ベット 三百", -3.3), ("三百", -9.1)])
RAISE_1800 = se.EarResult("レイズ千八百", -0.1, [("レイズ 千八百", -0.1), ("千八百", -4.7)])
EIGHT_HUNDRED = se.EarResult("レーズ八百句", -0.5, [("八百", -10.0), ("レイズ 八百", -10.8)])
POT = se.EarResult("一万二千です", -0.3, [("一万二千", -0.5)])
TWO_PAIR = se.EarResult("二ペア", -0.2, [("ツーペア", -0.4), ("百", -12.0)])


class TestAmountFromTheEar:
    """Whisper がベット・レイズを読めて額だけ崩したとき（「ベッド サンビュアック」「レイズ3 ハピック」）、第 2 の耳の
    額を入れる。自由に聞いた文と候補が同じ額のときだけ（要確認の印）。"""

    @pytest.mark.parametrize("text, result, want", [
        ("ベッド サンビュアック", BET_300, ("bet", 300)),
        ("レイズ3 ハピック", RAISE_1800, ("raise", 1800)),        # 100 未満 = 聞き違い
        ("レイズ アクション", EIGHT_HUNDRED, ("raise", 800)),      # 候補は額だけ・自由に聞いた文はレイズ + 額
    ])
    def test_the_amount_is_filled(self, text, result, want):
        ear = FakeEar(result)
        events, (transcript,) = _run(text, ear)
        assert [(e.action, e.amount) for e in events] == [want]
        assert "second_ear" in events[0].parse_flags and events[0].confidence <= se.EAR_CONFIDENCE
        assert transcript.ear_text == result.candidates[0][0] and len(ear.heard) == 1

    def test_no_amount_when_the_ear_heard_none(self):
        """額を次の発話で言った（「ベット、」「2000」）なら、第 2 の耳の自由に聞いた文にも額が無い。"""
        events, (transcript,) = _run("ベット", FakeEar(se.EarResult("ベッド", -0.1, [("ベット 三百", -3.0)])))
        assert [(e.action, e.amount) for e in events] == [("bet", 0)] and transcript.ear_text is None

    def test_a_read_amount_is_heard_only_for_the_amount_scores(self):
        """額を読めた発話も聞き直す（2026-10-01）が、読みは変えない。額ごとの点数を付けて engine に渡す（いま使えない
        額だったときに、使える額から選び直すため）。"""
        result = se.EarResult("レイズ千二百", -0.2, [("レイズ 千二百", -0.2)], amounts=[(1200, -0.2), (200, -3.0)])
        ear = FakeEar(result)
        events, (transcript,) = _run("レイズ 1200", ear)
        assert [(e.action, e.amount) for e in events] == [("raise", 1200)] and len(ear.heard) == 1
        assert "second_ear" not in events[0].parse_flags and transcript.ear_text is None
        assert events[0].amount_scores == ((1200, -0.2), (200, -3.0))
        assert transcript.ear["amounts"] == [[1200, -0.2], [200, -3.0]]

    def test_only_one_amountless_bet_is_filled(self):
        assert se.fill_amounts(se.rescue_events(None) + [], RAISE_1800.to_dict()) is None
        two = __import__("audio.recognizer", fromlist=["parse_actions"]).parse_actions("ベット、レイズ")
        assert se.fill_amounts(two, RAISE_1800.to_dict()) is None


class TestAnnouncements:
    def test_a_pot_announcement_is_not_heard_again(self):
        """「ポット1万2000です。」を第 2 の耳が「一万二千です」と聞いて額にしていた。2026-10-07 から全発話を聞くが、
        記録だけ（使わない）。"""
        ear = FakeEar(POT)
        events, (transcript,) = _run("ポット1万2000です。", ear)
        assert events == [] and len(ear.heard) == 1 and transcript.ear_wanted is False and transcript.ear_text is None

    def test_a_blind_announcement_is_not_an_action(self):
        """Whisper が間に「オールイン」を足した（「ブラインド200、オールイン400です。」）。"""
        events, _ = _run("ブラインド200、オールイン400です。", FakeEar(POT))
        assert events == []


class TestHandNames:
    def test_the_ear_hears_hand_names(self):
        """Whisper が「ツーペア」を「つぺよ!」にした（第 2 の耳は「二ペア」）。"""
        events, _ = _run("つぺよ!", FakeEar(TWO_PAIR))
        assert [(e.action, e.hand_name) for e in events] == [("end_hand", "Two pair")]
        assert "second_ear" in events[0].parse_flags

    def test_different_hand_names_do_not_agree(self):
        ear = se.EarResult("ワンペア", -0.2, [("ツーペア", -0.4)])
        assert se.agreed_candidate(ear.to_dict()) is None

    def test_candidates_include_hand_names(self):
        texts = {c.text for c in se.build_candidates(amounts=(100,))}
        assert {"ワンペア", "ツーペア", "フルハウス", "ストレートフラッシュ"} <= texts
        assert any(c.spoken == "二ペア" and c.text == "ツーペア" for c in se.build_candidates(amounts=(100,)))

    def test_candidates_include_hits_and_highs(self):
        texts = {c.text for c in se.build_candidates(amounts=(100,))}
        assert {"十ヒット", "テンヒット", "エースヒット", "キングハイ"} <= texts

    def test_a_hit_heard_only_by_the_ear(self):
        """店舗 2026-10-06 05cccd6c ハンド 18: Whisper「転筆と」、第 2 の耳は自由に「十ヒット」と聞いた。"""
        events, _ = _run("転筆と", FakeEar(se.EarResult("十ヒット", -0.3, [("十ヒット", -0.3), ("テンヒット", -1.5)])))
        assert [(e.action, e.hand_name, e.hit_rank) for e in events] == [("end_hand", "One pair", "T")]
        assert "second_ear" in events[0].parse_flags


class TestReparseUsesTheSameRules:
    def test_eval_store_fills_the_amount_from_the_logged_ear(self):
        row = {"utterance_start_ts": 5.0, "heard_at": 6.0, "text": "レイズ3 ハピック", "audio_sec": 1.6,
               "confidence": 0.4, "ear": RAISE_1800.to_dict()}
        events = eval_store.reparse_events([], [row])
        assert [(e.action, e.amount) for e in events] == [("raise", 1800)]
