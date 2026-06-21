from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from core.constants import (
    ACTION_KEYWORDS,
    KANJI_DIGIT,
    KANJI_UNIT,
    WHISPER_PROMPT_JA,
)
from core.events import AudioEvent

if TYPE_CHECKING:
    from core.engine_types import LegalContext

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


# 明示発話された席番号を抽出する（"シート3" / "seat 3" / 全角数字対応）。
_SEAT_NO_PATTERN = re.compile(r"(?:シート|seat)\s*([0-9０-９]+)", re.IGNORECASE)
_FW_TO_ASCII_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")


def _extract_seat_no(text: str) -> Optional[int]:
    """発話テキストから明示的な席番号 (1..9) を返す。見つからない/範囲外は None。

    actor 推定 (R3) ではなく「明示的に読み上げられた席」だけを拾う additive 仕様。
    """
    m = _SEAT_NO_PATTERN.search(text)
    if not m:
        return None
    try:
        n = int(m.group(1).translate(_FW_TO_ASCII_DIGITS))
    except ValueError:
        return None
    return n if 1 <= n <= 8 else None  # v2 計画書 (2026-06-22): 席数 8


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


def parse_action(text: str, confidence: Optional[float] = None) -> Optional[AudioEvent]:
    """Whisper の認識テキストからアクション種別と金額を抽出して AudioEvent を返す。
    認識できない場合は None を返す。

    confidence: Whisper per-segment 信頼度 [0,1]（呼び出し側が ASR から渡す）。additive。

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
        seat=_extract_seat_no(text),
        confidence=confidence,
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
        """PCM16 音声バイト列をテキストに変換して返す（信頼度を捨てる後方互換版）。"""
        return self.transcribe_with_confidence(audio_bytes)[0]

    def transcribe_with_confidence(
        self, audio_bytes: bytes
    ) -> tuple[str, Optional[float]]:
        """PCM16 音声バイト列を (テキスト, 信頼度[0,1]) に変換する。
        変換失敗・モデル未ロード時は ("", None) を返す（クラッシュしない）。

        入力は 16kHz モノラル PCM16 固定を前提とする（faster-whisper は配列長から
        16kHz を仮定するため sample_rate は受け取らない）。
        confidence は各 segment の avg_logprob（対数確率）平均を exp で 0..1 に
        写像したもの。segment が無ければ None。
        """
        if self._model is None:
            return "", None
        try:
            import math

            import numpy as np

            audio_array = (
                np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
            )
            segments, _ = self._model.transcribe(
                audio_array,
                language=self._language,
                initial_prompt=WHISPER_PROMPT_JA,
            )
            texts: list[str] = []
            logprobs: list[float] = []
            for seg in segments:
                texts.append(seg.text.strip())
                lp = getattr(seg, "avg_logprob", None)
                if lp is not None:
                    logprobs.append(lp)
            text = " ".join(texts)
            confidence: Optional[float] = None
            if logprobs:
                mean_lp = sum(logprobs) / len(logprobs)
                # avg_logprob は対数確率(≤0)。exp で 0..1 の信頼度へ写像。
                confidence = max(0.0, min(1.0, math.exp(mean_lp)))
            return text, confidence
        except Exception:
            logger.exception("Whisper transcription failed")
            return "", None


# ――― R3: 合法手への射影（apply_corrections, ADR-0009 §5）―――

_BETTING_ACTIONS = frozenset({"fold", "check", "call", "bet", "raise", "allin"})


@dataclass
class Correction:
    """apply_corrections の結果（合法手へ射影済みのアクション）。"""

    action: str                          # 射影後アクション
    amount: int                          # bet/raise は "to" 総額、call は call 額、他 0
    needs_review: bool
    corrected_from: Optional[str] = None  # 修復前の生 ASR action（変化なしなら None）
    reason: str = ""                      # 監査用の短い理由
    asr_confidence: Optional[float] = None  # 入力 Whisper 信頼度を下流へ持ち越す


def _snap_to_legal(a: int, lo: int, hi: int) -> tuple[int, int]:
    """heard 額 a を合法レンジ [lo, hi] に丸め、(snapped, gap) を返す。gap は移動量(絶対値)。

    ブラインド単位の round-number 寄せ（§5）は LegalContext に blind 情報が無いため D1 では
    行わず、決定的な clamp のみ。round 寄せは後続（blind を渡せる形に拡張時）。
    """
    if hi <= 0:  # raise/bet レンジ無し（呼び出し側で弾く前提だが安全側）
        return max(a, 0), 0
    if a < lo:
        return lo, lo - a
    if a > hi:
        return hi, a - hi
    return a, 0


def apply_corrections(
    action: str,
    amount: int,
    ctx: "LegalContext",
    whisper_conf: Optional[float] = None,
) -> Correction:
    """raw ASR (action, amount) を legal_ctx の合法手へ射影する純関数（ADR-0009 §5）。

    ゲーム状態を持たず、engine が `legal_context()` を渡して呼ぶ（recognizer を状態から疎結合に保つ）。
    **call/check は状態から決定的に一意化**する（heard キーワードの曖昧さに依存しない）= PHH/JSON で
    call と check を初めて区別できる核心。修復表は `docs/contracts/hand-reconstruction.md §5`。

    Args:
        action/amount: parse_action 由来の生 ASR。
        ctx: `legal_context()`（actor_seat / legal_actions / amount_to_call=c / min_raise=m(to) / max_raise=s(to)）。
        whisper_conf: ASR 信頼度。D1 では結果へ持ち越すのみ（融合は D3 §6）。
    """
    def mk(act: str, amt: int, review: bool,
           corrected_from: Optional[str] = None, reason: str = "") -> Correction:
        return Correction(act, amt, review, corrected_from, reason, whisper_conf)

    a = action.lower()
    # 制御アクション/未知（new_hand/winner/showdown 等）は射影対象外。そのまま通す。
    if a not in _BETTING_ACTIONS:
        return mk(action, amount, False)

    legal = ctx.legal_actions
    # 合法手プリオールが無い（手番でない/ハンド終了）→ 検証不能、flag。
    if not legal:
        return mk(a, amount, True, reason="no_legal_context")

    c = ctx.amount_to_call
    m = ctx.min_raise        # "to" 総額（raise/bet 不可なら 0）
    s = ctx.max_raise        # all-in "to" 総額（raise/bet 不可なら 0）

    if a == "fold":
        return mk("fold", 0, False)

    if a == "allin":
        # engine.apply_action("allin") が max-raise / call-all-in を再解釈する。to 総額を埋める。
        return mk("allin", s if s > 0 else c, False)

    if a in ("check", "call"):
        if c == 0:
            # チェック可。"call" と言っていても実質チェック（call 不要）。
            review = a == "call"
            return mk("check", 0, review,
                      corrected_from="call" if review else None,
                      reason="heard_call_but_check" if review else "")
        # c > 0
        if a == "call":
            return mk("call", c, False)  # heard 額は無視し engine の call 額を採用
        # heard "check" だが call 額あり → 非合法。call/fold へ（曖昧）。
        # call vs fold の尤度（chip-motion 等）は ISSUE-0009 / §8。保守的に call + review
        # （プレイヤーを勝手に hand から外さない側を既定）。
        if "call" in legal:
            return mk("call", c, True, corrected_from="check", reason="check_facing_bet")
        return mk("fold", 0, True, corrected_from="check", reason="check_illegal_fold")

    # bet / raise
    can_raise = "raise" in legal or "bet" in legal
    if not can_raise:
        if "call" in legal:
            return mk("call", c, True, corrected_from=a, reason=f"{a}_illegal_to_call")
        return mk("fold", 0, True, corrected_from=a, reason=f"{a}_illegal_to_fold")

    # legal_context は当ストリートに bet があれば "raise"、無ければ "bet" を出す（排他）。
    # heard が状態と食い違えば state 側へ再マップ（bet↔raise を決定的に正す）。
    target = "raise" if "raise" in legal else "bet"
    corrected_from = a if target != a else None
    amt, gap = _snap_to_legal(amount, m, s)
    if amount <= 0:
        review, reason = True, "no_amount_heard"
    elif gap > m:  # min-raise を超える移動 = 大幅 snap
        review, reason = True, "amount_snapped"
    else:
        review, reason = False, (f"{a}_to_{target}" if corrected_from else "")
    return mk(target, amt, review, corrected_from=corrected_from, reason=reason)
