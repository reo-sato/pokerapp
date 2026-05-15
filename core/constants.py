from __future__ import annotations

WHISPER_PROMPT_JA = (
    "ベット コール レイズ チェック フォールド オールイン ショーダウン ウィナー ハンド開始"
)

# アクションキーワード → 正規化アクション名
ACTION_KEYWORDS: dict[str, str] = {
    "ベット": "bet",
    "bet": "bet",
    "コール": "call",
    "call": "call",
    "レイズ": "raise",
    "上げる": "raise",
    "上げ": "raise",
    "raise": "raise",
    "チェック": "check",
    "check": "check",
    "フォールド": "fold",
    "降りる": "fold",
    "ダウン": "fold",
    "fold": "fold",
    "オールイン": "allin",
    "全部": "allin",
    "all in": "allin",
    "all-in": "allin",
    "allin": "allin",
    "ショーダウン": "showdown",
    "showdown": "showdown",
    "ウィナー": "winner",
    "wins": "winner",
    "winner": "winner",
    "ハンド開始": "new_hand",
    "new hand": "new_hand",
}

# 漢数字 → 数値
KANJI_DIGIT: dict[str, int] = {
    "〇": 0,
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}

KANJI_UNIT: dict[str, int] = {
    "十": 10,
    "百": 100,
    "千": 1000,
    "万": 10000,
}

KANJI_ALL: frozenset[str] = frozenset(KANJI_DIGIT) | frozenset(KANJI_UNIT)

# ストリートの順序（advance_street の検証用）
STREET_ORDER = ("preflop", "flop", "turn", "river", "showdown")
