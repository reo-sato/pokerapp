"""tests/test_corpus_0930_round2.py

読み上げ集 2 回目（2026-09-30 20:55, Kei, 店の言い方に作り直した 135 句）で読めていなかった書き起こし。

- 「チェックアラウンド」の音のゆれ（「チェッカーランド」）: 音の近さでは読めていたのに、「チェック」だけが語で
  残りの「アラウンド」がアクションでない言葉に数えられ、読まなかった。店舗 2026-09-29 の実卓（9d1d8536）にも出ていた。
- 速く言って伸ばす音が落ちた語（「コル コル」「ホルドフォルドコル」）。
- 「フォープレイヤーズ」を「フォールプレイヤー」（読み上げ集・台本 16:50 の 2 回）。
- 自信のとても低い Whisper の読みを、第 2 の耳がアクションを何も聞いていなければ捨てる（句の前の「あ」→「コール」
  0.06、無音 →「コール」0.10、「はい」→「オールイン」0.12）。
"""
from __future__ import annotations

import pytest

from audio.recognizer import parse_actions
from audio.second_ear import EAR_HEARD_NOTHING, EAR_VETO_CONFIDENCE, apply_ear, wants_ear
from tools.read_corpus import event_key


def _keys(text: str) -> list[str]:
    return [event_key(e) for e in parse_actions(text)]


class TestCheckAround:
    @pytest.mark.parametrize("text", ["チェッカーランド", "チェッカランド", "チェッカラウンド", "チェッカランの"])
    def test_sound_variants(self, text):
        events = parse_actions(text)
        assert [event_key(e) for e in events] == ["check_around"]
        assert "fuzzy_keyword" in events[0].parse_flags          # 音の近さで読んだ = 要確認

    @pytest.mark.parametrize("text, expected", [
        ("チェックアラウンド", ["check_around"]), ("チェック アラウンド", ["check_around"]),
        ("チェックアラウンド、ラストカード", ["check_around"]), ("チェック、チェックアラウンド", ["check", "check_around"]),
        ("チェック", ["check"]), ("チェックレイズ 2千", ["raise 2000"]),
    ])
    def test_unchanged(self, text, expected):
        assert _keys(text) == expected


class TestClippedLongVowel:
    @pytest.mark.parametrize("text, expected", [
        ("コル コル", ["call", "call"]), ("コルコル", ["call", "call"]), ("コル、コル", ["call", "call"]),
        ("ホルドフォルドコル", ["fold", "fold", "call"]), ("フォルドフォルドホルド", ["fold", "fold", "fold"]),
        ("コールオリン", ["call", "allin"]), ("コルです", ["call"]),
    ])
    def test_read(self, text, expected):
        assert _keys(text) == expected

    @pytest.mark.parametrize("text", [
        "起こる", "何が起こる？", "こる", "こるこる",            # ひらがなの語（「起こる」）は読まない
        "ゴルフ", "コルク", "オリンピック", "ホルダー",           # ほかの言葉の一部
    ])
    def test_not_read(self, text):
        assert _keys(text) == []


class TestPlayersLeftBySound:
    @pytest.mark.parametrize("text, expected", [
        ("フォールプレイヤー", ["players_left 4"]), ("コール、フォールプレイヤー", ["call", "players_left 4"]),
        ("フォープレイヤーズ", ["players_left 4"]), ("シックスプレイヤー", ["players_left 6"]),
        ("三プレーヤーです", ["players_left 3"]),
    ])
    def test_read(self, text, expected):
        assert _keys(text) == expected

    @pytest.mark.parametrize("text", [
        "ナイスプレイヤー", "エースプレイヤー", "いいプレイヤー", "トッププレイヤー", "ラッキープレイヤー",
        "ボタンプレイヤー", "プレイヤー", "スリープディーラー",
    ])
    def test_not_read(self, text):
        assert _keys(text) == []


def _whisper(text: str, confidence: float) -> list:
    return parse_actions(text, confidence=confidence)


class TestLowConfidenceVeto:
    def test_the_ear_heard_no_action(self):
        events = _whisper("コール", 0.058)
        assert wants_ear(events, "コール")
        assert apply_ear(events, "コール", {"text": "あ", "candidates": [{"text": "二千", "logp": -9.0}]}) == ([], "あ")

    def test_the_ear_heard_nothing(self):
        events = _whisper("コール", 0.1)
        assert apply_ear(events, "コール", {"text": "", "candidates": []}) == ([], EAR_HEARD_NOTHING)

    def test_the_ear_heard_an_action(self):
        events = _whisper("コール", 0.1)
        kept, used = apply_ear(events, "コール", {"text": "こる", "candidates": [{"text": "コール", "logp": -1.0}]})
        assert [e.action for e in kept] == ["call"] and used is None

    def test_normal_confidence_is_not_heard_again(self):
        events = _whisper("コール", EAR_VETO_CONFIDENCE + 0.01)
        assert not wants_ear(events, "コール")
        assert apply_ear(events, "コール", {"text": "あ", "candidates": []}) == (events, None)

    def test_without_a_confidence_nothing_changes(self):
        events = parse_actions("コール")
        assert not wants_ear(events, "コール")

    def test_the_corpus_reread_uses_the_saved_confidence(self):
        from tools.read_corpus import reread_segment

        seg = {"text": "コール", "confidence": 0.058, "ear": {"text": "あ", "candidates": []}}
        assert reread_segment(seg, "live") == [] and reread_segment(seg, "whisper") == ["call"]
