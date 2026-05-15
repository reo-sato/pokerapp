"""audio/speech_normalizer.py

音声認識後処理の正規化モジュール。

生の ASR テキストを受け取り、action_aliases / seat_aliases / 数字正規化を用いて
正規化テキスト・アクション候補・席番号・金額を抽出する。

使い方:
    # アプリ起動時 (main.py など) に一度だけ初期化する
    from audio.speech_normalizer import init_normalizer
    init_normalizer("./speech_normalization.json")

    # 認識ループ内（parse_action 経由で自動適用される）
    from audio.speech_normalizer import normalize
    result = normalize(raw_text)

辞書ファイルの追記は再起動なしで反映されない（現仕様）。
将来的に hot-reload が必要になった場合は mtime 監視を追加すること。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from core.constants import KANJI_DIGIT, KANJI_UNIT

logger = logging.getLogger(__name__)


# ── ひらがな数詞トークンテーブル ─────────────────────────────────────────────────
# (パターン, タイプ, 値)  ← 長さ降順ソート済み（最長マッチ優先）
#   "digit": 0-9 の数値。後続の unit と掛け合わされる
#   "unit" : 10/100/1000/10000 の乗数。前の digit がない場合は 1 とみなす

_T_DIGIT = "digit"
_T_UNIT  = "unit"

_HIRA_TOKENS: list[tuple[str, str, int]] = sorted([
    # 縮約形（本来の形より先にマッチさせる必要がある）
    ("いっ",   _T_DIGIT, 1),    # いっせん / いっぴゃく の先頭
    ("ろっ",   _T_DIGIT, 6),    # ろっぴゃく
    ("はっ",   _T_DIGIT, 8),    # はっぴゃく
    # 通常 2 文字以上の digit
    ("いち",   _T_DIGIT, 1),
    ("きゅう", _T_DIGIT, 9),
    ("しち",   _T_DIGIT, 7),
    ("なな",   _T_DIGIT, 7),
    ("はち",   _T_DIGIT, 8),
    ("ろく",   _T_DIGIT, 6),
    ("さん",   _T_DIGIT, 3),
    ("よん",   _T_DIGIT, 4),
    # 単文字 digit（曖昧なので最後に試みる）
    ("に",     _T_DIGIT, 2),
    ("し",     _T_DIGIT, 4),
    ("ご",     _T_DIGIT, 5),
    ("く",     _T_DIGIT, 9),
    # unit（連濁・半濁音含む）
    ("ぴゃく", _T_UNIT, 100),   # ろっぴゃく / いっぴゃく
    ("びゃく", _T_UNIT, 100),   # さんびゃく
    ("ひゃく", _T_UNIT, 100),
    ("じゅう", _T_UNIT,  10),
    ("ぜん",   _T_UNIT, 1000),  # さんぜん（連濁）
    ("せん",   _T_UNIT, 1000),
    ("まん",   _T_UNIT, 10000),
], key=lambda t: -len(t[0]))

_HIRA_UNIT_SET: frozenset[str] = frozenset(p for p, t, _ in _HIRA_TOKENS if t == _T_UNIT)
_HIRA_ALL_RE = re.compile(r"[ぁ-ん]+")

# ── 算用数字・漢数字マッチ用正規表現 ──────────────────────────────────────────────
_KANJI_NUM_RE    = re.compile(r"[一二三四五六七八九〇十百千万]+")
_MIXED_MAN_SEN_RE = re.compile(r"(\d[\d,]*)万(\d+)千")
_MAN_ONLY_RE     = re.compile(r"(\d[\d,]*)万")
_K_UNIT_RE       = re.compile(r"(\d[\d,]*)[Kk]")
_DIGIT_ONLY_RE   = re.compile(r"\d[\d,]+|\d")        # カンマ区切り含む
_SPACED_DIGIT_RE = re.compile(r"(\d+)\s+(\d{2,4})\b")  # "1 200" → 1200

# 金額が意味を持つアクション
_AMOUNT_ACTIONS: frozenset[str] = frozenset({"BET", "RAISE"})


# ── 数字パース関数群 ──────────────────────────────────────────────────────────────

def _kanji_to_int_local(s: str) -> int:
    """漢数字文字列を整数に変換する（recognizer.py 内 _kanji_to_int の複製）。

    循環インポートを避けるため speech_normalizer 内に独立して保持する。
    """
    if not s:
        return 0
    if "万" in s:
        idx = s.index("万")
        left  = s[:idx]
        right = s[idx + 1:]
        left_val  = _kanji_to_int_local(left)  if left  else 1
        right_val = _kanji_to_int_local(right) if right else 0
        return left_val * 10000 + right_val
    result = 0
    cur = 0
    for ch in s:
        if ch in KANJI_DIGIT:
            cur = KANJI_DIGIT[ch]
        elif ch in KANJI_UNIT:
            unit = KANJI_UNIT[ch]
            result += (cur if cur else 1) * unit
            cur = 0
        else:
            return 0
    result += cur
    return result


def _hira_to_int(s: str) -> Optional[int]:
    """ひらがな数詞文字列を整数に変換する。

    単位語（ひゃく/せん/まん 等）を含まない場合は None を返す（誤認識防止）。
    変換できない文字が含まれる場合も None を返す。

    例:
        ろくひゃく → 600
        せんにひゃく → 1200
        にせん → 2000
        ごひゃく → 500
        ろっぴゃく → 600
        はっぴゃく → 800
    """
    if not s:
        return None
    # 単位語を含まない純粋な 1-2 文字 digit のみはスキップ（助詞等との誤認識防止）
    if not any(u in s for u in _HIRA_UNIT_SET):
        return None

    result = 0
    cur = 0   # 直前の digit（次の unit と掛け合わせる）
    pos = 0
    while pos < len(s):
        matched = False
        for pattern, typ, val in _HIRA_TOKENS:   # 長さ降順ソート済み
            if s.startswith(pattern, pos):
                if typ == _T_DIGIT:
                    cur = val
                else:  # unit
                    result += (cur or 1) * val
                    cur = 0
                pos += len(pattern)
                matched = True
                break
        if not matched:
            return None  # 未知の文字 → 数詞ではない

    result += cur   # 末尾に単位なし digit が残った場合（例: "にじゅうご" → 25 の "ご"）
    return result if result > 0 else None


def _find_number_candidates(
    text: str,
    number_aliases: Optional[dict[str, int]] = None,
) -> list[tuple[int, int, int, str]]:
    """text から数値候補を (start, end, value, note) リストで返す。

    複数パターンが重複した場合は非重複貪欲選択を行い、同じ開始位置では
    「長いマッチ優先・同長なら大きい値優先」とする。

    対応形式:
      - 漢数字 (六百, 千二百)
      - ひらがな数詞・単体スパン (ろくひゃく)
      - ひらがな数詞・隣接スパン連結 (ろく ひゃく → ろくひゃく)
      - number_aliases の直接一致 (辞書登録済み複合語)
      - 万千組み合わせ (1万2千)
      - 万のみ (3万)
      - K 単位 (5K)
      - 算用数字・カンマ区切り (1,200)
      - スペース区切り算用数字 (1 200 → 1200)
    """
    raw: list[tuple[int, int, int, str]] = []

    # ── 漢数字 ────────────────────────────────────────────────────────────────
    for m in _KANJI_NUM_RE.finditer(text):
        val = _kanji_to_int_local(m.group())
        if val > 0:
            raw.append((m.start(), m.end(), val, f"kanji:{m.group()}"))

    # ── ひらがな数詞 ──────────────────────────────────────────────────────────
    spans = list(_HIRA_ALL_RE.finditer(text))

    # number_aliases の直接一致（複合語を辞書で登録している場合の優先使用）
    if number_aliases:
        for m in spans:
            s = m.group()
            if s in number_aliases and number_aliases[s] >= 10:
                raw.append((m.start(), m.end(), number_aliases[s], f"alias:{s}"))

    # 単体スパン → 複合数詞パーサ
    for m in spans:
        val = _hira_to_int(m.group())
        if val is not None:
            raw.append((m.start(), m.end(), val, f"hira:{m.group()}"))

    # 隣接スパン連結（"ろく ひゃく" → "ろくひゃく" → 600）
    for i in range(len(spans) - 1):
        m1, m2 = spans[i], spans[i + 1]
        if text[m1.end():m2.start()] == " ":
            combined = m1.group() + m2.group()
            val = _hira_to_int(combined)
            if val is not None:
                raw.append((m1.start(), m2.end(), val, f"hira_pair:{m1.group()}+{m2.group()}"))

    # ── 算用数字・単位付き ────────────────────────────────────────────────────
    for m in _MIXED_MAN_SEN_RE.finditer(text):
        man = int(m.group(1).replace(",", ""))
        sen = int(m.group(2))
        raw.append((m.start(), m.end(), man * 10000 + sen * 1000, f"man_sen:{m.group()}"))

    for m in _MAN_ONLY_RE.finditer(text):
        val = int(m.group(1).replace(",", "")) * 10000
        raw.append((m.start(), m.end(), val, f"man:{m.group()}"))

    for m in _K_UNIT_RE.finditer(text):
        val = int(m.group(1).replace(",", "")) * 1000
        raw.append((m.start(), m.end(), val, f"K:{m.group()}"))

    # ── 算用数字（カンマ区切り含む）────────────────────────────────────────────
    for m in _DIGIT_ONLY_RE.finditer(text):
        val = int(m.group().replace(",", ""))
        if val > 0:
            raw.append((m.start(), m.end(), val, f"digit:{m.group()}"))

    # ── スペース区切り数字 "1 200" → 1200 ─────────────────────────────────────
    for m in _SPACED_DIGIT_RE.finditer(text):
        try:
            val = int(m.group(1) + m.group(2))
            raw.append((m.start(), m.end(), val, f"spaced:{m.group()}"))
        except ValueError:
            pass

    # ── 非重複貪欲選択 ────────────────────────────────────────────────────────
    # ソート: 開始位置昇順 → 長さ降順 → 値降順（同位置なら長くて大きい候補を優先）
    raw.sort(key=lambda x: (x[0], -(x[1] - x[0]), -x[2]))
    selected: list[tuple[int, int, int, str]] = []
    covered_end = -1
    for start, end, val, note in raw:
        if start >= covered_end:
            selected.append((start, end, val, note))
            covered_end = end

    return selected


def _pick_amount(
    candidates: list[tuple[int, int, int, str]],
    action: Optional[str],
    action_end_pos: int = -1,
) -> Optional[int]:
    """action の種別と位置に基づいて最適な金額候補を選ぶ。

    BET / RAISE の場合のみ金額を返す。action_end_pos より後ろに現れる
    最初の候補を優先する（候補位置・アクション位置ともに置換前テキスト基準）。
    """
    if not candidates or action not in _AMOUNT_ACTIONS:
        return None

    if action_end_pos >= 0:
        after = [c for c in candidates if c[0] >= action_end_pos]
        if after:
            return after[0][2]

    # アクション後に見つからない場合は全候補の最初（席番号等のリスクがあるが最善努力）
    return candidates[0][2]


# ── NormalizedResult ─────────────────────────────────────────────────────────

@dataclass
class NormalizedResult:
    """speech_normalizer.normalize() の返り値。"""

    raw_text: str
    normalized_text: str
    action: Optional[str]               # 大文字正規形: "BET"/"RAISE"/"CALL"/... / None
    seat: Optional[int]                 # 正規化で抽出できた席番号、または None
    tokens: list[str]         = field(default_factory=list)
    matched_rules: list[str]  = field(default_factory=list)   # e.g. ["ベッド->BET"]
    amount: Optional[int]     = None                          # BET/RAISE 時の金額候補
    amount_candidates: list[int]  = field(default_factory=list)  # 全数値候補
    number_tokens: list[str]      = field(default_factory=list)  # テキストで見つかった数字表現
    parse_notes: list[str]        = field(default_factory=list)  # 数値化の根拠ルール


# ── SpeechNormalizer ─────────────────────────────────────────────────────────

class SpeechNormalizer:
    """speech_normalization.json を読み込んで ASR テキストを正規化するクラス。

    正規化の順序:
      1. seat_aliases でシート番号表現を統一 ("二番" → "シート2")
      2. action_aliases でアクション表現を統一 ("ベッド" → "BET")
      3. 数字抽出・正規化 (ひらがな/漢字/算用 → アラビア数字・金額候補)
      4. 将来: phrase_patterns / context validation を追加予定

    マッチングはすべてサブストリング・最長一致（左優先）で行う。
    辞書にない語はそのまま残す。
    """

    def __init__(self, normalization_path: str) -> None:
        self._path = normalization_path
        self._action_aliases: dict[str, str] = {}
        self._seat_aliases: dict[str, int] = {}
        self._number_aliases: dict[str, int] = {}   # 複合語の直接 value 登録
        self._action_keys: list[str] = []
        self._seat_keys: list[str] = []
        self._load()

    def _load(self) -> None:
        """JSON ファイルを読み込む。失敗した場合は空辞書で継続する。"""
        try:
            with open(self._path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            logger.warning(
                "Speech normalization file not found: %r (continuing with empty table)",
                self._path,
            )
            return
        except Exception:
            logger.exception(
                "Failed to load speech normalization file: %r (continuing with empty table)",
                self._path,
            )
            return

        raw_action: dict  = data.get("action_aliases", {})
        raw_seat: dict    = data.get("seat_aliases", {})
        raw_number: dict  = data.get("number_aliases", {})

        self._action_aliases = {
            k: v for k, v in raw_action.items()
            if isinstance(k, str) and isinstance(v, str)
        }
        self._seat_aliases = {
            k: int(v) for k, v in raw_seat.items()
            if isinstance(k, str) and str(v).lstrip("-").isdigit()
        }
        self._number_aliases = {
            k: int(v) for k, v in raw_number.items()
            if isinstance(k, str) and str(v).lstrip("-").isdigit()
        }

        self._action_keys = sorted(self._action_aliases, key=len, reverse=True)
        self._seat_keys   = sorted(self._seat_aliases,   key=len, reverse=True)

        logger.info(
            "SpeechNormalizer: loaded %d action_aliases, %d seat_aliases, "
            "%d number_aliases from %r",
            len(self._action_aliases), len(self._seat_aliases),
            len(self._number_aliases), self._path,
        )

    def normalize(self, raw_text: str) -> NormalizedResult:
        """raw_text を正規化して NormalizedResult を返す。

        処理順:
          1. seat_aliases: 席番号表現を統一
          2. action_aliases: アクション表現を統一
          3. 数字候補を抽出し、テキスト内の数字表現をアラビア数字に置換
          4. 金額（amount）を action に応じて決定
        """
        text = raw_text
        matched_rules: list[str] = []
        seat: Optional[int] = None
        action_candidates: list[tuple[int, str]] = []

        # ── 1. seat_aliases ──────────────────────────────────────────────────
        for key in self._seat_keys:
            pos = text.find(key)
            if pos != -1:
                seat_val = self._seat_aliases[key]
                seat = seat_val
                replacement = f"シート{seat_val}"
                text = text[:pos] + replacement + text[pos + len(key):]
                matched_rules.append(f"{key}->seat:{seat_val}")
                break

        # ── 2. action_aliases ────────────────────────────────────────────────
        pending: list[tuple[int, int, str, str]] = []
        for key in self._action_keys:
            pos = text.find(key)
            if pos != -1:
                alias = self._action_aliases[key]
                action_candidates.append((pos, alias))
                pending.append((pos, pos + len(key), key, alias))

        pending.sort(key=lambda x: x[0])
        parts: list[str] = []
        cursor = 0
        used: set[int] = set()
        for start, end, key, alias in pending:
            if start in used or any(start <= p < end for p in used):
                continue
            parts.append(text[cursor:start])
            parts.append(alias)
            cursor = end
            matched_rules.append(f"{key}->{alias}")
            used.update(range(start, end))
        parts.append(text[cursor:])
        text = "".join(parts)

        action: Optional[str] = None
        if action_candidates:
            action_candidates.sort(key=lambda x: x[0])
            unique_actions = list(dict.fromkeys(a for _, a in action_candidates))
            if len(unique_actions) > 1:
                logger.warning(
                    "SpeechNormalizer: multiple action candidates in %r: %s (using leftmost: %s)",
                    raw_text, unique_actions, unique_actions[0],
                )
            action = unique_actions[0]

        # ── 3. 数字抽出・金額決定・テキスト内数字表現をアラビア数字に置換 ──────────
        # 数字抽出は action/seat 置換後・数字置換前のテキストで行う（位置を確定させるため）
        num_candidates = _find_number_candidates(text, self._number_aliases)

        amount_candidates = [val for _, _, val, _ in num_candidates]
        number_tokens     = [note for _, _, _, note in num_candidates]
        parse_notes       = [note for _, _, _, note in num_candidates]

        # アクションキーワードの終端位置を置換前テキストで記録（_pick_amount で使用）
        action_end_pos = -1
        if action:
            ap = text.find(action)
            if ap >= 0:
                action_end_pos = ap + len(action)

        # ── 4. 金額決定（置換前テキスト基準の位置を使う）─────────────────────────
        amount = _pick_amount(num_candidates, action, action_end_pos)

        # テキスト内の数字表現をアラビア数字に置換（右から左で位置ずれ防止）
        for start, end, val, _ in sorted(num_candidates, key=lambda x: -x[0]):
            text = text[:start] + str(val) + text[end:]

        # matched_rules に数字変換ルールを追記
        num_rules = [
            f"{note.split(':', 1)[1] if ':' in note else note}->{val}"
            for _, _, val, note in num_candidates
        ]
        matched_rules.extend(num_rules)

        result = NormalizedResult(
            raw_text=raw_text,
            normalized_text=text,
            action=action,
            seat=seat,
            tokens=text.split(),
            matched_rules=matched_rules,
            amount=amount,
            amount_candidates=amount_candidates,
            number_tokens=number_tokens,
            parse_notes=parse_notes,
        )

        if matched_rules:
            logger.info(
                "Audio normalize: raw=%r normalized=%r action=%s seat=%s amount=%s rules=%s",
                raw_text, text, action, seat, amount,
                [r for r in matched_rules if r],  # noqa: C416
            )
        else:
            logger.debug("Audio normalize: raw=%r (no rules matched)", raw_text)

        return result

    def normalize_with_context(self, raw_text: str, game_state=None) -> NormalizedResult:
        """コンテキスト付き正規化（将来: ゲームステートと組み合わせる）。

        現時点では normalize() へのパススルー。
        game_state が渡された場合、現在有効なアクションのみを候補として絞り込む拡張を
        ここに追加することを想定している。
        """
        return self.normalize(raw_text)


# ── モジュールレベルシングルトン ────────────────────────────────────────────────

_normalizer: Optional[SpeechNormalizer] = None


def init_normalizer(path: str) -> None:
    """アプリ起動時に一度だけ呼び出してモジュールレベルノーマライザを初期化する。"""
    global _normalizer
    _normalizer = SpeechNormalizer(path)


def normalize(text: str) -> NormalizedResult:
    """モジュールレベルのノーマライザで text を正規化する。

    init_normalizer() が呼ばれていない場合は no-op の結果を返す。
    """
    if _normalizer is not None:
        return _normalizer.normalize(text)
    return NormalizedResult(
        raw_text=text,
        normalized_text=text,
        action=None,
        seat=None,
        tokens=text.split(),
        matched_rules=[],
    )


# ── スタンドアロン動作確認 ────────────────────────────────────────────────────

if __name__ == "__main__":
    import os
    import sys

    logging.basicConfig(level=logging.DEBUG,
                        format="%(levelname)s %(name)s: %(message)s")

    norm_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "speech_normalization.json",
    )
    if not os.path.exists(norm_path):
        print(f"[ERROR] {norm_path} が見つかりません", file=sys.stderr)
        sys.exit(1)

    n = SpeechNormalizer(norm_path)

    samples = [
        "ベッド 600",
        "別途 六百",
        "例 千二百",
        "シート2 レイズ 2000",
        "シート3 コール",
        "ベッド ろくひゃく",
        "レイズ せんにひゃく",
        "ベッド 1 200",
        "えっと ごひゃく",
        "椅子 にせんごひゃく",
        "ベット ろく ひゃく",
        "ゴール",
        "チェック",
        "オールイン",
        "二番 ベッド さんびゃく",
        "ろっぴゃく ベット",
        "レイズ 1,500",
        "ベット 5K",
    ]

    print("\n" + "=" * 72)
    print(f"{'raw':<30} {'action':<8} {'seat':<5} {'amount':<8} normalized")
    print("=" * 72)
    for s in samples:
        r = n.normalize(s)
        print(
            f"{s!r:<30} {str(r.action):<8} {str(r.seat):<5} "
            f"{str(r.amount):<8} {r.normalized_text!r}"
        )
        if r.parse_notes:
            print(f"  {'':30} rules: {r.matched_rules}")
    print("=" * 72)
