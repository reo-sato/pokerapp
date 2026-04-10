from __future__ import annotations

import datetime
import logging
import re
from typing import Optional

from core.constants import (
    ACTION_KEYWORDS,
    KANJI_ALL,
    KANJI_DIGIT,
    KANJI_UNIT,
    POSITION_KEYWORDS,
    WHISPER_PROMPT_JA,
)
from core.events import AudioEvent
from core.rule_engine import ActionType

logger = logging.getLogger(__name__)

# ── 正規表現パターン（すべて raw string） ────────────────────────────────────

# 連続漢数字トークン
_KANJI_PATTERN = re.compile(r"[一二三四五六七八九〇十百千万]+")

# 算用数字 + 単位パターン（左から右に試行）
_MIXED_MAN_SEN = re.compile(r"(\d[\d,]*)万(\d+)千")          # 1万2千
_MAN_ONLY      = re.compile(r"(\d[\d,]*)万")                  # 3万
_K_UNIT        = re.compile(r"(\d[\d,]*(?:\.\d+)?)[Kk]")     # 5K, 2.5K
_DIGIT_ONLY    = re.compile(r"\d[\d,]*")                      # 800 / 1,200

# 席番号除去パターン（parse_amount が誤認識しないよう除去する）
_SEAT_STRIP = re.compile(
    r"(?:シート[　 ]*[0-9０-９一二三四五六七八九十]+|seat\s*[0-9]+)",
    re.IGNORECASE,
)

# 席番号抽出パターン
_SEAT_EXTRACT = re.compile(
    r"シート[　 ]*([0-9０-９]+)"
    r"|seat\s*([0-9]+)"
    r"|([0-9]+)番",
    re.IGNORECASE,
)

# 全角数字 → 半角数字の変換テーブル
_FULLWIDTH_TABLE = str.maketrans("０１２３４５６７８９", "0123456789")


def _strip_seat_references(text: str) -> str:
    """席番号表現を除去する（parse_amount の誤認識防止）。"""
    return _SEAT_STRIP.sub("", text)


def _kanji_to_int(kanji: str) -> int:
    """連続した漢数字文字列を整数に変換する。変換できない場合は 0 を返す。"""
    if not kanji:
        return 0
    if "万" in kanji:
        idx = kanji.index("万")
        left  = kanji[:idx]
        right = kanji[idx + 1:]
        left_val  = _kanji_to_int(left)  if left  else 1
        right_val = _kanji_to_int(right) if right else 0
        return left_val * 10000 + right_val

    result = 0
    current_digit = 0
    for ch in kanji:
        if ch in KANJI_DIGIT:
            current_digit = KANJI_DIGIT[ch]
        elif ch in KANJI_UNIT:
            unit = KANJI_UNIT[ch]
            result += (current_digit if current_digit else 1) * unit
            current_digit = 0
        else:
            logger.debug("Unknown kanji character: %s", ch)
            return 0
    result += current_digit
    return result


def parse_amount(text: str) -> int:
    """テキストを左から右に走査し、最初にマッチした金額表現を int で返す。
    見つからなければ 0 を返す。
    """
    candidates: list[tuple[int, int]] = []

    for m in _KANJI_PATTERN.finditer(text):
        val = _kanji_to_int(m.group())
        if val > 0:
            candidates.append((m.start(), val))

    for m in _MIXED_MAN_SEN.finditer(text):
        man = int(m.group(1).replace(",", ""))
        sen = int(m.group(2))
        candidates.append((m.start(), man * 10000 + sen * 1000))

    for m in _MAN_ONLY.finditer(text):
        val = int(m.group(1).replace(",", "")) * 10000
        candidates.append((m.start(), val))

    for m in _K_UNIT.finditer(text):
        # 小数対応: 2.5K → 2500
        val = round(float(m.group(1).replace(",", "")) * 1000)
        candidates.append((m.start(), val))

    for m in _DIGIT_ONLY.finditer(text):
        val = int(m.group().replace(",", ""))
        candidates.append((m.start(), val))

    if not candidates:
        return 0

    # 最左優先。同位置なら大きい値を優先（より具体的な表現を採用）
    candidates.sort(key=lambda x: (x[0], -x[1]))
    return candidates[0][1]


# ── 公開 extract 関数（spec.md FR-19–21） ────────────────────────────────────

def extract_action(text: str) -> Optional[ActionType]:
    """テキストから最初に検出されたポーカーアクションを ActionType で返す。

    winner / showdown / new_hand などフェーズイベントは ActionType に含まれないため
    None を返す。
    """
    lower = text.lower()
    found_action: Optional[str] = None
    found_pos = len(text)
    found_kw_len = 0

    for keyword, action_str in ACTION_KEYWORDS.items():
        pos = lower.find(keyword.lower())
        if pos == -1:
            continue
        kw_len = len(keyword)
        if pos < found_pos or (pos == found_pos and kw_len > found_kw_len):
            found_pos = pos
            found_action = action_str
            found_kw_len = kw_len

    if found_action is None:
        return None
    try:
        return ActionType(found_action)
    except ValueError:
        # "winner", "showdown", "new_hand" は ActionType に含まれない
        return None


def extract_amount(text: str) -> Optional[int]:
    """テキストから金額を抽出して返す。金額が見つからない場合は None を返す。

    漢数字（二千五百）・K 表記（2.5K）・万表記（3万）に対応。
    席番号の数字は除去してから解析する。
    """
    val = parse_amount(_strip_seat_references(text))
    return val if val > 0 else None


def extract_seat(text: str) -> Optional[int]:
    """テキストから席番号を抽出して返す（FR-26）。

    対応フォーマット: 「シート3」「シート２」「seat 3」「3番」
    """
    m = _SEAT_EXTRACT.search(text)
    if not m:
        return None
    val = next((g for g in m.groups() if g is not None), None)
    if val is None:
        return None
    val = val.translate(_FULLWIDTH_TABLE)
    return int(val)


def extract_position(text: str) -> Optional[str]:
    """テキストからポジション言及を抽出して正規化ポジション名を返す（FR-26）。

    対応: 「BTN」「ボタン」「SB」「スモールブラインド」「BB」「UTG」「CO」「HJ」「LJ」等
    より長いキーワードを優先（例: "スモールブラインド" > "SB"）。
    """
    text_lower = text.lower()
    for keyword in sorted(POSITION_KEYWORDS, key=len, reverse=True):
        if keyword.lower() in text_lower:
            return POSITION_KEYWORDS[keyword]
    return None


# ── parse_action（IntegrationThread 向け一括パース） ─────────────────────────

def parse_action(text: str) -> Optional[AudioEvent]:
    """Whisper 認識テキストからアクション・金額・席・ポジションを抽出して
    AudioEvent を返す。認識できない場合は None を返す。

    キーワード選択ルール（最左優先・同位置なら長いキーワードを優先）。
    """
    lower = text.lower()
    found_action: Optional[str] = None
    found_pos = len(text)
    found_kw_len = 0

    for keyword, action_str in ACTION_KEYWORDS.items():
        pos = lower.find(keyword.lower())
        if pos == -1:
            continue
        kw_len = len(keyword)
        if pos < found_pos or (pos == found_pos and kw_len > found_kw_len):
            found_pos = pos
            found_action = action_str
            found_kw_len = kw_len

    if found_action is None:
        logger.debug("No action keyword found in: %r", text)
        return None

    amount = parse_amount(_strip_seat_references(text))

    return AudioEvent(
        action=found_action,
        amount=amount if amount > 0 else None,
        timestamp=datetime.datetime.now().isoformat(),
        raw_text=text,
        mentioned_seat=extract_seat(text),
        mentioned_position=extract_position(text),
    )


class WhisperTranscriber:
    """faster-whisper を使ってマイク音声をテキストに変換するクラス。"""

    def __init__(self, model_size: str = "medium", language: str = "ja") -> None:
        self._language = language
        logger.info("Loading Whisper model: %s", model_size)
        try:
            from faster_whisper import WhisperModel  # type: ignore[import]
            self._model = WhisperModel(model_size, device="cpu", compute_type="int8")
        except ImportError:
            logger.warning(
                "faster-whisper not installed. WhisperTranscriber will not function."
            )
            self._model = None

    def transcribe(self, audio_bytes: bytes) -> str:
        """PCM16 音声バイト列をテキストに変換して返す。失敗時は空文字列を返す。"""
        if self._model is None:
            return ""
        try:
            import numpy as np
            audio_array = (
                np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
            )
            segments, _ = self._model.transcribe(
                audio_array,
                language=self._language,
                initial_prompt=WHISPER_PROMPT_JA,
            )
            return " ".join(seg.text.strip() for seg in segments)
        except Exception:
            logger.exception("Whisper transcription failed")
            return ""
