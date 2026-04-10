from __future__ import annotations

from enum import Enum

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
    "raise": "raise",
    "チェック": "check",
    "check": "check",
    "フォールド": "fold",
    "fold": "fold",
    "オールイン": "allin",
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


# ── Street Enum（spec.md v4.0: game_state.py から移動） ─────────────────────
class Street(str, Enum):
    """ポーカーのストリート定義。"""
    PREFLOP  = "preflop"
    FLOP     = "flop"
    TURN     = "turn"
    RIVER    = "river"
    SHOWDOWN = "showdown"


# ── 音声分類定数（spec.md FR-16） ──────────────────────────────────────────

# 確認型（疑問形）判定パターン
QUESTION_PATTERNS: list[str] = ["ですか", "でよろしいですか", "ですか？", "ですよね"]

# 宣言型キーワード（PHASE_EVENT を即発火する発話）
DECLARATORY_KEYWORDS: set[str] = {
    "ハンド開始", "ショーダウン", "ウィナー", "wins", "winner",
    "new hand", "new_hand", "showdown", "ポット",
}

# ── ポジション定数（spec.md FR-05c） ────────────────────────────────────────

# ポジション名（BTN基準。インデックス0=BTN）
POSITION_NAMES: list[str] = ["BTN", "SB", "BB", "UTG", "UTG+1", "UTG+2", "LJ", "HJ", "CO"]

# 音声中のポジション言及 → 正規化ポジション名
POSITION_KEYWORDS: dict[str, str] = {
    "BTN": "BTN",   "ボタン": "BTN",
    "SB": "SB",     "スモールブラインド": "SB",
    "BB": "BB",     "ビッグブラインド": "BB",
    "UTG": "UTG",   "アンダーザガン": "UTG",
    "CO": "CO",     "カットオフ": "CO",
    "HJ": "HJ",     "ハイジャック": "HJ",
    "LJ": "LJ",     "ロージャック": "LJ",
}
