"""tools/count_penalties.py — 真のアクションと観測の対応づけ（台帳 F の F1）と数え上げ（F2）。"""
from __future__ import annotations

import math

import pytest

from tools.count_penalties import (Token, align_words, count_all, count_readings, logistic_fit, match_kind,
                                   street_cards)

T0 = 1_000_000.0


def _opts(t: float, *readings: tuple[str, list[tuple[str, int]]]):
    return (T0 + t, [(source, [Token(T0 + t, i, a, m) for i, (a, m) in enumerate(keys)]) for source, keys in readings])


ROWS = [
    {"street": "preflop", "seat": 5, "action": "raise", "amount": 700},
    {"street": "preflop", "seat": 6, "action": "call", "amount": 700},
    {"street": "preflop", "seat": 7, "action": "fold", "amount": 0},
    {"street": "flop", "seat": 5, "action": "check", "amount": 0},
    {"street": "flop", "seat": 6, "action": "bet", "amount": 900},
]


class TestMatchKind:
    @pytest.mark.parametrize("row, tok, kind", [
        (("raise", 700), ("bet", 700), "exact"),      # 額だけの語は「ベット」と読む
        (("raise", 700), ("raise", 0), "exact"),      # 額を言わなかった
        (("bet", 900), ("bet", 100), "amount"),
        (("allin", 25000), ("allin", 0), "exact"),    # オールインは額を比べない
        (("check", 0), ("call", 0), "sub"),
        (("fold", 0), ("call", 0), None),
    ])
    def test_kinds(self, row, tok, kind):
        assert match_kind({"action": row[0], "amount": row[1]}, Token(0.0, 0, *tok)) == kind


class TestAlignWords:
    def test_monotone_with_unspoken_rows_and_extra_words(self):
        utts = [_opts(5, ("whisper", [("bet", 700)])),
                _opts(8, ("whisper", [("call", 0)])),
                _opts(9, ("whisper", [("bet", 100)])),        # 余計な語（雑談の額）
                _opts(30, ("whisper", [("check", 0)])),
                _opts(34, ("whisper", [("bet", 900)]))]
        words, extra, _chosen = align_words(ROWS, utts, {"flop": T0 + 20}, T0)
        assert [w and (w["action"], round(w["utterance"] - T0)) for w in words] == [
            ("bet", 5), ("call", 8), None, ("check", 30), ("bet", 34)]   # フォールドは言われない
        assert [(x["action"], x["amount"]) for x in extra] == [("bet", 100)]

    def test_a_word_before_the_street_card_is_not_that_streets(self):
        # フロップの札（20 秒）の 3 秒より前の「チェック」はフロップのチェックにしない
        utts = [_opts(5, ("whisper", [("bet", 700), ("call", 0), ("fold", 0)])), _opts(12, ("whisper", [("check", 0)]))]
        words, extra, _ = align_words(ROWS, utts, {"flop": T0 + 20}, T0)
        assert words[3] is None and [x["action"] for x in extra] == ["check"]

    def test_the_reading_that_explains_more_rows_is_chosen(self):
        utts = [_opts(5, ("drop", []), ("ear", [("bet", 700)]), ("ear", [("bet", 7000)]))]
        words, _extra, chosen = align_words(ROWS[:1], utts, {}, T0)
        assert words[0]["source"] == "ear" and words[0]["kind"] == "exact" and chosen[0]["source"] == "ear"

    def test_whisper_wins_a_tie(self):
        # 開発データ 845da8e6#1: 雑談の読み直し（ライブの規則で 500）と、続けて言った「レイズ 500」
        utts = [_opts(5, ("rescue", [("bet", 700)])), _opts(7, ("whisper", [("raise", 700)]))]
        words, extra, _ = align_words(ROWS[:1], utts, {}, T0)
        assert words[0]["source"] == "whisper" and [x["source"] for x in extra] == ["rescue"]

    def test_ear_candidates_only_match_exactly(self):
        # 開発データ 9db1e032#4: 雑音の発話の第 2 の耳の「bet 100」を、真のベット 900 の聞き違いにしない
        utts = [_opts(34, ("drop", []), ("ear", [("bet", 100)]))]
        words, _extra, _ = align_words(ROWS[4:], utts, {"flop": T0 + 20}, T0)
        assert words == [None]

    def test_check_around_explains_the_checks(self):
        rows = [{"street": "flop", "seat": s, "action": "check", "amount": 0} for s in (5, 6, 7)]
        utts = [_opts(30, ("whisper", [("check_around", 0)]))]
        words, _extra, _ = align_words(rows, utts, {"flop": T0 + 20}, T0)
        assert [w["kind"] for w in words] == ["around"] * 3


def test_street_cards_by_time_not_position():
    # 開発データ 845da8e6#1: 4 枚目の札がフロップの札より先に読めた
    hand = {"board_timeline": [{"index": 1, "dealt_at": "2026-10-14T18:00:02.9"},
                               {"index": 2, "dealt_at": "2026-10-14T18:00:03.5"},
                               {"index": 4, "dealt_at": "2026-10-14T17:59:57.0"},
                               {"index": 5, "dealt_at": "2026-10-14T18:00:16.5"}]}
    cards = street_cards(hand)
    assert set(cards) == {"flop", "turn"} and cards["turn"] - cards["flop"] == pytest.approx(19.5)


def test_count_unspoken_and_departures():
    a = {"data": "店舗（実卓）", "seats": 3, "start": T0, "extra": [], "truth_button": None,
         "departures": [{"seat": 6, "t": T0 + 3, "back": T0 + 9, "kind": "lift"}],
         "rows": [{"street": "preflop", "seat": 4, "action": "raise", "amount": 600, "departure": None,
                   "word": {"utterance": T0 + 2, "source": "whisper", "kind": "exact"}},
                  {"street": "preflop", "seat": 5, "action": "fold", "amount": 0, "departure": T0 + 5.2,
                   "word": {"utterance": T0 + 5, "source": "whisper", "kind": "exact"}},
                  {"street": "preflop", "seat": 6, "action": "fold", "amount": 0, "departure": T0 + 8, "word": None}]}
    res = count_all([a])["開発データ / 全部"]
    assert (res["p_miss_fold"]["x"], res["p_miss_fold"]["n"]) == (0, 1)
    assert (res["p_miss_last_fold"]["x"], res["p_miss_last_fold"]["n"]) == (1, 1)   # 最後のフォールドは言われない
    assert res["fold_lag"]["n"] == 1 and res["fold_lag"]["median"] == pytest.approx(0.2)
    assert res["lift_sec"]["lifts"] == 1


def test_chosen_reading_counts_the_matched_words():
    utts = [_opts(5, ("whisper", [("call", 0)]), ("ear", [("bet", 700)]))]
    _words, _extra, chosen = align_words(ROWS[:1], utts, {}, T0)
    assert chosen[0]["option"] == 1 and chosen[0]["matched"] == 1


def test_count_readings():
    def utt(option, matched, *opts):
        return {"option": option, "matched": matched,
                "options": [{"source": s, "logp": lp, "tokens": n} for s, lp, n in opts]}
    a = {"utterances": [utt(0, 1, ("whisper", 0.0, 1), ("ear", -2.0, 1), ("drop", -4.0, 0)),
                        utt(1, 1, ("whisper", 0.0, 1), ("ear", -2.0, 1)),
                        utt(0, 0, ("whisper", 0.0, 1), ("drop", -4.0, 0))]}
    res = count_readings([a])
    assert (res["既定: whisper"]["x"], res["既定: whisper"]["n"]) == (1, 3)
    assert (res["ear"]["x"], res["ear"]["n"]) == (1, 2)
    assert (res["捨てる"]["x"], res["捨てる"]["n"]) == (1, 2)       # 何も説明しなかった発話の「捨てる」が当たり
    assert res["ear"]["model"] == pytest.approx(math.exp(-2) / (1 + math.exp(-2) + math.exp(-4)) / 2
                                                + math.exp(-2) / (1 + math.exp(-2)) / 2)


def test_logistic_fit_recovers_the_slope():
    import random

    rng = random.Random(3)
    xs = [rng.uniform(-6, 2) for _ in range(3000)]
    ys = [int(rng.random() < 1 / (1 + math.exp(-(0.5 + 1.0 * x)))) for x in xs]
    a, b = logistic_fit(xs, ys)
    assert a == pytest.approx(0.5, abs=0.2) and b == pytest.approx(1.0, abs=0.15)


@pytest.mark.slow
def test_dev_hand_845da8e6_1():
    """開発データで目で確かめたハンド（ボードの位置の入れ替わり・雑談の読み直し・チェックの言い直し）。"""
    from tools.count_penalties import all_alignments

    (_name, a), = all_alignments(only={"845da8e6#1"})
    got = [(r.street, r.seat, r.action, r.word and (r.word["source"], round(r.word["utterance"] - a.start, 1)))
           for r in a.rows]
    assert got == [("preflop", 7, "raise", ("whisper", 7.3)), ("preflop", 8, "fold", ("whisper", 8.5)),
                   ("preflop", 5, "fold", ("short", 14.9)), ("preflop", 6, "call", ("whisper", 17.1)),
                   ("flop", 6, "check", ("whisper", 38.5)), ("flop", 7, "check", ("whisper", 38.5)),
                   ("turn", 6, "bet", ("whisper", 59.2)), ("turn", 7, "raise", ("whisper", 62.2)),
                   ("turn", 6, "fold", ("whisper", 65.1))]
    assert a.rows[-1].departure is not None
