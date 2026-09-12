"""tests/test_recognizer_amounts.py

日本語金額解析の直接ユニットテスト (review hardening)。

parse_amount / _kanji_to_int / _strip_seat_references / _extract_seat_no は
誤ると額面がハンド履歴全体を汚す中核経路だが、test_parser.py は基本形しか
カバーしていなかったため、エッジケースをここで固定する。
"""
from __future__ import annotations

import pytest

from audio.recognizer import (
    _extract_seat_no,
    _kanji_to_int,
    _strip_seat_references,
    parse_action,
    parse_amount,
)


class TestKanjiToInt:
    @pytest.mark.parametrize("kanji,expected", [
        ("五千", 5000),
        ("二千五百", 2500),
        ("千五百", 1500),          # 先頭単位（digit 省略 = 1）
        ("一万二千三百", 12300),
        ("三万", 30000),
        ("十", 10),
        ("百", 100),
        ("二十五", 25),
        ("万", 10000),             # 「万」単独 = 1万
        ("一万五百", 10500),       # 万の右に百のみ
    ])
    def test_values(self, kanji: str, expected: int):
        assert _kanji_to_int(kanji) == expected

    def test_empty_returns_zero(self):
        assert _kanji_to_int("") == 0


class TestParseAmountEdges:
    @pytest.mark.parametrize("text,expected", [
        ("レイズ 2万5千", 25000),       # 混合 万+千
        ("ベット 5k", 5000),            # 小文字 k
        ("ベット 1,200,000", 1200000),  # 複数カンマ
        ("コール ８００", 800),          # 全角数字（\\d は Unicode 数字に一致）
        ("レイズ 二千五百", 2500),       # 漢数字のみ
        ("チェック", 0),                # 金額なし
        ("", 0),
    ])
    def test_values(self, text: str, expected: int):
        assert parse_amount(text) == expected

    def test_leftmost_amount_wins(self):
        # 言い直しがあっても最左を採用（決定的）
        assert parse_amount("800 いや 1000") == 800

    def test_man_preferred_over_bare_digit_at_same_pos(self):
        # 同位置マッチは大きい値（より具体的な表現）を優先
        assert parse_amount("1万") == 10000


class TestSeatStripping:
    def test_strip_seat_ja(self):
        assert _strip_seat_references("シート1 レイズ 800").strip() == "レイズ 800"

    def test_strip_seat_fullwidth(self):
        assert "８００" in _strip_seat_references("シート３ レイズ ８００")
        assert "シート" not in _strip_seat_references("シート３ レイズ ８００")

    def test_strip_seat_en(self):
        assert _strip_seat_references("seat 3 raise 1200").strip() == "raise 1200"


class TestExtractSeatNo:
    @pytest.mark.parametrize("text,expected", [
        ("シート3 コール", 3),
        ("シート３ コール", 3),     # 全角
        ("seat 7 fold", 7),
        ("コール 500", None),       # 席言及なし
        ("シート0 コール", None),   # 範囲外 (1..9)
        ("シート12 コール", None),  # 範囲外
    ])
    def test_values(self, text: str, expected):
        assert _extract_seat_no(text) == expected


class TestParseActionAmountInteraction:
    def test_seat_digit_not_taken_as_amount_fullwidth(self):
        ev = parse_action("シート３ レイズ ８００")
        assert ev is not None
        assert ev.action == "raise"
        assert ev.amount == 800
        assert ev.seat == 3

    def test_call_with_kanji_amount(self):
        ev = parse_action("シート2 コール 五百")
        assert ev is not None
        assert ev.action == "call"
        assert ev.amount == 500
