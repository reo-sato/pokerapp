from __future__ import annotations

import logging
import re
import time
from typing import Optional

from core.constants import (
    ACTION_KEYWORDS,
    KANJI_ALL,
    KANJI_DIGIT,
    KANJI_UNIT,
    WHISPER_PROMPT_JA,
)
from core.events import AudioEvent

logger = logging.getLogger(__name__)

# 以下の正規表現はすべて raw string (r"...") で記述する。
# バックスラッシュの二重エスケープや誤解を防ぐためのプロジェクト規約。

# 連続漢数字トークンにマッチする正規表現
_KANJI_PATTERN = re.compile(r"[一二三四五六七八九〇十百千万]+")

# 算用数字 + 単位パターン（左から右に順番に試行）
# 1万2千, 1万, 5K, 1,200, 800 の順に試行
_MIXED_MAN_SEN = re.compile(r"(\d[\d,]*)万(\d+)千")  # 1万2千
_MAN_ONLY      = re.compile(r"(\d[\d,]*)万")          # 3万
_K_UNIT        = re.compile(r"(\d[\d,]*)[Kk]")        # 5K
_DIGIT_ONLY    = re.compile(r"\d[\d,]*")              # 800 / 1,200


# 席番号表現（金額パースの前に除去する）
# 例: "シート1", "シート２", "seat 3"
_SEAT_PATTERN = re.compile(
    r"(?:シート[0-9０-９一二三四五六七八九十]+|seat\s*[0-9]+)",
    re.IGNORECASE,
)


def _strip_seat_references(text: str) -> str:
    """席番号表現をテキストから除去して返す。

    parse_amount() が席番号の数字を金額として誤認識することを防ぐ。
    例: "シート1 レイズ 800" → " レイズ 800"
    """
    return _SEAT_PATTERN.sub("", text)


def _kanji_to_int(kanji: str) -> int:
    """連続した漢数字文字列を整数に変換する。

    桁単位の乗算・累算方式:
        二千五百 → 2×1000 + 5×100 = 2500
        一万二千三百 → 1×10000 + 2×1000 + 3×100 = 12300
        五千 → 5×1000 = 5000

    変換できない場合は 0 を返す。
    """
    if not kanji:
        return 0
    # 万以上の単位で分割してから再帰的に処理する
    # 万 が含まれる場合: 左側×10000 + 右側
    if "万" in kanji:
        idx = kanji.index("万")
        left = kanji[:idx]
        right = kanji[idx + 1 :]
        left_val = _kanji_to_int(left) if left else 1
        right_val = _kanji_to_int(right) if right else 0
        return left_val * 10000 + right_val

    result = 0
    current_digit = 0  # 単位の前にある数字（例: 二千 → current_digit=2）

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

    result += current_digit  # 末尾の数字（単位なし）を加算
    return result


def parse_amount(text: str) -> int:
    """テキストを左から右に走査し、最初にマッチした金額表現を int で返す。
    見つからなければ 0 を返す。

    走査ポリシー:
    - 連続漢数字トークン ([一二三四五六七八九〇十百千万]+) は _kanji_to_int で処理する
    - それ以外（算用数字+単位 / K / カンマ区切り）は regex で処理する
    - テキストを左から右に走査し、最初にマッチした表現を採用する
    """
    # 全パターンの候補を (開始位置, 変換値) として収集し、最左のものを返す
    candidates: list[tuple[int, int]] = []

    # 連続漢数字トークン
    for m in _KANJI_PATTERN.finditer(text):
        val = _kanji_to_int(m.group())
        if val > 0:
            candidates.append((m.start(), val))

    # 1万2千
    for m in _MIXED_MAN_SEN.finditer(text):
        man = int(m.group(1).replace(",", ""))
        sen = int(m.group(2))
        candidates.append((m.start(), man * 10000 + sen * 1000))

    # 3万（1万2千にマッチしなかった箇所）
    for m in _MAN_ONLY.finditer(text):
        # 1万2千 として既にマッチしている範囲はスキップしない（最左判定で自然に解決）
        val = int(m.group(1).replace(",", "")) * 10000
        candidates.append((m.start(), val))

    # 5K
    for m in _K_UNIT.finditer(text):
        val = int(m.group(1).replace(",", "")) * 1000
        candidates.append((m.start(), val))

    # 算用数字のみ（カンマ区切り含む）
    for m in _DIGIT_ONLY.finditer(text):
        val = int(m.group().replace(",", ""))
        candidates.append((m.start(), val))

    if not candidates:
        return 0

    # 最左（開始位置が最小）のものを採用。
    # 同じ位置に複数のパターンがマッチした場合は値が大きい方を優先する。
    # （例: "1万" と "1" が同位置にマッチするとき、より具体的な表現である
    #   "1万"=10000 を採用するため）
    candidates.sort(key=lambda x: (x[0], -x[1]))
    return candidates[0][1]


def parse_action(text: str) -> Optional[AudioEvent]:
    """Whisper の認識テキストからアクション種別と金額を抽出して AudioEvent を返す。
    認識できない場合は None を返す。

    キーワード選択ルール:
    1. テキスト内で最も左に現れたキーワードを優先する。
    2. 同じ開始位置に複数のキーワードがマッチした場合は、より長いキーワードを優先する。
       （例: "all in" と "all" が同位置にマッチ → "all in" を採用）
    """
    lower = text.lower()

    found_action: Optional[str] = None
    found_pos = len(text)
    found_kw_len = 0  # タイブレーク用: 同じ位置なら長い方を優先

    for keyword, action in ACTION_KEYWORDS.items():
        pos = lower.find(keyword.lower())
        if pos == -1:
            continue
        kw_len = len(keyword)
        # 最左優先。同位置なら長いキーワードを優先（より具体的な表現を採用するため）
        if pos < found_pos or (pos == found_pos and kw_len > found_kw_len):
            found_pos = pos
            found_action = action
            found_kw_len = kw_len

    if found_action is None:
        logger.debug("No action keyword found in: %r", text)
        return None

    # 席番号表現（シート1 / seat 3 等）を除去してから金額を抽出する。
    # 除去しないと parse_amount() が席番号の数字を最初の金額候補として拾ってしまう。
    # call/check/fold の金額: Phase 1 では parse_amount() の結果をそのまま使う簡易仕様。
    # （例: "コール 500" → amount=500、"チェック" → amount=0）
    # 精緻化する場合は action ごとに金額の妥当性検証を追加すること。
    amount = parse_amount(_strip_seat_references(text))

    return AudioEvent(
        action=found_action,
        amount=amount,
        timestamp=time.time(),
        raw_text=text,
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
        """PCM16 音声バイト列をテキストに変換して返す。
        変換失敗時は空文字列を返す（クラッシュしない）。

        入力は 16kHz モノラル PCM16 固定を前提とする。
        faster-whisper の transcribe() は numpy 配列の長さから 16kHz を仮定するため、
        sample_rate は引数として受け取らない。
        """
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
