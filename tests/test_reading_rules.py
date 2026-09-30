"""tests/test_reading_rules.py

読み取りの直しを、見つけた言い方ごとの例外ではなく一般的な規則にした（2026-09-30, オーナー「読み取りの直しについて、
一般化できますか？」）。店舗・台本で見つけた言い方と、まだ出ていない同じ種類の言い方を並べて確かめる。

1. 語の区切りは元の文字の種類で見る: 片仮名の語の隣のひらがな（助詞・「です」「する」）は区切り。
   前は「を」「です」だけを区切りにしていた（「フォールドをホールド」「オーリンです」）。
2. 続けて言う 2 語のアクション（チェックアラウンド・チェックレイズ）の後ろの語は音の近さで読む。
   前は「アランド」「アウンド」「ラウンド」「ランド」を並べていた。
3. どの額がどのアクションのものかを 1 つの表で決める（`_AMOUNT_OWNERSHIP`）: フォールド・チェック・ヘッズアップは
   額を持たない、コールは続けて言った額だけ、ベット・レイズ・オールインは語のあとの最初の額（無ければ前）。
   ほかの額は別の人の賭け。前は「区切った額」「空白」「フォールド600」を別々に直していた。
"""
from __future__ import annotations

import logging

import pytest

from audio.phonetic import sounds_like
from audio.recognizer import parse_actions


@pytest.fixture(autouse=True)
def _quiet():
    logging.disable(logging.WARNING)
    yield
    logging.disable(logging.NOTSET)


def _read(text: str) -> list[tuple]:
    return [(e.action, e.amount) + (("around",) if "check_around" in e.parse_flags else ())
            for e in parse_actions(text)]


class TestWordBoundaryByScript:
    @pytest.mark.parametrize("text, expected", [
        ("フォールドをホールド", [("fold", 0), ("fold", 0)]),       # 台本 2026-09-30
        ("オーリンです", [("allin", 0)]),                         # 店舗 2026-09-29
        ("フォールドとホールド", [("fold", 0), ("fold", 0)]),
        ("コールとゴール", [("call", 0), ("call", 0)]),
        ("ゴールします", [("call", 0)]),
        ("ホールドで", [("fold", 0)]),
        ("オーリンでした", [("allin", 0)]),
    ])
    def test_hiragana_next_to_a_katakana_word_is_a_boundary(self, text, expected):
        assert _read(text) == expected

    @pytest.mark.parametrize("text", ["ゴールド", "ホールドデスク", "なべとなって", "ベトナム", "ゴールドのチップ"])
    def test_a_short_word_inside_a_longer_word_is_not_an_action(self, text):
        assert _read(text) == []

    def test_all_hiragana_input_still_uses_desu_and_wo(self):
        assert _read("ほーるどです") == [("fold", 0)]
        assert _read("ふぉーるどをほーるど") == [("fold", 0), ("fold", 0)]


class TestCheckAroundBySound:
    @pytest.mark.parametrize("text", [
        "チェックアラウンド", "チェック アラウンド",
        "チェック、アランド", "チェックアウンド", "チッカーランド",        # 店舗 2026-09-27
        "チェックランド", "チェック ランド",                           # 店舗（書き起こし）
        "チェックラウンド",                                          # 台本 2026-09-30
        "チェック、アワンド", "チェックアラン", "チェック、ラウンズ",       # まだ出ていない言い方
        "check around",
    ])
    def test_check_around(self, text):
        assert _read(text) == [("check", 0, "around")]

    @pytest.mark.parametrize("text", [
        "チェック", "チェック、ターンカード", "チェック、ターン", "チェック、ラストカード", "チェック、ハンド",
    ])
    def test_a_street_word_after_check_is_not_around(self, text):
        assert _read(text) == [("check", 0)]

    def test_the_word_after_check_is_compared_as_said_after_check(self):
        """「チェック」の「ク」に続く「アラウンド」の「ア」は消えやすい（「ランド」= アラウンド、「ハンド」ではない）。"""
        assert sounds_like("ランド", "アラウンド", after="チェック")
        assert not sounds_like("ランド", "アラウンド")
        assert not sounds_like("ハンド", "アラウンド", after="チェック")


class TestCheckRaiseBySound:
    @pytest.mark.parametrize("text, expected", [
        ("チェックレイズ 600", [("raise", 600)]),
        ("チェックレーズ 2千", [("raise", 2000)]),        # 読み上げ集 2026-09-30
        ("チェックレース 2千", [("raise", 2000)]),        # まだ出ていない言い方（前はチェック + 額が消えていた）
        ("チェックレイス 1800", [("raise", 1800)]),
    ])
    def test_check_raise_said_as_one_word(self, text, expected):
        assert _read(text) == expected

    def test_separated_check_and_raise_are_two_players(self):
        assert _read("チェック、レイズ 600") == [("check", 0), ("raise", 600)]

    @pytest.mark.parametrize("tail", ["リスト", "レート", "ライズ", "リレイズ"])
    def test_other_words_after_check_are_not_raise(self, tail):
        assert not sounds_like(tail, "レイズ", after="チェック")


class TestAmountOwnership:
    @pytest.mark.parametrize("text, expected", [
        # フォールド・チェック・ヘッズアップは額を持たない（額は次の人の賭け）
        ("フォールド600", [("fold", 0), ("bet", 600)]),
        ("600フォールド", [("bet", 600), ("fold", 0)]),
        ("チェック、800", [("check", 0), ("bet", 800)]),
        ("ヘッズアップ、600", [("heads_up", 0), ("bet", 600)]),
        ("ヘッドアップ 1200", [("heads_up", 0), ("bet", 1200)]),
        # コールは続けて言った額だけ（コールの額の言い直し）
        ("600点コールです", [("call", 600)]),
        ("コール600", [("call", 600)]),
        ("コール 3000", [("call", 0), ("bet", 3000)]),
        ("2千点、コール", [("bet", 2000), ("call", 0)]),
        # ベット・レイズ・オールインは語のあとの最初の額、無ければ前の最後の額
        ("レイズ 1800", [("raise", 1800)]),
        ("1800点レイズ", [("raise", 1800)]),
        ("2千点、ベット", [("bet", 2000)]),
        ("600、レイズ 1800", [("bet", 600), ("raise", 1800)]),
        ("2千点レイズ、6千", [("bet", 2000), ("raise", 6000)]),
        ("3000、オールイン 5000", [("bet", 3000), ("allin", 5000)]),
        # 自分の額と同じ額は言い直し
        ("レイズ 1800、1800です", [("raise", 1800)]),
        ("コール600、600", [("call", 600)]),
    ])
    def test_which_amount_belongs_to_the_action(self, text, expected):
        assert [(action, amount) for action, amount, *_ in _read(text)] == expected

    def test_the_seat_goes_with_its_own_part(self):
        events = parse_actions("コール シート4 600")
        assert [(e.action, e.amount, e.seat) for e in events] == [("call", 0, None), ("bet", 600, 4)]

    def test_a_number_that_is_not_an_amount_is_not_the_raise(self):
        (raise_,) = parse_actions("3人、レイズ 1800")
        assert (raise_.action, raise_.amount) == ("raise", 1800)

    def test_the_heard_text_keeps_its_spacing(self):
        first, second = parse_actions("シート2 レイズ 600、シート5 1800")
        assert (first.raw_text, second.raw_text) == ("シート2 レイズ 600", "シート5 1800")

    def test_control_words_keep_their_amounts(self):
        """勝者・ハンドの終わりの額（ポットの読み上げなど）は別の賭けにしない。"""
        assert [e.action for e in parse_actions("シート3 ウィナー 1万2000")] == ["winner"]
