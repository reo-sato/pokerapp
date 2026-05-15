from __future__ import annotations

import json
import logging
import os
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
from audio.speech_normalizer import normalize as _normalize_speech

logger = logging.getLogger(__name__)

# ── 誤認識補正テーブル ─────────────────────────────────────────────────────────
# corrections.json: {"誤認識テキスト": "正しいテキスト", ...}
# ファイルが存在しない場合は補正なし。アプリ起動中に編集しても次の認識で自動反映される。
_CORRECTIONS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "corrections.json")
_corrections_cache: dict[str, str] = {}
_corrections_mtime: float = 0.0
# 長いキーから順に並べたリスト（部分マッチ衝突回避のため事前ソート）
_corrections_sorted: list[tuple[str, str]] = []


def _load_corrections() -> None:
    """corrections.json を読み込む。mtime が変わった場合のみ再読み込み（ホットリロード）。"""
    global _corrections_cache, _corrections_mtime, _corrections_sorted
    try:
        mtime = os.stat(_CORRECTIONS_PATH).st_mtime
    except OSError:
        return
    if mtime == _corrections_mtime:
        return
    try:
        with open(_CORRECTIONS_PATH, encoding="utf-8") as f:
            raw: dict = json.load(f)
        _corrections_cache = {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)}
        _corrections_sorted = sorted(_corrections_cache.items(), key=lambda x: -len(x[0]))
        _corrections_mtime = mtime
        logger.info("Loaded %d corrections from %s", len(_corrections_cache), _CORRECTIONS_PATH)
    except Exception:
        logger.exception("Failed to load corrections from %s", _CORRECTIONS_PATH)


def apply_corrections(text: str) -> str:
    """corrections.json の補正テーブルを text に適用して返す。

    長いキーから順に置換することで短いパターンが長いマッチを壊すことを防ぐ。
    ファイルが存在しない場合や補正なしの場合は text をそのまま返す。
    """
    _load_corrections()
    for src, dst in _corrections_sorted:
        text = text.replace(src, dst)
    return text


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
    """ASR テキストからアクション種別と金額を抽出して AudioEvent を返す。
    認識できない場合は None を返す。

    処理順:
      1. apply_corrections() - 既知の誤認識文字列を補正
      2. _normalize_speech()  - action_aliases / seat_aliases で正規化
      3. 正規化で action が確定した場合はそのまま採用
      4. 確定しない場合は ACTION_KEYWORDS による従来のキーワードマッチング

    キーワード選択ルール（フォールバック時）:
      - テキスト内で最も左に現れたキーワードを優先する
      - 同じ開始位置に複数キーワードがある場合は長い方を優先
    """
    original_text = text
    text = apply_corrections(text)

    # ── Speech normalization ────────────────────────────────────────────────
    norm = _normalize_speech(text)
    normalized = norm.normalized_text

    if norm.action is not None:
        # normalizer が action を特定済み: ACTION_KEYWORDS マッチングをスキップ
        action = norm.action.lower()
        amount = parse_amount(_strip_seat_references(normalized))
        return AudioEvent(
            action=action,
            amount=amount,
            timestamp=time.time(),
            raw_text=original_text,
        )

    # ── Fallback: ACTION_KEYWORDS keyword matching ──────────────────────────
    lower = normalized.lower()
    found_action: Optional[str] = None
    found_pos = len(normalized)
    found_kw_len = 0

    for keyword, action in ACTION_KEYWORDS.items():
        pos = lower.find(keyword.lower())
        if pos == -1:
            continue
        kw_len = len(keyword)
        if pos < found_pos or (pos == found_pos and kw_len > found_kw_len):
            found_pos = pos
            found_action = action
            found_kw_len = kw_len

    if found_action is None:
        logger.debug("No action keyword found in: %r (normalized: %r)", original_text, normalized)
        return None

    # 席番号表現（シート1 / seat 3 等）を除去してから金額を抽出する。
    amount = parse_amount(_strip_seat_references(normalized))

    return AudioEvent(
        action=found_action,
        amount=amount,
        timestamp=time.time(),
        raw_text=original_text,
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
