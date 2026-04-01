from __future__ import annotations

import pytest

from audio.recognizer import _kanji_to_int, parse_action, parse_amount


class TestKanjiToInt:
    def test_single_digit(self):
        assert _kanji_to_int("五") == 5

    def test_sen(self):
        assert _kanji_to_int("五千") == 5000

    def test_man(self):
        assert _kanji_to_int("三万") == 30000

    def test_man_sen(self):
        assert _kanji_to_int("一万二千") == 12000

    def test_man_sen_hyaku(self):
        assert _kanji_to_int("一万二千三百") == 12300

    def test_ichi_man(self):
        assert _kanji_to_int("一万") == 10000

    def test_juu(self):
        assert _kanji_to_int("二十") == 20

    def test_empty(self):
        assert _kanji_to_int("") == 0


class TestParseAmount:
    # 算用数字のみ
    def test_plain_digits(self):
        assert parse_amount("800") == 800

    def test_large_digits(self):
        assert parse_amount("12000") == 12000

    # カンマ区切り
    def test_comma_separated(self):
        assert parse_amount("1,200") == 1200

    def test_comma_large(self):
        assert parse_amount("10,000") == 10000

    # K 単位
    def test_k_unit_upper(self):
        assert parse_amount("5K") == 5000

    def test_k_unit_lower(self):
        assert parse_amount("12k") == 12000

    # 万 単位
    def test_man_unit(self):
        assert parse_amount("3万") == 30000

    # 混合: 算用数字 + 漢字単位
    def test_man_sen_mixed(self):
        assert parse_amount("1万2千") == 12000

    def test_two_man_five_sen(self):
        assert parse_amount("2万5千") == 25000

    # 漢数字のみ
    def test_kanji_only_sen(self):
        assert parse_amount("五千") == 5000

    def test_kanji_only_man_sen(self):
        assert parse_amount("一万二千") == 12000

    def test_kanji_only_man_sen_hyaku(self):
        assert parse_amount("一万二千三百") == 12300

    # テキスト中に埋め込まれた金額
    def test_embedded_in_sentence(self):
        assert parse_amount("レイズ 2400") == 2400

    def test_allin_sentence(self):
        assert parse_amount("オールイン 12000") == 12000

    # 金額なし
    def test_no_amount(self):
        assert parse_amount("コール") == 0

    def test_empty_string(self):
        assert parse_amount("") == 0

    # 左から右に走査: 最初の金額を採用
    def test_leftmost_match(self):
        # "800 と 1200" → 最左の 800 を返す
        assert parse_amount("800 と 1200") == 800


class TestParseAction:
    def test_bet_with_amount(self):
        event = parse_action("ベット 800")
        assert event is not None
        assert event.action == "bet"
        assert event.amount == 800

    def test_raise_with_amount(self):
        event = parse_action("レイズ 2400")
        assert event is not None
        assert event.action == "raise"
        assert event.amount == 2400

    def test_call_no_amount(self):
        event = parse_action("コール")
        assert event is not None
        assert event.action == "call"
        assert event.amount == 0

    def test_check(self):
        event = parse_action("チェック")
        assert event is not None
        assert event.action == "check"
        assert event.amount == 0

    def test_fold(self):
        event = parse_action("フォールド")
        assert event is not None
        assert event.action == "fold"
        assert event.amount == 0

    def test_allin(self):
        event = parse_action("オールイン 12000")
        assert event is not None
        assert event.action == "allin"
        assert event.amount == 12000

    def test_showdown(self):
        event = parse_action("ショーダウン")
        assert event is not None
        assert event.action == "showdown"

    def test_winner(self):
        event = parse_action("シート3 ウィナー")
        assert event is not None
        assert event.action == "winner"

    def test_new_hand(self):
        event = parse_action("ハンド開始")
        assert event is not None
        assert event.action == "new_hand"

    def test_english_bet(self):
        event = parse_action("bet 500")
        assert event is not None
        assert event.action == "bet"
        assert event.amount == 500

    def test_english_raise(self):
        event = parse_action("raise 1500")
        assert event is not None
        assert event.action == "raise"
        assert event.amount == 1500

    def test_unknown_text(self):
        assert parse_action("何も関係ない文章") is None

    def test_raw_text_preserved(self):
        event = parse_action("レイズ 800")
        assert event is not None
        assert event.raw_text == "レイズ 800"

    def test_kanji_amount(self):
        event = parse_action("レイズ 一万二千")
        assert event is not None
        assert event.action == "raise"
        assert event.amount == 12000
