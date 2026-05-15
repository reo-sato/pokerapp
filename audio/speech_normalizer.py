"""audio/speech_normalizer.py

Whisper 認識テキストから action / amount / seat を抜き出して構造化する。

既存の audio.recognizer.parse_action / parse_amount を内部利用しつつ、
action が欠落する場合 (金額のみ) も拾えるように拡張している。

返り値 NormalizedSpeech は大文字の action ラベル (BET/CALL/RAISE/...) を
使用する。これは betting_state / action_inference と共通の語彙。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from audio.recognizer import _strip_seat_references, parse_amount
from core.constants import ACTION_KEYWORDS

logger = logging.getLogger(__name__)

# 内部 (lowercase) → 外部 (uppercase) 表記の変換
_ACTION_UP = {
    "bet": "BET",
    "call": "CALL",
    "raise": "RAISE",
    "check": "CHECK",
    "fold": "FOLD",
    "allin": "ALLIN",
    "showdown": "SHOWDOWN",
    "winner": "WINNER",
    "new_hand": "NEW_HAND",
}

_SEAT_NUMERIC = re.compile(r"(?:シート|seat)\s*([0-9０-９]+)", re.IGNORECASE)
_SEAT_KANJI = re.compile(r"シート\s*([一二三四五六七八九十]+)")

_KANJI_DIGIT_SIMPLE = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}


@dataclass
class NormalizedSpeech:
    """音声認識テキストの構造化結果。

    どのフィールドも欠落しうる (None / 空)。これを state-aware な
    action_inference に渡して最終解釈する。
    """

    action: Optional[str]       # BET/CALL/RAISE/CHECK/FOLD/ALLIN/SHOWDOWN/WINNER/NEW_HAND or None
    amount: Optional[int]
    seat: Optional[int]
    raw_text: str
    normalized_text: str
    matched_rules: list[str] = field(default_factory=list)


def _parse_seat(text: str) -> Optional[int]:
    """テキスト中の席番号を抽出。例: "シート3" "seat 4" "シート三"。"""
    m = _SEAT_NUMERIC.search(text)
    if m:
        try:
            return int(_normalize_digits(m.group(1)))
        except ValueError:
            return None
    m = _SEAT_KANJI.search(text)
    if m:
        return _KANJI_DIGIT_SIMPLE.get(m.group(1))
    return None


def _normalize_digits(s: str) -> str:
    """全角数字を半角に変換。"""
    table = str.maketrans("０１２３４５６７８９", "0123456789")
    return s.translate(table)


def _find_action_in_text(text: str) -> tuple[Optional[str], int, list[str]]:
    """テキスト中の action キーワードを (action_uppercase, position, rules) で返す。

    audio.recognizer.parse_action と同じ「最左優先・同位置なら長い方優先」ルール。
    見つからなければ (None, -1, [])。
    """
    lower = text.lower()
    found_action: Optional[str] = None
    found_pos = len(text)
    found_kw_len = 0
    matched_keyword: Optional[str] = None

    for keyword, action in ACTION_KEYWORDS.items():
        pos = lower.find(keyword.lower())
        if pos == -1:
            continue
        kw_len = len(keyword)
        if pos < found_pos or (pos == found_pos and kw_len > found_kw_len):
            found_pos = pos
            found_action = action
            found_kw_len = kw_len
            matched_keyword = keyword

    if found_action is None:
        return None, -1, []
    return _ACTION_UP.get(found_action, found_action.upper()), found_pos, [
        f"action_keyword:{matched_keyword}"
    ]


def normalize_speech(raw_text: str) -> NormalizedSpeech:
    """raw_text を解析し、構造化された NormalizedSpeech を返す。

    認識ロジック:
      1. 全角数字を半角に置換した正規化テキストを生成。
      2. テキスト中の action キーワードを最左マッチで検出。
      3. 席番号 (シート3 / seat 4 / シート三) を抽出。
      4. 席番号表現を除いたテキストから金額を抽出。0 のときは「金額なし」と判断。
    """
    normalized_text = _normalize_digits(raw_text)
    matched_rules: list[str] = []

    action, _, rules = _find_action_in_text(normalized_text)
    matched_rules.extend(rules)

    seat = _parse_seat(normalized_text)
    if seat is not None:
        matched_rules.append(f"seat:{seat}")

    amount_raw = parse_amount(_strip_seat_references(normalized_text))
    amount: Optional[int] = amount_raw if amount_raw > 0 else None
    if amount is not None:
        matched_rules.append(f"amount:{amount}")

    logger.debug(
        "normalize_speech: raw=%r → action=%s amount=%s seat=%s rules=%s",
        raw_text, action, amount, seat, matched_rules,
    )
    return NormalizedSpeech(
        action=action,
        amount=amount,
        seat=seat,
        raw_text=raw_text,
        normalized_text=normalized_text,
        matched_rules=matched_rules,
    )
