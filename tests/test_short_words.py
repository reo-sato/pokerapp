"""tests/test_short_words.py — 短い語の分類器（audio/short_words.py・tools/short_words.py）と推定器の読み（2026-10-07）。"""
from __future__ import annotations

import math

import numpy as np
import pytest

from audio import second_ear as se
from audio import short_words as sw


def _ear(words: dict[str, float], free: float = -1.0, text: str = "これ", amounts=None, frames: int = 20,
         candidates=(("コール", -3.0),)) -> dict:
    return se.EarResult(text, free, list(candidates), amounts=amounts or [(600, -9.0), (1500, -11.0)],
                        words=words, frames=frames).to_dict()


class TestFeatures:
    def test_old_ears_without_short_words_have_no_features(self):
        assert sw.features(se.EarResult("六百", -1.0, [("六百", -1.0)]).to_dict(), ("コール",)) is None
        assert sw.features(None, ("コール",)) is None

    def test_differences_from_the_free_text(self):
        x = sw.features(_ear({"コール": -3.0, "これ": -1.0}), ("コール", "これ", "フォールド"))
        names = sw.feature_names(("コール", "これ", "フォールド"))
        assert len(x) == len(names)
        got = dict(zip(names, x))
        assert got["word:コール"] == pytest.approx(-2.0) and got["word:これ"] == pytest.approx(0.0)
        assert got["word:フォールド"] == -40.0                         # 採点していない語 = いちばん低い
        assert got["best_amount"] == pytest.approx(-8.0) and got["best_candidate"] == pytest.approx(-2.0)
        assert got["frames"] == pytest.approx(0.2) and got["free_chars"] == pytest.approx(0.2)


def _model(weights: np.ndarray, words=("コール", "これ")) -> sw.Model:
    n = len(sw.feature_names(words))
    return sw.Model({"version": "t", "words": list(words), "labels": list(sw.LABELS), "mean": [0.0] * n,
                     "std": [1.0] * n, "weights": weights.tolist(), "prior": {}})


class TestModel:
    def test_predict_is_a_distribution_over_the_labels(self):
        n = len(sw.feature_names(("コール", "これ")))
        w = np.zeros((n + 1, len(sw.LABELS)))
        w[1, sw.LABELS.index("call")] = 1.0                      # 「これ」が確からしいほどコール
        model = _model(w)
        p = model.predict(_ear({"コール": -3.0, "これ": -1.0}))
        assert set(p) == set(sw.LABELS) and sum(p.values()) == pytest.approx(1.0)
        q = model.predict(_ear({"コール": -3.0, "これ": -9.0}))
        assert p["call"] > q["call"]
        assert sw.Model(model.to_dict()).predict(_ear({"コール": -3.0, "これ": -1.0})) == pytest.approx(p)

    def test_the_committed_model_reads(self):
        model = sw.load()
        assert model is not None and model.words == se.SHORT_WORDS and model.labels == sw.LABELS


class TestTraining:
    def test_the_alignment_lets_an_unreadable_utterance_carry_a_row(self):
        from tools import short_words as tsw

        items = [{"u": 0, "kind": "unreadable", "tok": None, "hint": ("call", 0)},
                 {"u": 1, "kind": "token", "tok": ("fold", 0), "hint": None},
                 {"u": 2, "kind": "unreadable", "tok": None, "hint": None}]
        matched = tsw.align([("call", 0), ("fold", 0)], items, [False, False])
        assert matched == [0, 1]
        assert tsw._label(["call"]) == "call" and tsw._label(["raise"]) == "amount" and tsw._label([]) == "none"
        assert tsw._label(["call", "fold"]) == "multi" and tsw._label(["allin"]) == "other"

    def test_a_garbled_call_word_can_carry_the_fold(self):
        """聞き違えたコールの語（「コールド」）はフォールドの行も受け持つ（店舗 b7f32031 ハンド 2: 「コーデー」= コール・
        「コールド」= フォールドで、チェック 3 つのあとのチェックアラウンドは言い直し）。"""
        from tools import short_words as tsw

        items = [{"u": 0, "kind": "token", "tok": ("call", 0), "hint": None},
                 {"u": 1, "kind": "unreadable", "tok": None, "hint": ("call", 0)},
                 {"u": 2, "kind": "token", "tok": ("call", 0), "hint": None, "garbled": True},
                 *({"u": 3 + k, "kind": "token", "tok": ("check", 0), "hint": None} for k in range(3)),
                 {"u": 6, "kind": "token", "tok": ("check_around", 0), "hint": None}]
        truth = [("call", 0), ("call", 0), ("fold", 0), ("check", 0), ("check", 0), ("check", 0)]
        assert tsw.align(truth, items, [False] * 6) == [0, 1, 2, 3, 4, 5]

    def test_owner_labels_are_the_answer(self):
        """オーナーが聞いたラベルの発話は、その語として突き合わせる（雑談なら行を受け持たない = 隣の「アクション」に
        フォールドを付けない、店舗 05cccd6c ハンド 19）。"""
        from tools import short_words as tsw

        rows = [{"utterance_start_ts": 10.0, "text": "アクション"}, {"utterance_start_ts": 13.0, "text": "ご覧いただき"}]
        items = tsw.heard_items(rows, {13.0: "fold"})
        assert [it["kind"] for it in items] == ["unreadable", "owner"]
        assert tsw.align([("fold", 0)], items, [False]) == [1]
        items = tsw.heard_items(rows, {10.0: "none", 13.0: "fold"})
        assert items[0]["kind"] == "other" and tsw.align([("fold", 0)], items, [False]) == [1]

    def test_owner_labels_file(self, tmp_path):
        from tools import short_words as tsw

        (tmp_path / tsw.LABELS_FILE).write_text(
            '{"labels": {"1791283703.5776863": {"label": "call", "item": 2}, "5.0": {"label": "allin"}}}',
            encoding="utf-8")
        assert tsw.owner_labels(tmp_path) == {1791283703.5776863: "call"}
        assert tsw.owner_labels(tmp_path / "none") == {} and tsw.owner_labels(None) == {}

    def test_the_hand_ending_fold_is_the_next_utterance(self):
        """ハンドを終わらせるフォールドは、最後のベットの次の発話（読めない・聞き違えたコール、6 秒以内）。後ろの雑談の
        「フォールドして、フォールドじゃねーよ」には付けない（店舗 7b897671 ハンド 1）。"""
        from tools import short_words as tsw

        rows = [{"utterance_start_ts": t} for t in (0.0, 2.4, 11.0)]
        items = [{"u": 0, "kind": "token", "tok": ("wager", 2400), "hint": None},
                 {"u": 1, "kind": "unreadable", "tok": None, "hint": None},
                 {"u": 2, "kind": "token", "tok": ("fold", 0), "hint": None}]
        truth = [{"action": "bet", "amount": 2400, "optional": False}, {"action": "fold", "optional": True}]
        assert tsw.end_fold(truth, items, [0, 2], rows) == [0, 1]
        assert tsw.end_fold(truth, items, [0, None], rows) == [0, 1]
        late = [dict(r, utterance_start_ts=r["utterance_start_ts"] + (7.0 if i else 0.0)) for i, r in enumerate(rows)]
        assert tsw.end_fold(truth, items, [0, None], late) == [0, None]          # 6 秒より後 = 言われていない
        chat = [items[0], {"u": 1, "kind": "token", "tok": ("check", 0), "hint": None}, items[2]]
        assert tsw.end_fold(truth, chat, [0, 2], rows) == [0, 2]                  # 次の発話がほかの語
        same = [items[0], {"u": 0, "kind": "token", "tok": ("fold", 0), "hint": None}, *items[1:]]
        assert tsw.end_fold(truth, same, [0, 1], rows) == [0, 1]                  # ベットと同じ発話で言った

    def test_restated_words_take_the_label_of_the_word(self):
        """言い直し（同じ語を 15 秒以内にもう一度・読めない発話は隣の語と第 2 の耳の候補が同じ）は、その語のラベル。"""
        from tools import short_words as tsw

        rows = [{"utterance_start_ts": t} for t in (0.0, 1.0, 3.0, 40.0)]
        items = [{"u": 0, "kind": "unreadable", "tok": None, "hint": ("call", 0)},       # 「コーナー」
                 {"u": 1, "kind": "token", "tok": ("call", 0), "hint": None},            # 「コール7400点。」
                 {"u": 2, "kind": "token", "tok": ("call", 0), "hint": None},            # 「コール」
                 {"u": 3, "kind": "token", "tok": ("call", 0), "hint": None}]            # 40 秒後
        truth = [{"action": "call", "amount": 7400, "optional": False}]
        assert tsw.restated(truth, items, [1], rows) == {0: "call", 2: "call"}

    def test_training_separates_and_is_repeatable(self):
        from tools import short_words as tsw

        rng = np.random.default_rng(0)
        words = ("コール", "これ")
        n = len(sw.feature_names(words))
        x = rng.normal(size=(300, n))
        y = np.asarray([0 if v < 0 else 1 for v in x[:, 1]])
        a = tsw.train(x, y, words)
        b = tsw.train(x, y, words)
        assert a.version == b.version and np.allclose(a.weights, b.weights)
        z = np.hstack([(x - a.mean) / a.std, np.ones((len(x), 1))]) @ a.weights
        assert float((z.argmax(axis=1) == y).mean()) > 0.9


class FakeModel:
    def __init__(self, probs: dict[str, float]) -> None:
        self.probs = probs

    def predict(self, ear):
        return dict(self.probs) if ear else None


@pytest.fixture
def v1_params():
    """推定器 v1 の読みの値から、分類器の読みの上限（`short_cap`）を外したもの（式を確かめる）。"""
    from integration.estimator import reading_params

    return dict(reading_params(), short_cap=99.0)


class TestEstimatorOptions:
    def _options(self, monkeypatch, row, probs, params):
        from tools import estimate

        monkeypatch.setattr(sw, "load", lambda path=None: FakeModel(probs))
        return estimate.utterance_options(row, params)

    def test_a_clipped_call_heard_as_chatter(self, monkeypatch, v1_params):
        """Whisper は雑談と書いたが、第 2 の耳の音はコールらしい = コールの読みを選択肢に（確からしさの比 + Whisper が
        読めなかった項）。"""
        row = {"utterance_start_ts": 1.0, "text": "撮れないからね。", "no_speech": False, "ear_wanted": False,
               "ear": _ear({"コール": -3.0, "これ": -1.0})}
        probs = {"none": 0.1, "call": 0.8, "check": 0.04, "fold": 0.04, "amount": 0.02}
        opts = self._options(monkeypatch, row, probs, v1_params)
        assert opts[0].source == "drop" and opts[0].logp == 0.0
        short = [o for o in opts if o.source == "short"]
        assert [o.text for o in short] == ["コール"]
        assert short[0].logp == pytest.approx(v1_params["short_none_penalty"] + math.log(0.8 / 0.1))

    def test_an_amount_from_the_ear_table(self, monkeypatch, v1_params):
        row = {"utterance_start_ts": 1.0, "text": "ぜひ見てみてください。", "no_speech": False, "ear_wanted": False,
               "ear": _ear({"コール": -9.0}, amounts=[(1500, -2.0), (500, -6.0), (2500, -7.0), (100, -12.0)])}
        probs = {"none": 0.2, "call": 0.05, "check": 0.05, "fold": 0.05, "amount": 0.65}
        opts = self._options(monkeypatch, row, probs, v1_params)
        amounts = [o for o in opts if o.source == "short_amount"]
        assert [o.text for o in amounts] == ["1500", "500", "2500"]          # 上から short_amount_top 個
        share = -2.0 - (-2.0 + math.log(1 + math.exp(-4) + math.exp(-5) + math.exp(-10)))
        assert amounts[0].logp == pytest.approx(v1_params["short_none_penalty"] + math.log(0.65 / 0.2) + share)
        assert amounts[0].keys[0][:2] == ("bet", 1500)

    def test_a_clear_whisper_word_costs_a_substitution(self, monkeypatch, v1_params):
        row = {"utterance_start_ts": 1.0, "text": "チェック", "no_speech": False, "ear_wanted": False,
               "ear": _ear({"コール": -1.0}, candidates=(("六百", -9.0),))}     # 候補にコールが無い（耳の候補と別に）
        probs = {"none": 0.05, "call": 0.6, "check": 0.3, "fold": 0.03, "amount": 0.02}
        opts = self._options(monkeypatch, row, probs, v1_params)
        short = [o for o in opts if o.source == "short"]
        assert [o.text for o in short] == ["コール"]
        assert short[0].logp == pytest.approx(opts[0].logp + v1_params["short_read_penalty"] + math.log(0.6 / 0.3))

    def test_the_whisper_term_comes_from_the_confusion_table(self, monkeypatch, v1_params):
        """表があれば Whisper の読みの項は log P(Whisper の読み | L) − log P(Whisper の読み | L0)（固定の罰の代わり）。"""
        from tools import estimate

        class WithTable(FakeModel):
            def whisper_logp(self, heard, label):
                table = {("none", "none"): 0.8, ("none", "call"): 0.2}
                return math.log(table[(heard, label)]) if (heard, label) in table else None

        row = {"utterance_start_ts": 1.0, "text": "撮れないからね。", "no_speech": False, "ear_wanted": False,
               "ear": _ear({"コール": -3.0, "これ": -1.0})}
        probs = {"none": 0.1, "call": 0.8, "check": 0.04, "fold": 0.04, "amount": 0.02}
        monkeypatch.setattr(sw, "load", lambda path=None: WithTable(probs))
        short = [o for o in estimate.utterance_options(row, v1_params) if o.source == "short"]
        assert [o.text for o in short] == ["コール"]
        assert short[0].logp == pytest.approx(math.log(0.8 / 0.1) + math.log(0.2 / 0.8))

    def test_v1_reads_at_most_one_above_the_default(self, monkeypatch):
        """v1 は分類器の読みを既定の読みより log で 1 までしか高くしない（`short_cap` 1 = どの読みかは主にハンドの筋で
        決める）。"""
        from integration.estimator import reading_params

        params = reading_params()
        assert params["all_ears"] == 1.0 and params["short_words"] == 1.0 and params["short_cap"] == 1.0
        row = {"utterance_start_ts": 1.0, "text": "撮れないからね。", "no_speech": False, "ear_wanted": False,
               "ear": _ear({"コール": -3.0, "これ": -1.0})}
        probs = {"none": 0.01, "call": 0.97, "check": 0.01, "fold": 0.005, "amount": 0.005}
        opts = self._options(monkeypatch, row, probs, params)
        assert [(o.source, o.logp) for o in opts if o.source == "short"] == [("short", 1.0)]
        weak = {"none": 0.1, "call": 0.8, "check": 0.04, "fold": 0.04, "amount": 0.02}     # 上限より下 = 式のまま
        opts = self._options(monkeypatch, row, weak, params)
        assert [o.logp for o in opts if o.source == "short"] == [pytest.approx(params["short_none_penalty"] + math.log(8))]

    def test_not_for_questions_or_words_the_engine_reads_by_the_table(self, monkeypatch, v1_params):
        """確認の問い（「これでいいですか?」）と、engine が札の離脱・手番で読む語（「これで終わりです」= garbled_call）には
        分類器の読みを重ねない（店舗 05cccd6c ハンド 1・d0f055fb ハンド 8 を悪くした）。"""
        probs = {"none": 0.05, "call": 0.5, "check": 0.05, "fold": 0.35, "amount": 0.05}
        for text in ("これでいいですか?", "これで終わりです。"):
            row = {"utterance_start_ts": 1.0, "text": text, "no_speech": False, "ear_wanted": False,
                   "ear": _ear({"コール": -3.0, "これ": -1.0})}
            opts = self._options(monkeypatch, row, probs, v1_params)
            assert not [o for o in opts if o.source.startswith("short")], text

    def test_off_in_v0_and_for_sounds_that_are_not_voices(self, monkeypatch, v1_params):
        from tools.estimate import PARAMS

        row = {"utterance_start_ts": 1.0, "text": "撮れないからね。", "no_speech": False, "ear_wanted": False,
               "ear": _ear({"コール": -3.0, "これ": -1.0})}
        probs = {"none": 0.1, "call": 0.8, "check": 0.04, "fold": 0.04, "amount": 0.02}
        assert not [o for o in self._options(monkeypatch, row, probs, PARAMS) if o.source.startswith("short")]
        silent = dict(row, no_speech=True, text="")
        assert not [o for o in self._options(monkeypatch, silent, probs, v1_params) if o.source.startswith("short")]

    def test_record_only_ears_give_other_readings_but_not_the_default(self, monkeypatch, v1_params):
        """`all_ears`: 記録だけの耳（ライブは使っていない）も別の読みの候補になる。既定の読みはライブの規則のまま。"""
        from tools.estimate import PARAMS

        ear = se.EarResult("六百", -1.0, [("六百", -1.0), ("八百", -6.0)]).to_dict()
        row = {"utterance_start_ts": 1.0, "text": "撮れないからね。", "no_speech": False, "ear_wanted": False, "ear": ear}
        v1 = self._options(monkeypatch, row, {"none": 1.0, "call": 0, "check": 0, "fold": 0, "amount": 0}, v1_params)
        assert v1[0].source == "drop" and "六百" in [o.text for o in v1 if o.source == "ear"]
        v0 = self._options(monkeypatch, row, {"none": 1.0, "call": 0, "check": 0, "fold": 0, "amount": 0}, PARAMS)
        assert [o.source for o in v0] == ["drop"]
