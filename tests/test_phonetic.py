"""tests/test_phonetic.py

音の近さでアクションの語を読む（`audio/phonetic.py`, ADR-0056 追記 1 の S2）。

店舗の書き起こしには辞書に無いゆれが毎回新しく出る（ヘッドアップ・ヘッドホップ・ヘッドロップ・ヘッドゾップ =
ヘッズアップ）。見つけるたびに語を足す代わりに、片仮名の語をアクションの語と音の近さで照合する。
"""
from __future__ import annotations

import pytest

import audio.phonetic as phonetic
from audio.phonetic import distance, match_keyword, morae
from audio.recognizer import parse_actions
from audio.recorder import describe_events


def _parsed(text: str) -> list[tuple]:
    return [(e.action, e.amount, e.seat, tuple(e.parse_flags)) for e in parse_actions(text, confidence=0.5)]


class TestMorae:
    def test_small_kana_join_the_previous_kana(self):
        assert morae("フォールド") == [("f", "o", ""), ("", "o", "long"), ("r", "u", ""), ("d", "o", "")]
        assert morae("チェック") == [("ch", "e", ""), ("Q", "", "Q"), ("k", "u", "")]
        assert morae("キャ") == [("ky", "a", "")]

    def test_spellings_of_the_same_sound_are_equal(self):
        assert morae("レイズ") == morae("レーズ")
        assert morae("ショウダウン") == morae("ショーダウン")
        assert morae("コオル") == morae("コール")
        assert morae("ふぉーるど") == morae("フォールド")        # ひらがなも同じ
        assert morae("ヘッズ・アップ") == morae("ヘッズアップ")    # 仮名以外は無視


class TestDistance:
    def test_identical_is_zero(self):
        assert distance(morae("ヘッズアップ"), morae("ヘッズアップ")) == 0

    def test_near_sounds_cost_less_than_unrelated_ones(self):
        # 濁点の違い（ゴール）は、無関係な子音（ネット → ベット）より近い
        assert distance(morae("ゴール"), morae("コール")) < distance(morae("ネット"), morae("ベット"))
        # フ/ホ・シ/ス は近い音
        assert distance(morae("ホールド"), morae("フォールド")) <= 0.1
        assert distance(morae("ソーダウン"), morae("ショーダウン")) <= 0.1

    def test_a_dropped_last_sound_is_cheap(self):
        assert distance(morae("フォール"), morae("フォールド")) < distance(morae("フォール"), morae("コール"))

    def test_the_first_sound_is_weighted(self):
        # 語頭の無関係な音への置き換え（ワールド）は、語中の同じ置き換えより遠い
        assert distance(morae("ワールド"), morae("フォールド")) > distance(morae("フォーワド"), morae("フォールド"))


class TestMatchKeyword:
    @pytest.mark.parametrize("heard, rewrite", [
        # 店舗の書き起こしで辞書に無かったゆれ（2026-09-27）
        ("ヘッドゾップ", "ヘッズアップ"),
        ("フォール", "フォールド"),
        # 近い音のゆれ
        ("ペット", "ベット"),
        ("レイス", "レイズ"),
        ("テック", "チェック"),
        ("ショーダン", "ショーダウン"),
        ("ヘッツアップ", "ヘッズアップ"),
        ("オーイン", "オールイン"),
        ("スリーベッド", "レイズ"),
        ("チェックアウンド", "チェックアラウンド"),
    ])
    def test_near_words_are_read(self, heard, rewrite):
        match = match_keyword(heard)
        assert match is not None and match.rewrite == rewrite

    @pytest.mark.parametrize("heard", [
        # 店舗の書き起こしで、アクションではなかった片仮名の語
        "トーン", "トップ", "フローク", "ローピック", "ペットボックス", "ファミリーポット", "タワーオン", "マッケー",
        "シング", "ラストカード", "アクション",
        # 卓の用語・相づち・ありふれた語
        "フロップ", "ターン", "リバー", "ボタン", "オーライ", "オッケー", "ジャック", "キッカー",
        "ワールド", "ネット", "セット", "ベスト", "ポール", "ボール", "ドール", "コーラ", "コーヒー", "クール",
        "バック", "マッチ", "ショー", "ダウン", "ヘッド", "ゴールデン", "ホテル",
        # 意味の違う 2 つの語に同じくらい近い（ゴールド = ホールド / ゴール、オール = オールイン / ホールド）
        "ゴールド", "オール",
    ])
    def test_other_words_are_not_read(self, heard):
        assert match_keyword(heard) is None

    def test_two_sounds_are_too_short(self):
        assert match_keyword("チェク") is None
        assert match_keyword("オル") is None

    def test_store_variants_are_read_without_the_dictionary(self, monkeypatch):
        """店舗で見つけて辞書に足したゆれは、正準の語だけでも音の近さで読める（足さなくてよかった）。"""
        canonical = {"ベット", "コール", "レイズ", "リレイズ", "スリーベット", "チェックレイズ", "チェック",
                     "フォールド", "オールイン", "ショーダウン", "ヘッズアップ", "チェックアラウンド"}
        monkeypatch.setattr(phonetic, "_TARGETS", tuple(
            t for t in phonetic._TARGETS if t.rewrite is None or t.word in canonical))   # noqa: SLF001
        store = {
            "ベッド": "ベット", "ゴール": "コール", "ホールド": "フォールド", "フォルド": "フォールド",
            "オーリン": "オールイン", "ソーダウン": "ショーダウン", "ヘッドアップ": "ヘッズアップ",
            "ヘッドホップ": "ヘッズアップ", "ヘッドロップ": "ヘッズアップ", "ヘッドゾップ": "ヘッズアップ",
            "チッカーランド": "チェックアラウンド", "チェックアランド": "チェックアラウンド",
            "チェックアウンド": "チェックアラウンド", "フォール": "フォールド",
        }
        read = {heard: getattr(match_keyword(heard), "rewrite", None) for heard in store}
        assert read == store

    def test_rank_lists_every_word(self):
        ranked = phonetic.rank("ヘッドゾップ")
        assert ranked[0][0] <= ranked[1][0]
        assert {rewrite for _, _, rewrite in ranked[:3]} == {"ヘッズアップ"}


class TestParseActions:
    def test_store_heads_up(self):
        (event,) = parse_actions("ここまでのヘッドゾップです。")
        assert event.action == "heads_up" and event.parse_flags == ("fuzzy_keyword",)
        assert event.raw_text == "ここまでのヘッドゾップです。"         # 聞こえたまま

    def test_second_word_of_a_chain(self):
        events = parse_actions("フォールド、フォール")
        assert [(e.action, e.parse_flags, e.raw_text) for e in events] == [
            ("fold", (), "フォールド"), ("fold", ("fuzzy_keyword",), "フォール"),
        ]

    def test_amounts_and_seats_are_read_as_usual(self):
        assert _parsed("シート3 レイス 2千") == [("raise", 2000, 3, ("fuzzy_keyword",))]
        assert _parsed("ペット600") == [("bet", 600, None, ("fuzzy_keyword",))]
        assert _parsed("ペットナナ") == [("bet", 7, None, ("fuzzy_keyword",))]       # ベトナナ と同じ
        assert _parsed("チェック、ペット2千") == [
            ("check", 0, None, ()), ("bet", 2000, None, ("fuzzy_keyword",)),
        ]

    def test_same_sound_spellings_are_not_marked_for_review(self):
        assert _parsed("レーズ 800") == [("raise", 800, None, ())]
        assert _parsed("ヘッズ・アップ") == [("heads_up", 0, None, ())]
        assert _parsed("ショウダウンです") == [("showdown", 0, None, ())]

    @pytest.mark.parametrize("text", [
        "かわいいペット",                                     # 会話の中の片仮名の語
        "マッケーさんに言われると思うんですけど ターンとリバーをこの犬を隠すように出してほしい",
        "はい、トップ!",
        "はい、フローク",
        "トーン",
        "ペットですか?",                                     # 確認型（疑問形）
        # 音の近さで読んだ 4 拍以下の語は、前後が区切りのときだけ読む（辞書の語と違い「です」も区切りにしない）
        "ホールドする", "ペットです",
    ])
    def test_not_actions(self, text):
        assert parse_actions(text) == []

    def test_amounts_take_precedence(self):
        assert _parsed("センゴヒャクテン") == [("bet", 1500, None, ("amount_only",))]

    def test_dictionary_words_are_kept_when_the_near_word_is_ambiguous(self):
        assert _parsed("フォールド、ゴールド") == [("fold", 0, None, ())]

    def test_description(self):
        assert "音の近さで読んだ" in describe_events(parse_actions("ここまでのヘッドゾップです"))


class TestRecord:
    def test_a_near_word_is_recorded_for_review(self, tmp_path):
        pytest.importorskip("pokerkit")
        from tests.test_spoken_amounts import _Table

        tb = _Table(tmp_path)
        tb.deal()
        tb.say("コール コール チェック")
        tb.say("ペット600")
        (bet,) = [a for a in tb.actions if a.action == "bet"]
        assert bet.amount == 600 and bet.needs_review and "fuzzy_keyword" in bet.reason
        assert bet.raw_text == "ペット600"
