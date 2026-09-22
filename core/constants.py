from __future__ import annotations

# Whisper の initial_prompt。キーワードの箇条列挙はオウム返し型ハルシネーションの温床になるため、
# 自然文コンテキストに語彙を埋め込む（ADR-C / T4）。無音時のガードは recorder 側の有音ゲートが担う。
WHISPER_PROMPT_JA = (
    "ポーカーのディーラーがアクションを読み上げています。"
    "シート3 レイズ 2千、コール、チェック、フォールド、オールイン、"
    "ショーダウン、ウィナー、ハンド開始、チョップ などの言葉が含まれます。"
)

# アクションキーワード → 正規化アクション名
# 部分文字列マッチ（最左・同位置は長い方優先）で照合される。複合語（例: スリーベット）は
# 内包する短い語（ベット）より左からマッチするため、span 重複は選択語が勝つ。
ACTION_KEYWORDS: dict[str, str] = {
    "ベット": "bet",
    "bet": "bet",
    "コール": "call",
    "call": "call",
    "レイズ": "raise",
    "raise": "raise",
    "リレイズ": "raise",
    "スリーベット": "raise",
    "チェック": "check",
    "check": "check",
    "フォールド": "fold",
    "フォルド": "fold",       # 促音落ちの誤認識ゆらぎ
    "fold": "fold",
    "降ります": "fold",
    "マック": "fold",          # muck
    "オールイン": "allin",
    "all in": "allin",
    "all-in": "allin",
    "allin": "allin",
    "ショーダウン": "showdown",
    "showdown": "showdown",
    "ウィナー": "winner",
    "wins": "winner",
    "winner": "winner",
    "チョップ": "winner",      # split pot（席が複数読み上げられる, ADR-D）
    "スプリット": "winner",
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
