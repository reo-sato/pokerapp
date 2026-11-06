"""tests/test_store_1105_phrases.py

11/05 の推定の見直し（オーナーのメモ, 2026-11-06）で分かった店の言い方の読み:

- 書き起こしゆれ「レイザー」= レイズ（ハンド 23）・「メット」= ベット（ハンド 62）。額はその賭けに付く。
- 促しのせりふ「コールが 400、レイズ 800 になります」「レイズは 1600 点からになります」（ハンド 4）と声かけ
  「ナイスオールイン」「ナイスベット」（ハンド 6）はアクションにしない。
- オールインのあとに額を数えて言った声は、ほかの人のレイズではなくオールインの額の確かめ（推定器, ハンド 40 など）。
"""
from __future__ import annotations

import pytest

from audio.recognizer import parse_actions


def _read(text: str) -> list[tuple[str, int]]:
    return [(e.action, e.amount or 0) for e in parse_actions(text)]


@pytest.mark.parametrize("text, expected", [
    ("フォールド 1600点レイザー入ってます。フォールド", [("fold", 0), ("raise", 1600), ("fold", 0)]),
    ("メット 7500 フォールド", [("bet", 7500), ("fold", 0)]),
    ("1600点のレイズ入ってます", [("raise", 1600)]),
    ("ヘルメット", []),
])
def test_store_spellings(text, expected):
    assert _read(text) == expected


@pytest.mark.parametrize("text", [
    "コールが400、レイズ800になります。",
    "コールが四百になります",
    "レイズは1600点からになります。",
    "レイザー千六百点からになります",
    "六百がBBになります",
    "ナイスオールイン",
    "ナイスベットです",
])
def test_prompts_and_compliments_are_not_actions(text):
    assert _read(text) == []


@pytest.mark.parametrize("text, expected", [
    ("フォールド、ナイスオールインです。", [("fold", 0)]),
    ("フォールド、ナイスベッドです。", [("fold", 0)]),
    ("コールが入りました", [("call", 0)]),               # 宣言は読む
    ("ベットが2000点入ってます", [("bet", 2000)]),
    ("コール2000点になります", [("call", 2000)]),
])
def test_declarations_around_them_are_read(text, expected):
    assert _read(text) == expected


def test_an_amount_after_an_allin_restates_it():
    from core.events import AudioEvent
    from integration.estimator import _restates, _Row

    allin_word = AudioEvent(timestamp=100.0, raw_text="オールイン", action="allin", amount=0,
                            utterance_start_ts=100.0)
    rows = [_Row(street="flop", seat=7, action="allin", t=100.0, word=allin_word)]
    count = AudioEvent(timestamp=130.0, raw_text="4万8200点です。", action="bet", amount=48200,
                       utterance_start_ts=130.0, parse_flags=("amount_only",))
    assert _restates(count, rows)
    late = AudioEvent(timestamp=200.0, raw_text="5万点。", action="bet", amount=50000, utterance_start_ts=200.0)
    assert not _restates(late, rows)


def test_allin_call_after_an_allin_is_a_recap():
    """前にオールインがあるハンドの「オールインコール」の「オールイン」はまとめ（オーナー 2026-11-06）。"""
    from core.events import AudioEvent
    from integration.estimator import _allin_call_recap, _Row

    first = AudioEvent(timestamp=10.0, raw_text="オールイン", action="allin", amount=0, utterance_start_ts=10.0)
    words = [AudioEvent(timestamp=30.0, raw_text="オールインコール", action=a, amount=0, utterance_start_ts=30.0)
             for a in ("allin", "call")]
    rows = [_Row(street="preflop", seat=3, action="allin", t=10.0, word=first)]
    assert _allin_call_recap(words[0], words, rows)
    assert not _allin_call_recap(words[0], words, [])                 # 前にオールインが無い = 2 行
    assert not _allin_call_recap(words[0], words[:1], rows)           # 「コール」が続かない
