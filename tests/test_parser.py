# tests/test_parser.py
import json
import os
import tempfile

from audio.recognizer import parse_amount, parse_action, apply_corrections
import audio.recognizer as _rec_mod

def test_parse_amount_basic_numbers():
    assert parse_amount("レイズ 800") == 800
    assert parse_amount("レイズ 1,200 です") == 1200

def test_parse_amount_k_and_man():
    assert parse_amount("レイズ 5K") == 5000
    assert parse_amount("レイズ 3万") == 30000
    assert parse_amount("レイズ 1万2千") == 12000
    assert parse_amount("レイズ 五千") == 5000

def test_parse_action_basic():
    ev = parse_action("シート3 レイズ 800")
    assert ev is not None
    assert ev.action == "raise"
    assert ev.amount == 800

    ev = parse_action("シート2 コール")
    assert ev is not None
    assert ev.action == "call"
    assert ev.amount == 0

def test_seat_prefix_ja_raise():
    """席番号（日本語）が amount に混入しないこと。"""
    ev = parse_action("シート1 レイズ 800")
    assert ev is not None
    assert ev.action == "raise"
    assert ev.amount == 800

def test_seat_prefix_ja_call():
    """席番号のみで金額なし → amount=0。"""
    ev = parse_action("シート2 コール")
    assert ev is not None
    assert ev.action == "call"
    assert ev.amount == 0

def test_seat_prefix_en_raise():
    """席番号（英語）が amount に混入しないこと。"""
    ev = parse_action("seat 3 raise 1200")
    assert ev is not None
    assert ev.action == "raise"
    assert ev.amount == 1200


# ── corrections.json テスト ───────────────────────────────────────────────────

def _with_corrections(mapping: dict, fn):
    """テスト用: 一時 corrections.json を作成して fn() を呼び、キャッシュをリセットする。"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(mapping, f, ensure_ascii=False)
        tmp_path = f.name
    orig_path = _rec_mod._CORRECTIONS_PATH
    orig_mtime = _rec_mod._corrections_mtime
    orig_cache = _rec_mod._corrections_cache.copy()
    orig_sorted = list(_rec_mod._corrections_sorted)
    try:
        _rec_mod._CORRECTIONS_PATH = tmp_path
        _rec_mod._corrections_mtime = 0.0  # force reload
        _rec_mod._corrections_cache = {}
        _rec_mod._corrections_sorted = []
        return fn()
    finally:
        _rec_mod._CORRECTIONS_PATH = orig_path
        _rec_mod._corrections_mtime = orig_mtime
        _rec_mod._corrections_cache = orig_cache
        _rec_mod._corrections_sorted = orig_sorted
        os.unlink(tmp_path)


def test_apply_corrections_basic():
    """corrections.json の置換が適用されること。"""
    result = _with_corrections({"ベッド": "ベット"}, lambda: apply_corrections("ベッド"))
    assert result == "ベット"


def test_apply_corrections_no_file():
    """ファイルなしでもクラッシュせず元のテキストを返すこと。"""
    orig_path = _rec_mod._CORRECTIONS_PATH
    orig_mtime = _rec_mod._corrections_mtime
    orig_cache = _rec_mod._corrections_cache.copy()
    orig_sorted = list(_rec_mod._corrections_sorted)
    try:
        _rec_mod._CORRECTIONS_PATH = "/nonexistent/corrections.json"
        _rec_mod._corrections_mtime = 0.0
        _rec_mod._corrections_cache = {}
        _rec_mod._corrections_sorted = []
        assert apply_corrections("ベッド") == "ベッド"
    finally:
        _rec_mod._CORRECTIONS_PATH = orig_path
        _rec_mod._corrections_mtime = orig_mtime
        _rec_mod._corrections_cache = orig_cache
        _rec_mod._corrections_sorted = orig_sorted


def test_apply_corrections_longest_match_first():
    """長いキーが短いキーに先行して適用されること（元のテキストで重複しない例）。

    "コール" (4文字) と "コ" (1文字) が同じ入力に含まれるとき、
    長い "コール" を先に置換することで "コ" の部分マッチを防ぐ。
    """
    # longest-first: "コール" → "call" (長いほうが先に置換される)
    #   → 結果 "call" に "コ" は存在しないので二回目の置換は起きない
    # shortest-first: "コ" → "X" になり "Xール" → "コール" にマッチせず → "Xール"
    mapping = {"コール": "call", "コ": "XMARK"}
    result = _with_corrections(mapping, lambda: apply_corrections("コール"))
    assert result == "call"


def test_parse_action_after_correction():
    """corrections.json で補正された後にアクション認識が成功すること。"""
    def _run():
        ev = parse_action("ベッド 1000")
        assert ev is not None
        assert ev.action == "bet"
        assert ev.amount == 1000
    _with_corrections({"ベッド": "ベット"}, _run)


def test_parse_action_correction_with_amount():
    """補正 + 金額認識が組み合わさること。"""
    def _run():
        ev = parse_action("ベッズ 5K")
        assert ev is not None
        assert ev.action == "bet"
        assert ev.amount == 5000
    _with_corrections({"ベッズ": "ベット"}, _run)
