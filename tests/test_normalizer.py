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


# ── ひらがな数詞パーサ単体テスト ──────────────────────────────────────────────

from audio.speech_normalizer import _hira_to_int, _find_number_candidates


class TestHiraToInt:
    def test_roku_hyaku(self):
        assert _hira_to_int("ろくひゃく") == 600

    def test_sen_ni_hyaku(self):
        assert _hira_to_int("せんにひゃく") == 1200

    def test_ni_sen(self):
        assert _hira_to_int("にせん") == 2000

    def test_go_hyaku(self):
        assert _hira_to_int("ごひゃく") == 500

    def test_hyaku_alone(self):
        assert _hira_to_int("ひゃく") == 100

    def test_sen_alone(self):
        assert _hira_to_int("せん") == 1000

    def test_issen(self):
        assert _hira_to_int("いっせん") == 1000

    def test_roku_ppyaku(self):
        assert _hira_to_int("ろっぴゃく") == 600

    def test_hap_pyaku(self):
        assert _hira_to_int("はっぴゃく") == 800

    def test_san_byaku(self):
        assert _hira_to_int("さんびゃく") == 300

    def test_go_sen(self):
        assert _hira_to_int("ごせん") == 5000

    def test_ni_sen_go_hyaku(self):
        assert _hira_to_int("にせんごひゃく") == 2500

    def test_digit_only_returns_none(self):
        """単位語なし → amount として不適切なので None。"""
        assert _hira_to_int("に") is None
        assert _hira_to_int("ろく") is None
        assert _hira_to_int("ご") is None

    def test_unknown_char_returns_none(self):
        assert _hira_to_int("あいうえお") is None

    def test_empty_returns_none(self):
        assert _hira_to_int("") is None

    def test_sen_ni_hyaku_spaced(self):
        """スペースが入ると _hira_to_int は None（隣接スパン連結で対処）。"""
        assert _hira_to_int("せん にひゃく") is None


class TestFindNumberCandidates:
    def test_arabic_digit(self):
        cands = _find_number_candidates("BET 600")
        assert any(v == 600 for _, _, v, _ in cands)

    def test_kanji(self):
        cands = _find_number_candidates("BET 六百")
        assert any(v == 600 for _, _, v, _ in cands)

    def test_hira_single(self):
        cands = _find_number_candidates("BET ろくひゃく")
        assert any(v == 600 for _, _, v, _ in cands)

    def test_hira_adjacent_pair(self):
        """スペース区切りひらがな → 隣接スパン連結で認識する。"""
        cands = _find_number_candidates("BET ろく ひゃく")
        assert any(v == 600 for _, _, v, _ in cands)

    def test_spaced_digits(self):
        """1 200 → 1200 候補が出ること。"""
        cands = _find_number_candidates("BET 1 200")
        assert any(v == 1200 for _, _, v, _ in cands)

    def test_k_unit(self):
        cands = _find_number_candidates("BET 5K")
        assert any(v == 5000 for _, _, v, _ in cands)

    def test_comma_digit(self):
        cands = _find_number_candidates("RAISE 1,200")
        assert any(v == 1200 for _, _, v, _ in cands)

    def test_no_overlap(self):
        """六百 と 百 で重複しないこと（六百が採用されるべき）。"""
        cands = _find_number_candidates("BET 六百")
        vals = [v for _, _, v, _ in cands]
        assert 600 in vals
        assert 100 not in vals  # 百は六百に含まれるのでスキップ


# ── 数値正規化統合テスト ──────────────────────────────────────────────────────

class TestNumberNormalization:
    """SpeechNormalizer.normalize() の amount / amount_candidates テスト。"""

    def test_arabic_bet(self, normalizer):
        r = normalizer.normalize("ベッド 600")
        assert r.action == "BET"
        assert r.amount == 600

    def test_arabic_raise(self, normalizer):
        r = normalizer.normalize("例 1200")
        assert r.action == "RAISE"
        assert r.amount == 1200

    def test_kanji_bet(self, normalizer):
        r = normalizer.normalize("ベッド 六百")
        assert r.action == "BET"
        assert r.amount == 600

    def test_hira_bet(self, normalizer):
        r = normalizer.normalize("ベッド ろくひゃく")
        assert r.action == "BET"
        assert r.amount == 600

    def test_hira_spaced_bet(self, normalizer):
        r = normalizer.normalize("ベッド ろく ひゃく")
        assert r.action == "BET"
        assert r.amount == 600

    def test_hira_senni_raise(self, normalizer):
        r = normalizer.normalize("例 せんにひゃく")
        assert r.action == "RAISE"
        assert r.amount == 1200

    def test_spaced_digit_bet(self, normalizer):
        r = normalizer.normalize("ベッド 1 200")
        assert r.action == "BET"
        assert r.amount == 1200

    def test_k_unit_raise(self, normalizer):
        r = normalizer.normalize("例 5K")
        assert r.action == "RAISE"
        assert r.amount == 5000

    def test_seat_does_not_pollute_amount(self, normalizer):
        """席番号の数字が amount に混入しないこと。"""
        r = normalizer.normalize("シート2 例 2000")
        assert r.action == "RAISE"
        assert r.amount == 2000

    def test_call_has_no_amount(self, normalizer):
        r = normalizer.normalize("コール")
        assert r.action == "CALL"
        assert r.amount is None

    def test_check_has_no_amount(self, normalizer):
        r = normalizer.normalize("チェック")
        assert r.action == "CHECK"
        assert r.amount is None

    def test_normalized_text_has_arabic(self, normalizer):
        """normalized_text 内の数字表現がアラビア数字に変換されること。"""
        r = normalizer.normalize("ベッド 六百")
        assert "600" in r.normalized_text

    def test_hira_normalized_text(self, normalizer):
        r = normalizer.normalize("ベッド ろくひゃく")
        assert "600" in r.normalized_text

    def test_amount_candidates_populated(self, normalizer):
        r = normalizer.normalize("ベッド 600")
        assert 600 in r.amount_candidates

    def test_no_number_bet_amount_zero(self, normalizer):
        """BET で数字が認識されない場合 → amount は None（integartion で review）。"""
        r = normalizer.normalize("ベッド")
        assert r.action == "BET"
        assert r.amount is None


class TestParseActionWithAmount:
    """parse_action() が normalizer の amount を優先すること。"""

    def test_hira_amount_used(self, normalizer):
        ev = _with_module_normalizer(normalizer, lambda: parse_action("ベッド ろくひゃく"))
        assert ev is not None
        assert ev.action == "bet"
        assert ev.amount == 600

    def test_kanji_amount_used(self, normalizer):
        ev = _with_module_normalizer(normalizer, lambda: parse_action("例 千二百"))
        assert ev is not None
        assert ev.action == "raise"
        assert ev.amount == 1200

    def test_seat_and_amount(self, normalizer):
        ev = _with_module_normalizer(normalizer, lambda: parse_action("シート2 レイズ 2000"))
        assert ev is not None
        assert ev.action == "raise"
        assert ev.amount == 2000

    def test_call_amount_zero(self, normalizer):
        """CALL は amount が取れなくても 0 であること（parse_amount フォールバック）。"""
        ev = _with_module_normalizer(normalizer, lambda: parse_action("コール"))
        assert ev is not None
        assert ev.action == "call"
        assert ev.amount == 0
