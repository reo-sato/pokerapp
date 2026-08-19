from __future__ import annotations

import logging
import re
import time
import unicodedata
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

# 算用数字 + 単位パターン（すべての候補を収集し、最左・同位置なら大きい値を採用）。
# パースは NFKC 正規化済みテキストに対して行う（全角数字・全角ピリオドは半角化済み）。
_MIXED_MAN_SEN = re.compile(r"(\d[\d,]*)万(\d+)千")            # 1万2千
_MAN_DECIMAL   = re.compile(r"(\d[\d,]*)\.(\d+)万")            # 1.5万 (ADR-A S1)
_MAN_TRAILING  = re.compile(r"(\d[\d,]*)万(\d)(?![\d,.千百十万Kk])")  # 4万2 (曖昧, ADR-A S1)
_MAN_ONLY      = re.compile(r"(\d[\d,]*)万")                   # 3万
_SEN_HYAKU     = re.compile(r"(\d+)千(\d+)百")                 # 2千5百 (ADR-A S1)
_SEN_ONLY      = re.compile(r"(\d+)千")                        # 2千 (ADR-A S1: 従来 2 と誤読)
_HYAKU_ONLY    = re.compile(r"(\d+)百")                        # 5百 (ADR-A S1)
_K_DECIMAL     = re.compile(r"(\d[\d,]*)\.(\d+)[Kk]")          # 1.5K (ADR-A S1)
_K_UNIT        = re.compile(r"(\d[\d,]*)[Kk]")                 # 5K
_DIGIT_ONLY    = re.compile(r"\d[\d,]*")                       # 800 / 1,200


# 席番号表現。strip（金額パース前の除去）と抽出の両方が同一パターンを共有する
# （ADR-A S2: 従来は strip 側が「シート 3」(空白) / 漢数字席を取りこぼし、席番号が金額に流入した）。
_SEAT_PATTERN = re.compile(
    r"(?:シート|seat)\s*([0-9０-９]+|[一二三四五六七八九])",
    re.IGNORECASE,
)
_FW_TO_ASCII_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")


def _strip_seat_references(text: str) -> str:
    """席番号表現をテキストから除去して返す。

    parse_amount() が席番号の数字を金額として誤認識することを防ぐ。
    例: "シート1 レイズ 800" → " レイズ 800"
    """
    return _SEAT_PATTERN.sub("", text)


def _seat_token_to_int(token: str) -> Optional[int]:
    """席番号トークン（半角/全角数字 or 漢数字 1 桁）を int に変換する。"""
    if token in KANJI_DIGIT:
        return KANJI_DIGIT[token]
    try:
        return int(token.translate(_FW_TO_ASCII_DIGITS))
    except ValueError:
        return None


def _extract_seat_no(text: str) -> Optional[int]:
    """発話テキストから明示的な席番号 (1..9) を返す。見つからない/範囲外は None。

    actor 推定 (R3) ではなく「明示的に読み上げられた席」だけを拾う additive 仕様。
    """
    m = _SEAT_PATTERN.search(text)
    if not m:
        return None
    n = _seat_token_to_int(m.group(1))
    return n if n is not None and 1 <= n <= 9 else None


def _extract_all_seat_nos(text: str) -> list[int]:
    """発話テキストから明示的な席番号 (1..9) を出現順に全部返す（重複除去, ADR-D S7）。

    "シート3 シート5 チョップ" のような split-pot 読み上げで複数勝者席を拾う。
    """
    seats: list[int] = []
    for m in _SEAT_PATTERN.finditer(text):
        n = _seat_token_to_int(m.group(1))
        if n is not None and 1 <= n <= 9 and n not in seats:
            seats.append(n)
    return seats


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


def _kanji_amount(kanji: str) -> tuple[int, bool]:
    """漢数字トークンを (金額, 曖昧フラグ) に変換する。

    「四万二」のような「万 + 単位なし 1 桁」は口頭で 42000 の省略形
    （四万二(千)）である可能性が高いが 40002 とも読めるため、
    千単位解釈を採用しつつ ambiguous=True を返す（ADR-A S1）。
    """
    if "万" in kanji:
        idx = kanji.index("万")
        right = kanji[idx + 1 :]
        if len(right) == 1 and right in KANJI_DIGIT and KANJI_DIGIT[right] > 0:
            left_val = _kanji_to_int(kanji[:idx]) or 1
            return left_val * 10000 + KANJI_DIGIT[right] * 1000, True
    return _kanji_to_int(kanji), False


@dataclass(frozen=True)
class AmountParse:
    """parse_amount_ex の結果。ambiguous=True は「N万M」型の桁省略が疑われる読み。"""

    value: int
    ambiguous: bool = False


def parse_amount_ex(text: str) -> AmountParse:
    """テキストを左から右に走査し、最初にマッチした金額表現を返す（ADR-A S1）。
    見つからなければ value=0。

    走査ポリシー:
    - 連続漢数字トークン ([一二三四五六七八九〇十百千万]+) は _kanji_amount で処理する
    - それ以外（算用数字+単位 / 小数 / K / カンマ区切り）は regex で処理する
    - 全パターンの候補を収集し、最左のものを採用する。同じ開始位置に複数マッチした
      場合は値が大きい方を優先する（"2千" では 2000 > 2 なので単位付き解釈が勝つ =
      従来「2千」を 2 と誤読して静かに clamp されていた経路の修正）
    - 「4万2」のような単位省略は 42000 と解釈しつつ ambiguous=True を立てる
      （呼び出し側が needs_review を付ける）
    """
    candidates: list[tuple[int, int, bool]] = []  # (start, value, ambiguous)

    for m in _KANJI_PATTERN.finditer(text):
        val, amb = _kanji_amount(m.group())
        if val > 0:
            candidates.append((m.start(), val, amb))

    for m in _MIXED_MAN_SEN.finditer(text):
        man = int(m.group(1).replace(",", ""))
        sen = int(m.group(2))
        candidates.append((m.start(), man * 10000 + sen * 1000, False))

    for m in _MAN_DECIMAL.finditer(text):
        man = int(m.group(1).replace(",", ""))
        frac = int(m.group(2)) / (10 ** len(m.group(2)))
        candidates.append((m.start(), int(round((man + frac) * 10000)), False))

    for m in _MAN_TRAILING.finditer(text):
        man = int(m.group(1).replace(",", ""))
        tail = int(m.group(2))
        if tail > 0:
            candidates.append((m.start(), man * 10000 + tail * 1000, True))

    for m in _MAN_ONLY.finditer(text):
        candidates.append((m.start(), int(m.group(1).replace(",", "")) * 10000, False))

    for m in _SEN_HYAKU.finditer(text):
        candidates.append(
            (m.start(), int(m.group(1)) * 1000 + int(m.group(2)) * 100, False)
        )

    for m in _SEN_ONLY.finditer(text):
        candidates.append((m.start(), int(m.group(1)) * 1000, False))

    for m in _HYAKU_ONLY.finditer(text):
        candidates.append((m.start(), int(m.group(1)) * 100, False))

    for m in _K_DECIMAL.finditer(text):
        base = int(m.group(1).replace(",", ""))
        frac = int(m.group(2)) / (10 ** len(m.group(2)))
        candidates.append((m.start(), int(round((base + frac) * 1000)), False))

    for m in _K_UNIT.finditer(text):
        candidates.append((m.start(), int(m.group(1).replace(",", "")) * 1000, False))

    for m in _DIGIT_ONLY.finditer(text):
        candidates.append((m.start(), int(m.group().replace(",", "")), False))

    if not candidates:
        return AmountParse(0)

    # 最左（開始位置が最小）→ 同位置なら値が大きい方（より具体的な単位付き解釈）を採用。
    candidates.sort(key=lambda x: (x[0], -x[1]))
    start, value, ambiguous = candidates[0]
    return AmountParse(value, ambiguous)


def parse_amount(text: str) -> int:
    """後方互換ラッパー: 金額のみを返す（曖昧フラグは parse_amount_ex を使う）。"""
    return parse_amount_ex(text).value


def parse_action(
    text: str,
    confidence: Optional[float] = None,
    utterance_start_ts: Optional[float] = None,
) -> Optional[AudioEvent]:
    """Whisper の認識テキストからアクション種別と金額を抽出して AudioEvent を返す。
    認識できない場合は None を返す。

    confidence: Whisper per-segment 信頼度 [0,1]（呼び出し側が ASR から渡す）。additive。
    utterance_start_ts: 発話キャプチャの開始時刻（recorder が渡す, ADR-B T1）。additive。

    キーワード選択ルール:
    1. テキスト内で最も左に現れたキーワードを優先する。
    2. 同じ開始位置に複数のキーワードがマッチした場合は、より長いキーワードを優先する。
       （例: "all in" と "all" が同位置にマッチ → "all in" を採用）
    3. 採用キーワードの span と重ならない位置に**別アクション**のキーワードがあれば
       parse_flags に "multi_action_keywords" を立てる（V1: 先頭のみ採用 + 要レビュー。
       "スリーベット" 内の "ベット" のような包含マッチは flag しない）。
    """
    # 全角数字・全角英字・半角カナ等を正規化してからパースする（raw_text は原文を保持）。
    norm = unicodedata.normalize("NFKC", text)
    lower = norm.lower()

    matches: list[tuple[int, int, str]] = []  # (pos, kw_len, action)
    for keyword, action in ACTION_KEYWORDS.items():
        kw = keyword.lower()
        start = 0
        while True:
            pos = lower.find(kw, start)
            if pos == -1:
                break
            matches.append((pos, len(kw), action))
            start = pos + 1

    if not matches:
        logger.debug("No action keyword found in: %r", text)
        return None

    # 最左優先。同位置なら長いキーワードを優先（より具体的な表現を採用するため）
    matches.sort(key=lambda t: (t[0], -t[1]))
    found_pos, found_len, found_action = matches[0]
    span_end = found_pos + found_len

    flags: list[str] = []
    for pos, kw_len, action in matches[1:]:
        if action == found_action:
            continue
        if pos < span_end and pos + kw_len > found_pos:
            continue  # 採用キーワードと重なる包含マッチ（例: スリーベット ⊃ ベット）
        flags.append("multi_action_keywords")
        break

    # 席番号表現（シート1 / seat 3 等）を除去してから金額を抽出する。
    # 除去しないと parse_amount() が席番号の数字を最初の金額候補として拾ってしまう。
    amount = parse_amount_ex(_strip_seat_references(norm))
    if amount.ambiguous:
        flags.append("ambiguous_amount")

    return AudioEvent(
        action=found_action,
        amount=amount.value,
        timestamp=time.time(),
        raw_text=text,
        seat=_extract_seat_no(norm),
        confidence=confidence,
        parse_flags=tuple(flags),
        utterance_start_ts=utterance_start_ts,
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
        confidence は各 segment の avg_logprob（対数確率）平均を exp で 0..1 に写像し、
        no_speech_prob が高い segment はその分減衰させたもの（ADR-C T4:
        プロンプト由来のオウム返しハルシネーションは無音区間で no_speech_prob が
        高く出るため、制御語ガードの入力として意味を持つ）。segment が無ければ None。
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
            no_speech: list[float] = []
            for seg in segments:
                texts.append(seg.text.strip())
                lp = getattr(seg, "avg_logprob", None)
                if lp is not None:
                    logprobs.append(lp)
                nsp = getattr(seg, "no_speech_prob", None)
                if nsp is not None:
                    no_speech.append(nsp)
            text = " ".join(texts)
            confidence: Optional[float] = None
            if logprobs:
                mean_lp = sum(logprobs) / len(logprobs)
                # avg_logprob は対数確率(≤0)。exp で 0..1 の信頼度へ写像。
                confidence = max(0.0, min(1.0, math.exp(mean_lp)))
                if no_speech:
                    mean_nsp = sum(no_speech) / len(no_speech)
                    confidence *= max(0.0, 1.0 - mean_nsp)
            return text, confidence
        except Exception:
            logger.exception("Whisper transcription failed")
            return "", None


# ――― R3: 合法手への射影（apply_corrections, ADR-0009 §5）―――

_BETTING_ACTIONS = frozenset({"fold", "check", "call", "bet", "raise", "allin"})

# ADR-0009 §6 条件② / ADR-C G4: この信頼度以上の ASR が状態と食い違って射影された場合、
# 状態側（ストリート遷移漏れ等）を疑って needs_review を付ける。
HIGH_CONF_ASR = 0.85


@dataclass
class Correction:
    """apply_corrections の結果（合法手へ射影済みのアクション）。"""

    action: str                          # 射影後アクション
    amount: int                          # bet/raise は "to" 総額、call は call 額、他 0
    needs_review: bool
    corrected_from: Optional[str] = None  # 修復前の生 ASR action（変化なしなら None）
    reason: str = ""                      # 監査用の短い理由（複数は "+" 区切り）
    asr_confidence: Optional[float] = None  # 入力 Whisper 信頼度を下流へ持ち越す


def _snap_to_legal(a: int, lo: int, hi: int) -> tuple[int, int]:
    """heard 額 a を合法レンジ [lo, hi] に丸め、(snapped, gap) を返す。gap は移動量(絶対値)。"""
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

    ADR-A/C での強化:
    - S4: 金額 snap の review 閾値を同次元比較（gap >= bb）に修正（従来の gap > min_raise_to は
      「2千→2 誤読 → min へ clamp」を無警告で通していた）。bb 不明（=0）は従来閾値に fallback。
    - S3: heard 額が to 解釈では非合法だが「追加額(by)解釈」なら合法という場合、
      by 読み上げの可能性を reason="raise_to_vs_by_ambiguous" で明示（採用は従来どおり to 解釈 + snap）。
      両解釈とも合法な通常レイズは慣例（to 読み上げ）を信頼し flag しない。
    - V4: heard 額が bb の倍数でない場合、bb 倍数への丸めが合法レンジ内なら丸める
      （reason="rounded_to_bb"。ASR の端数誤認識対策）。
    - G4: 高信頼 ASR（>= HIGH_CONF_ASR）なのに action が射影で変わった場合は review
      （状態側の疑い, ADR-0009 §6 条件②）。

    Args:
        action/amount: parse_action 由来の生 ASR。
        ctx: `legal_context()`（actor_seat / legal_actions / amount_to_call=c / min_raise=m(to) /
             max_raise=s(to) / bb / committed）。
        whisper_conf: ASR 信頼度。結果へ持ち越し、G4 の高信頼判定にも使う。
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
    bb = ctx.bb

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
    reasons: list[str] = []
    review = False

    if corrected_from:
        reasons.append(f"{a}_to_{target}")
        if whisper_conf is not None and whisper_conf >= HIGH_CONF_ASR:
            # G4: 高信頼 ASR が状態と矛盾 → ストリート遷移漏れ等、状態側の疑い。
            review = True
            reasons.append("high_conf_asr_projection")

    if amount <= 0:
        snapped, _ = _snap_to_legal(0, m, s)
        reasons.append("no_amount_heard")
        return mk(target, snapped, True, corrected_from, "+".join(reasons))

    val = amount

    # S3: raise の to/by 曖昧性。to 解釈が非合法（min 未満）だが「追加額」解釈
    # （現最高額 committed+c に heard を上乗せ）なら合法 → by 読み上げの可能性を flag。
    if target == "raise" and val < m:
        by_total = ctx.committed + c + val
        if m <= by_total <= s:
            review = True
            reasons.append("raise_to_vs_by_ambiguous")

    # V4: bb 倍数への round 寄せ（合法レンジ内に収まる場合のみ）。
    if bb > 0 and val % bb != 0:
        rounded = int(round(val / bb)) * bb
        if m <= rounded <= s:
            reasons.append("rounded_to_bb")
            val = rounded

    snapped, gap = _snap_to_legal(val, m, s)
    if gap > 0:
        reasons.append("amount_snapped")
        # S4: 同次元比較。snap 移動量が bb 以上なら「聞き取りが大きく外れた」として review。
        if (gap >= bb) if bb > 0 else (gap > m):
            review = True

    return mk(target, snapped, review, corrected_from, "+".join(reasons))
