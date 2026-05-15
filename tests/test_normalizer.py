# tests/test_normalizer.py
"""SpeechNormalizer / parse_action 正規化統合のユニットテスト。"""
import json
import os
import tempfile

import pytest

from audio.speech_normalizer import SpeechNormalizer, NormalizedResult
from audio.recognizer import parse_action
import audio.speech_normalizer as _norm_mod


# ── フィクスチャ ──────────────────────────────────────────────────────────────

SAMPLE_DICT = {
    "action_aliases": {
        "ベッド":     "BET",
        "別途":       "BET",
        "別":         "BET",
        "えっと":     "BET",
        "ベット":     "BET",
        "レイズ":     "RAISE",
        "例":         "RAISE",
        "えー":       "RAISE",
        "椅子":       "RAISE",
        "ズ":         "RAISE",
        "コール":     "CALL",
        "ゴール":     "CALL",
        "チェック":   "CHECK",
        "フォールド": "FOLD",
        "オールイン": "ALLIN",
        "全部":       "ALLIN",
    },
    "seat_aliases": {
        "シート1": 1,
        "シート2": 2,
        "シート3": 3,
        "一番": 1,
        "二番": 2,
    },
}


@pytest.fixture
def normalizer(tmp_path):
    """一時ファイルに書いた SAMPLE_DICT で初期化した SpeechNormalizer を返す。"""
    p = tmp_path / "norm.json"
    p.write_text(json.dumps(SAMPLE_DICT, ensure_ascii=False), encoding="utf-8")
    return SpeechNormalizer(str(p))


def _with_module_normalizer(normalizer_instance, fn):
    """モジュールレベルの _normalizer を一時的に差し替えて fn() を実行する。"""
    orig = _norm_mod._normalizer
    _norm_mod._normalizer = normalizer_instance
    try:
        return fn()
    finally:
        _norm_mod._normalizer = orig


# ── SpeechNormalizer 単体テスト ───────────────────────────────────────────────

class TestSpeechNormalizerLoad:
    def test_loads_valid_file(self, normalizer):
        assert len(normalizer._action_aliases) > 0
        assert len(normalizer._seat_aliases) > 0

    def test_missing_file_does_not_raise(self, tmp_path):
        n = SpeechNormalizer(str(tmp_path / "nonexistent.json"))
        assert n._action_aliases == {}
        assert n._seat_aliases == {}

    def test_invalid_json_does_not_raise(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("{invalid json", encoding="utf-8")
        n = SpeechNormalizer(str(p))
        assert n._action_aliases == {}

    def test_corrupted_entry_skipped(self, tmp_path):
        data = {"action_aliases": {"正常": "BET", 123: "BAD"}, "seat_aliases": {}}
        p = tmp_path / "mixed.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        n = SpeechNormalizer(str(p))
        assert "正常" in n._action_aliases
        assert 123 not in n._action_aliases


class TestSpeechNormalizerActionAliases:
    def test_bed_to_bet(self, normalizer):
        r = normalizer.normalize("ベッド 600")
        assert r.action == "BET"
        assert "BET" in r.normalized_text
        assert any("ベッド->BET" in rule for rule in r.matched_rules)

    def test_betsu_to_to_bet(self, normalizer):
        r = normalizer.normalize("別途 1000")
        assert r.action == "BET"

    def test_betsu_to_bet(self, normalizer):
        r = normalizer.normalize("別 800")
        assert r.action == "BET"

    def test_etto_to_bet(self, normalizer):
        r = normalizer.normalize("えっと 500")
        assert r.action == "BET"

    def test_rei_to_raise(self, normalizer):
        r = normalizer.normalize("例 1200")
        assert r.action == "RAISE"

    def test_ee_to_raise(self, normalizer):
        r = normalizer.normalize("えー 5000")
        assert r.action == "RAISE"

    def test_isu_to_raise(self, normalizer):
        r = normalizer.normalize("椅子 800")
        assert r.action == "RAISE"

    def test_zu_to_raise(self, normalizer):
        r = normalizer.normalize("ズ 600")
        assert r.action == "RAISE"

    def test_goal_to_call(self, normalizer):
        r = normalizer.normalize("ゴール")
        assert r.action == "CALL"

    def test_check(self, normalizer):
        r = normalizer.normalize("チェック")
        assert r.action == "CHECK"

    def test_allin_full(self, normalizer):
        r = normalizer.normalize("オールイン")
        assert r.action == "ALLIN"

    def test_allin_zenbu(self, normalizer):
        r = normalizer.normalize("全部")
        assert r.action == "ALLIN"

    def test_no_match_returns_none_action(self, normalizer):
        r = normalizer.normalize("よくわからない発話")
        assert r.action is None
        assert r.matched_rules == []
        assert r.normalized_text == "よくわからない発話"

    def test_raw_text_preserved(self, normalizer):
        r = normalizer.normalize("ベッド 600")
        assert r.raw_text == "ベッド 600"


class TestSpeechNormalizerSeatAliases:
    def test_seat_number(self, normalizer):
        r = normalizer.normalize("シート2 コール")
        assert r.seat == 2

    def test_ichibanbig_to_seat1(self, normalizer):
        r = normalizer.normalize("一番 ベット 500")
        assert r.seat == 1

    def test_seat_and_action(self, normalizer):
        r = normalizer.normalize("シート2 コール")
        assert r.seat == 2
        assert r.action == "CALL"

    def test_no_seat_returns_none(self, normalizer):
        r = normalizer.normalize("コール")
        assert r.seat is None


class TestSpeechNormalizerTokens:
    def test_tokens_split(self, normalizer):
        r = normalizer.normalize("BET 600")
        assert "600" in r.tokens

    def test_empty_input(self, normalizer):
        r = normalizer.normalize("")
        assert r.action is None
        assert r.normalized_text == ""


# ── parse_action 統合テスト ───────────────────────────────────────────────────

class TestParseActionWithNormalizer:
    def test_bed_recognized_as_bet(self, normalizer):
        ev = _with_module_normalizer(normalizer, lambda: parse_action("ベッド 1000"))
        assert ev is not None
        assert ev.action == "bet"
        assert ev.amount == 1000

    def test_rei_recognized_as_raise(self, normalizer):
        ev = _with_module_normalizer(normalizer, lambda: parse_action("例 1200"))
        assert ev is not None
        assert ev.action == "raise"
        assert ev.amount == 1200

    def test_goal_recognized_as_call(self, normalizer):
        ev = _with_module_normalizer(normalizer, lambda: parse_action("ゴール"))
        assert ev is not None
        assert ev.action == "call"

    def test_etto_500_recognized(self, normalizer):
        ev = _with_module_normalizer(normalizer, lambda: parse_action("えっと 500"))
        assert ev is not None
        assert ev.action == "bet"
        assert ev.amount == 500

    def test_seat2_call(self, normalizer):
        ev = _with_module_normalizer(normalizer, lambda: parse_action("シート2 コール"))
        assert ev is not None
        assert ev.action == "call"

    def test_raw_text_is_original(self, normalizer):
        """AudioEvent.raw_text は補正・正規化前の元テキストであること。"""
        ev = _with_module_normalizer(normalizer, lambda: parse_action("ベッド 600"))
        assert ev is not None
        assert ev.raw_text == "ベッド 600"

    def test_unknown_utterance_returns_none(self, normalizer):
        ev = _with_module_normalizer(normalizer, lambda: parse_action("よくわからない発話"))
        assert ev is None

    def test_fallback_to_keyword_matching(self, normalizer):
        """normalizer が action を特定できなくても既存キーワードマッチングで認識できること。"""
        # "fold" はサンプル辞書に action_aliases として入っていないが ACTION_KEYWORDS にある
        ev = _with_module_normalizer(normalizer, lambda: parse_action("fold"))
        assert ev is not None
        assert ev.action == "fold"

    def test_no_normalizer_still_works(self):
        """_normalizer が None でも parse_action が正常動作すること。"""
        ev = _with_module_normalizer(None, lambda: parse_action("コール"))
        assert ev is not None
        assert ev.action == "call"
