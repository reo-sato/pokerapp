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
    "ベッド": "bet",          # Whisper が「ベット」を「ベッド」と書き起こす（店舗の実測 2026-09-25, ADR-0061）
    "ベト": "bet",            # 「ベット なな」を「ベトナナ」と書き起こした（店舗の実測 2026-09-25, ADR-0062）
    "bet": "bet",
    "コール": "call",
    "ゴール": "call",         # 「レイズ、コール」の「コール」を「ゴール」と書き起こした（同上）
    "call": "call",
    "レイズ": "raise",
    "raise": "raise",
    "リレイズ": "raise",
    "スリーベット": "raise",
    "チェックレイズ": "raise",  # 一度チェックした人のレイズ = 1 アクション（「チェック」「レイズ」に分けない）
    "チェック": "check",
    "check": "check",
    "フォールド": "fold",
    "フォルド": "fold",       # 促音落ちの誤認識ゆらぎ
    "ホールド": "fold",       # 続けて言った 2 つ目の「フォールド」を Whisper がこう書き起こした（店舗の実測 2026-09-25, ADR-0061）
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
    # ショーダウンで残った全員が手札を見せた = 手札とボードで勝者を決めてよい（ADR-0062）
    "ハンド終了": "end_hand",
}

# ショーダウンでディーラーが言う **勝った役の名前**（言い方 → pokerkit の役名）。この店では「ウィナー」は
# 言わず、勝ったプレイヤーの役名を言う（オーナー, 2026-09-26）。役名 = ハンドの終わり（「ハンド終了」と同じ）で、
# 手札とボードの判定と突き合わせる（違えば要確認、片方の手札が読めていなければ勝者を決める材料）。
# 同じ位置では長い語が勝つ（「ストレートフラッシュ」⊃「ストレート」「フラッシュ」）。
HAND_NAME_KEYWORDS: dict[str, str] = {
    "ロイヤルストレートフラッシュ": "Straight flush",
    "ロイヤルフラッシュ": "Straight flush",
    "ストレートフラッシュ": "Straight flush",
    "フォーカード": "Four of a kind",
    "フォーオブアカインド": "Four of a kind",
    "クワッズ": "Four of a kind",
    "4カード": "Four of a kind",
    "フルハウス": "Full house",
    "フラッシュ": "Flush",
    "ストレート": "Straight",
    "スリーカード": "Three of a kind",
    "スリーオブアカインド": "Three of a kind",
    "トリップス": "Three of a kind",
    "3カード": "Three of a kind",
    "ツーペア": "Two pair",
    "トゥーペア": "Two pair",
    "2ペア": "Two pair",
    "ワンペア": "One pair",
    "1ペア": "One pair",
    "ハイカード": "High card",
    "ノーペア": "High card",
}
ACTION_KEYWORDS.update({keyword: "end_hand" for keyword in HAND_NAME_KEYWORDS})

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
