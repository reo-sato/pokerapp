from __future__ import annotations

import logging
import re
import time
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from core.constants import (
    ACTION_KEYWORDS,
    HAND_NAME_KEYWORDS,
    KANJI_DIGIT,
    KANJI_UNIT,
    WHISPER_PROMPT_JA,
)
from core.events import AudioEvent
from core.positions import parse_position

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


# ひらがな → カタカナ（`ACTION_KEYWORDS` / 席表現はカタカナ + 英字で書かれているため, ISSUE-0027）。
# ASR が「ちぇっく」と書き起こす場合と、CLI で IME 変換せずに「ちぇっく」と打った場合の両方を拾う。
# U+3041..U+3096（ぁ..ゖ）を +0x60 してカタカナ帯に移す 1:1 写像なので **文字位置が保たれる**
# （NFKC はひらがなを変えないので、NFKC の後に重ねて使う）。
_HIRAGANA_TO_KATAKANA = str.maketrans(
    {chr(c): chr(c + 0x60) for c in range(0x3041, 0x3097)}
)


def _to_katakana(text: str) -> str:
    """ひらがなをカタカナに正規化する（長さ・文字位置は不変）。"""
    return text.translate(_HIRAGANA_TO_KATAKANA)


# 席番号表現。strip（金額パース前の除去）と抽出の両方が同一パターンを共有する
# （ADR-0047 S2: 従来は strip 側が「シート 3」(空白) / 漢数字席を取りこぼし、席番号が金額に流入した）。
# 呼び出し側が生テキストを渡すこともある（engine の rebuy / winner）ので、照合前にひらがなを寄せる。
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
    return _SEAT_PATTERN.sub("", _to_katakana(text))


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
    m = _SEAT_PATTERN.search(_to_katakana(text))
    if not m:
        return None
    n = _seat_token_to_int(m.group(1))
    return n if n is not None and 1 <= n <= 9 else None


def _extract_all_seat_nos(text: str) -> list[int]:
    """発話テキストから明示的な席番号 (1..9) を出現順に全部返す（重複除去, ADR-D S7）。

    "シート3 シート5 チョップ" のような split-pot 読み上げで複数勝者席を拾う。
    """
    seats: list[int] = []
    for m in _SEAT_PATTERN.finditer(_to_katakana(text)):
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


# 仮名で書き起こされた数の読み → 漢数字（ADR-0062）。Whisper は「ベット なな」を「ベトナナ」のように
# 仮名で書くことがある。照合前にひらがなはカタカナへ寄せてあるので、カタカナだけを持つ。
# 同じ位置では長い読みを先に試す（「キュウ」「ナナ」を「キュ」「ナ」…と切らない）。
_KANA_NUMBER_WORDS: tuple[tuple[str, str], ...] = tuple(sorted((
    ("イチ", "一"), ("イッ", "一"), ("ニ", "二"), ("サン", "三"), ("ヨン", "四"),
    ("ゴ", "五"), ("ロク", "六"), ("ロッ", "六"), ("ナナ", "七"), ("シチ", "七"),
    ("ハチ", "八"), ("ハッ", "八"), ("キュウ", "九"), ("キュー", "九"),
    ("ジュウ", "十"), ("ジュー", "十"),
    ("ヒャク", "百"), ("ビャク", "百"), ("ピャク", "百"),
    ("セン", "千"), ("ゼン", "千"), ("マン", "万"),
), key=lambda w: -len(w[0])))
# 促音の形（イッ / ロッ / ハッ）は単位（セン / ピャク）の前にしか来ない
_KANA_NUMBER_CLIPPED = frozenset({"イッ", "ロッ", "ハッ"})
# 数の読みのあとに続いてよいカタカナ（ひらがなの「です」「で」「まい」もカタカナに寄っている）
_KANA_NUMBER_SUFFIXES = ("デス", "デ", "ダ", "マイ", "ポイント", "エン", "テン")
_KANJI_DIGITS_ONLY = frozenset("一二三四五六七八九")


def _is_katakana(ch: str) -> bool:
    return "ァ" <= ch <= "ヺ"


def _kana_number_at(text: str, pos: int) -> Optional[tuple[int, int, str]]:
    """`pos` から始まる仮名の数の読みを漢数字にする。(開始, 終了, 漢数字) か、数でなければ None。

    「ニシマス」（〜にします）の「ニ」や「ゴール」の「ゴ」のように、続きがほかの言葉なら数と
    みなさない（読みの直後が文の終わり・区切り・カタカナ以外・「です」等のときだけ数）。
    """
    i, words = pos, []
    while i < len(text):
        for kana, kanji in _KANA_NUMBER_WORDS:
            if text.startswith(kana, i):
                words.append((kana, kanji))
                i += len(kana)
                while i < len(text) and text[i] == "ー":   # 「ゴー」「ニー」のように伸ばした読み
                    i += 1
                break
        else:
            break
    if not words:
        return None
    rest = text[i:]
    if rest and _is_katakana(rest[0]) and not rest.startswith(_KANA_NUMBER_SUFFIXES):
        return None
    kanji = "".join(k for _, k in words)
    for (kana, _), nxt in zip(words, [*kanji[1:], ""]):
        if kana in _KANA_NUMBER_CLIPPED and nxt not in ("千", "百"):
            return None
    if any(a in _KANJI_DIGITS_ONLY and b in _KANJI_DIGITS_ONLY for a, b in zip(kanji, kanji[1:])):
        return None                                          # 「ニサン」のように数字が 2 つ並ぶ
    return pos, i, kanji


def _kana_amount_to_kanji(norm: str, keyword_end: int) -> str:
    """アクションの語の直後にある仮名の数の読みを漢数字に置き換えた文字列を返す（ADR-0062）。

    「ベトナナ」→「ベト七」、「ベット にじゅうさん」→「ベット 二十三」。語の直後だけを見るのは、
    ほかの言葉の中の「ゴ」「ニ」を金額と取り違えないため。
    """
    i = keyword_end
    while i < len(norm) and norm[i] in _SPLIT_DELIMITERS:
        i += 1
    found = _kana_number_at(norm, i)
    if found is None:
        return norm
    start, end, kanji = found
    return norm[:start] + kanji + norm[end:]


# 書き起こしゆれとして足した短い語は、ほかの言葉の一部として現れやすい（「なべとなって」の「ベト」、
# 「ゴールド」の「ゴール」）。前後が区切り・数・別のアクションの語のときだけアクションとみなす（ADR-0063）。
_BOUNDARY_KEYWORDS = frozenset({"ベト", "ゴール", "ホールド", "ベッド"})


def _keyword_matches(norm: str) -> list[tuple[int, int, str]]:
    """正規化済みテキスト中のアクションキーワードの出現 (位置, 長さ, action) を返す。

    最左優先・同位置なら長いキーワードを先（より具体的な表現を採用するため）に並べる。
    """
    lower = norm.lower()
    matches: list[tuple[int, int, str, str]] = []
    for keyword, action in ACTION_KEYWORDS.items():
        # キーワード側にも同じ正規化を掛ける（「降ります」のようにひらがなを含む語彙が、
        # カタカナ化した入力と食い違わないように）。
        kw = _to_katakana(unicodedata.normalize("NFKC", keyword)).lower()
        start = 0
        while True:
            pos = lower.find(kw, start)
            if pos == -1:
                break
            matches.append((pos, len(kw), action, kw))
            start = pos + 1
    starts = {m[0] for m in matches}
    ends = {m[0] + m[1] for m in matches}

    def isolated(pos: int, end: int) -> bool:
        before_ok = pos == 0 or not _is_katakana(lower[pos - 1]) or pos in ends
        after_ok = (
            end == len(lower) or not _is_katakana(lower[end]) or end in starts
            or _kana_number_at(norm, end) is not None
        )
        return before_ok and after_ok

    kept = [
        (pos, length, action) for pos, length, action, kw in matches
        if kw not in _BOUNDARY_KEYWORDS or isolated(pos, pos + length)
    ]
    kept.sort(key=lambda t: (t[0], -t[1]))
    return kept


# Whisper は雑音や聞き取れない音に対して initial_prompt（WHISPER_PROMPT_JA）をそのまま書き起こすことがある
# （店舗の実測: 「シート3 レイズ 2千、コール、チョップ などの言葉が含まれます」が信頼度 0.1〜0.2 で出て、
# レイズ・コール・ウィナーとして記録された, ADR-0063）。ディーラーが言うはずのない部分で見分ける。
_PROMPT_ECHO_MARKERS = ("言葉が含まれ", "読み上げています", "ウィナー、ハンド開始", "ハンド開始、チョップ")
# プロンプトの語をその順に 4 つ以上並べたもの（「コール、チェック、フォールド、オールイン、…」を繰り返す幻聴。
# 店舗の 3 回目の通しテストで「シート4 レイズ 2千」として記録された）。ディーラーはこの順には言わない。
_PROMPT_WORD_ORDER = ("コール", "チェック", "フォールド", "オールイン", "ショーダウン", "ウィナー", "ハンド")
_PROMPT_WORD_RUNS = tuple(
    "、".join(_PROMPT_WORD_ORDER[i:i + 4]) for i in range(len(_PROMPT_WORD_ORDER) - 3)
)
# 無音・雑音に対する Whisper の定型の幻聴（動画の字幕に多い締めの言葉）。
_STOCK_HALLUCINATIONS = ("ご視聴", "ご覧いただ", "ご覧頂", "チャンネル登録", "お楽しみに")


def is_prompt_echo(text: str) -> bool:
    """書き起こしが雑音への幻聴（initial_prompt の繰り返し・字幕の定型句）か。"""
    norm = unicodedata.normalize("NFKC", text).replace(" ", "").replace("　", "").replace(",", "、")
    return any(marker in norm for marker in (
        *_PROMPT_ECHO_MARKERS, *_PROMPT_WORD_RUNS, *_STOCK_HALLUCINATIONS,
    ))


# 発話の長さで言える文字数の上限（空白を除く）。人は 1 秒に 10 文字も言わない。幻聴のループ（「…ウィナー、
# ハンド、チェック、フォールド、…」を繰り返す）は 1 秒の音から数十文字が出る（店舗の 5 回目の通しテスト:
# 3.6 秒の音から 250 文字）。書き起こしを長さの上限で打ち切ったものもここで捨てる（`WhisperTranscriber`）。
_TEXT_CHARS_BASE = 12
_TEXT_CHARS_PER_SEC = 12


def is_implausibly_long(text: str, audio_sec: float) -> bool:
    """書き起こしが発話の長さでは言えない量か（雑音への幻聴のループ）。

    切り出した音は語頭の前 0.3 秒と語尾の無音を含むので 1 秒より短くはならない（1 秒として数える）。
    """
    chars = len(re.sub(r"\s", "", text))
    return chars > _TEXT_CHARS_BASE + _TEXT_CHARS_PER_SEC * max(1.0, audio_sec)


# 数字だけの発話（「600点」「2千点です」）はベットかレイズ（店のディーラーは語を省いて額だけ言う。店舗の
# 3 回目の通しテストで「600点」「2千点」がベットだった）。どちらかは engine が状態から決め、額がいまのベット
# 以下・最小ベット未満なら使わない。額の前後に付いてよいのは下の語だけ — 「ポット 2千点」「残り 1500」
# 「7ヒット」のような発話や、違う数が並ぶ発話（「5 6 7」）はアクションにしない。
_AMOUNT_TOKEN = re.compile(r"(?:\d[\d,]*(?:\.\d+)?[万千百Kk]?)+|[一二三四五六七八九〇十百千万]+")
_AMOUNT_ONLY_REST = re.compile(
    r"(?:[\s、。・!?,.ー〜~]|点|テン|ポイント|デス|デース|ニナリマス|ハイ|エー|エット|エート|エ|アー|ア"
    r"|ジャア|ジャ|デハ|アクション|ネ|ヨ)*"
)
# 数字だけの部分を区切る文字（空白では区切らない: 「シート3 600点」「5 6 7」を 1 まとまりに見る）
_AMOUNT_CHUNK = re.compile(r"[^、。・!?]+")
# 前後に区切って言った額を、別の人のベット・レイズとして分けるアクション（「2千点、コール」= 2000 のベット
# のあとにコール）。ベット・レイズ・オールインの前後の額はそのアクションの額なので分けない。
_AMOUNT_SPLIT_ACTIONS = frozenset({"call", "check", "fold"})
# 「チェックアラウンド」= まだ動いていない全員がチェックした（オーナーの説明, 2026-09-25）。
# 「チェック、アランド」のように区切って書き起こされることもある（店舗の実測）。
_CHECK_AROUND = re.compile(
    r"(?:チェック|check)[\s、。,.・]*(?:ア(?:ラウ|ラ)ン(?:ド|ト)?|around)", re.IGNORECASE,
)


def parse_amount_only(
    text: str,
    confidence: Optional[float] = None,
    utterance_start_ts: Optional[float] = None,
) -> Optional[AudioEvent]:
    """額だけを言った発話を、ベットかレイズの候補（action="bet" + flag "amount_only"）にする。

    席番号・ポジション名は付いていてもよい。額でなければ None。
    """
    from core.positions import _ALIAS_PATTERN

    norm = _to_katakana(unicodedata.normalize("NFKC", text))
    body = _ALIAS_PATTERN.sub(" ", _SEAT_PATTERN.sub(" ", norm))
    tokens = [m.group() for m in _AMOUNT_TOKEN.finditer(body)]
    if not tokens:
        # 仮名で書き起こされた額（「ロッピャク」「センゴヒャクテン」）。発話が額だけのときに限る
        for start in range(len(body) + 1):
            if not _AMOUNT_ONLY_REST.fullmatch(body[:start]):
                continue
            found = _kana_number_at(body, start)
            if found is not None and _AMOUNT_ONLY_REST.fullmatch(body[found[1]:]):
                tokens = [found[2]]
                body = body[:start] + found[2] + body[found[1]:]
                break
        if not tokens:
            return None
    amounts = [parse_amount_ex(token) for token in tokens]
    if len({a.value for a in amounts}) != 1 or amounts[0].value <= 0:
        return None                         # 違う数が並ぶ（「5 6 7」）
    if not _AMOUNT_ONLY_REST.fullmatch(_AMOUNT_TOKEN.sub(" ", body)):
        return None                         # 額のほかに言葉がある（「ポット 2千点」）
    flags = ["amount_only"]
    if amounts[0].ambiguous:
        flags.append("ambiguous_amount")
    return AudioEvent(
        action="bet",
        amount=amounts[0].value,
        timestamp=time.time(),
        raw_text=text,
        seat=_extract_seat_no(norm),
        confidence=confidence,
        position=parse_position(norm),
        parse_flags=tuple(flags),
        utterance_start_ts=utterance_start_ts,
    )


def _split_off_amounts(
    part: str, event: AudioEvent,
    confidence: Optional[float], utterance_start_ts: Optional[float],
) -> list[AudioEvent]:
    """コール・チェック・フォールドの前後に区切って言った額を、別のベット・レイズとして分ける。

    「2千点、コール」→ 2000 / コール。区切らずに言った額（「600点コールです」）はコールの額のまま。
    同じ額の言い直しは engine が「いまのベット以下」として捨てる。
    """
    if event.action not in _AMOUNT_SPLIT_ACTIONS:
        return [event]
    nfkc = unicodedata.normalize("NFKC", part)
    norm = _to_katakana(nfkc)
    source = part if len(nfkc) == len(part) else nfkc
    matches = _keyword_matches(norm)
    if not matches:
        return [event]
    keyword_at = matches[0][0]
    before: list[AudioEvent] = []
    after: list[AudioEvent] = []
    kept: list[str] = []
    for m in _AMOUNT_CHUNK.finditer(norm):
        chunk = source[m.start():m.end()]
        if m.start() <= keyword_at < m.end():
            kept.append(chunk)
            continue
        amount = parse_amount_only(chunk, confidence, utterance_start_ts)
        if amount is None:
            kept.append(chunk)
        elif m.end() <= keyword_at:
            before.append(amount)
        else:
            after.append(amount)
    if not before and not after:
        return [event]
    main = parse_action("、".join(kept), confidence=confidence, utterance_start_ts=utterance_start_ts)
    return [*before, main or event, *after]


# 1 回の発話から分けるアクションの上限。9 人卓の 1 ラウンドは最大 8 アクションで足り、
# これを超えるのは Whisper の繰り返し（同じ語が何十回も並ぶ幻聴）とみなして分けない。
_MAX_ACTIONS_PER_UTTERANCE = 8
# アクションの区切りとみなす文字（Whisper は間の短い発話を「、」や空白でつなぐ）。
_SPLIT_DELIMITERS = frozenset("、。，,.・ 　")


def _distinct_keywords(matches: list[tuple[int, int, str]]) -> list[tuple[int, int, str]]:
    """重なり（「スリーベット」⊃「ベット」等の包含）を除いた、左から順のキーワード出現。"""
    kept: list[tuple[int, int, str]] = []
    for m in matches:
        if kept and m[0] < kept[-1][0] + kept[-1][1]:
            continue
        kept.append(m)
    return kept


def _split_points(norm: str, keywords: list[tuple[int, int, str]]) -> list[int]:
    """隣り合うキーワードの間で、発話をどこで切るか（次のアクションの始まり）を返す。

    1. 間に席番号・ポジション名（次のアクションの主語）があれば、その直前で切る。
    2. 無ければ最後の区切り文字（「、」・空白など）の直後で切る（金額は前のアクションに残る:
       「レイズ 600、コール」→「レイズ 600、」「コール」）。
    3. 区切りも無ければ次のキーワードの直前で切る（「フォールドフォールド」）。
    """
    from core.positions import _ALIAS_PATTERN

    cuts: list[int] = []
    for prev, nxt in zip(keywords, keywords[1:]):
        lo, hi = prev[0] + prev[1], nxt[0]
        between = norm[lo:hi]
        subjects = [m.start() for m in _SEAT_PATTERN.finditer(between)]
        subjects += [m.start() for m in _ALIAS_PATTERN.finditer(between)]
        if subjects:
            cuts.append(lo + min(subjects))
            continue
        delims = [i for i, ch in enumerate(between) if ch in _SPLIT_DELIMITERS]
        cuts.append(lo + delims[-1] + 1 if delims else hi)
    return cuts


def parse_actions(
    text: str,
    confidence: Optional[float] = None,
    utterance_start_ts: Optional[float] = None,
) -> list[AudioEvent]:
    """1 回の発話からアクションを**言った順にすべて**返す（ADR-0061）。

    ディーラーが間を空けずに続けて言うと（「フォールド、フォールド、コール」）、1 つの発話として
    書き起こされる。席番号を言わない運用では手番の順でアクターを決めるので、1 つでも落とすと
    以降のアクターがすべてずれる。キーワードが 2 つ以上あれば `_split_points` で切り分け、
    それぞれを `parse_action` で読む（切り分けた各アクションには複数アクションの flag は付かない）。
    キーワードが 1 つ、または多すぎる（繰り返しの幻聴）ときは従来どおり `parse_action` 1 件。
    """
    nfkc = unicodedata.normalize("NFKC", text)
    norm = _to_katakana(nfkc)
    keywords = _distinct_keywords(_keyword_matches(norm))
    if not keywords:
        # アクションの語が無くても、額だけを言っていればベットかレイズ（「600点」）
        event = parse_amount_only(text, confidence=confidence, utterance_start_ts=utterance_start_ts)
        return [event] if event is not None else []
    if len(keywords) == 1 or len(keywords) > _MAX_ACTIONS_PER_UTTERANCE:
        event = parse_action(text, confidence=confidence, utterance_start_ts=utterance_start_ts)
        if event is None:
            return []
        if len(keywords) > _MAX_ACTIONS_PER_UTTERANCE:
            # 繰り返しの聞き違いの疑い。分けずに 1 件にして要レビューにする。
            event.parse_flags = (*event.parse_flags, "too_many_actions")
            return [event]
        return _split_off_amounts(text, event, confidence, utterance_start_ts)
    cuts = _split_points(norm, keywords)
    # 区間を原文から切り出す（NFKC で長さが変わった入力だけは正規化後の文字列から切る）。
    source = text if len(nfkc) == len(text) else nfkc
    bounds = [0, *cuts, len(norm)]
    events: list[AudioEvent] = []
    for start, end in zip(bounds, bounds[1:]):
        part = source[start:end].strip("".join(_SPLIT_DELIMITERS))
        event = parse_action(part, confidence=confidence, utterance_start_ts=utterance_start_ts)
        if event is not None:
            events.extend(_split_off_amounts(part, event, confidence, utterance_start_ts))
    return events


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
    4. **NFKC + ひらがな→カタカナ**に正規化してから照合する（`ACTION_KEYWORDS` はカタカナ + 英字）。
       ASR が「ちぇっく」と書き起こす場合と、CLI で IME 変換せずに打った場合の両方を拾う
       （ISSUE-0027）。`raw_text` は元のテキストをそのまま残す。
    5. 席番号（"シート3"）に加え **ポジション名**（"BTN、コール"）も拾う（仕様 §7 / FR-26,
       ISSUE-0032）。どちらも「誰が行動したか」の明示証拠だが、席への解決はボタンを知っている
       engine 側の責務なので、ここでは正準名を持ち回るだけにする。
    """
    # 全角数字・全角英字・半角カナ等を正規化し、ひらがなをカタカナに寄せてからパースする
    # （raw_text は原文を保持）。どちらも文字位置を保つ 1:1 の写像。
    norm = _to_katakana(unicodedata.normalize("NFKC", text))
    matches = _keyword_matches(norm)

    if not matches:
        logger.debug("No action keyword found in: %r", text)
        return None

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
    if found_action == "check" and _CHECK_AROUND.match(norm, found_pos):
        flags.append("check_around")
    # ショーダウンで言った勝った役名（「ツーペア」= ハンドの終わり + 判定との突き合わせ, 2026-09-26）
    hand_name = _hand_name_for(norm[found_pos:span_end]) if found_action == "end_hand" else None

    # 席番号表現（シート1 / seat 3 等）を除去してから金額を抽出する。
    # 除去しないと parse_amount() が席番号の数字を最初の金額候補として拾ってしまう。
    # 語の直後の仮名の数（「ベトナナ」の「ナナ」）は漢数字にしてから読む（ADR-0062）。
    # 役名に額は無い（「2ペア」の 2 を額にしない）。
    amount = (AmountParse(0) if hand_name else
              parse_amount_ex(_strip_seat_references(_kana_amount_to_kanji(norm, span_end))))
    if amount.ambiguous:
        flags.append("ambiguous_amount")

    return AudioEvent(
        action=found_action,
        amount=amount.value,
        timestamp=time.time(),
        raw_text=text,
        seat=_extract_seat_no(norm),
        confidence=confidence,
        position=parse_position(norm),
        parse_flags=tuple(flags),
        utterance_start_ts=utterance_start_ts,
        hand_name=hand_name,
    )


def _hand_name_for(keyword_text: str) -> Optional[str]:
    """照合に使った語（正規化済み）が役名なら pokerkit の役名を返す。"""
    kw = keyword_text.lower()
    for keyword, name in HAND_NAME_KEYWORDS.items():
        if _to_katakana(unicodedata.normalize("NFKC", keyword)).lower() == kw:
            return name
    return None


# 1 発話の書き起こしに使うトークン数の上限 = 基本 + 音の長さ（秒）あたり。人の発話（1 秒に 10 トークン前後）
# の倍以上を残し、雑音への幻聴のループ（プロンプトの語を 448 トークンまで繰り返して 1 回 15〜18 秒かかった,
# 店舗の 5 回目の通しテスト）を早く打ち切る。打ち切った書き起こしは `is_implausibly_long` で雑音になる。
_TOKENS_BASE = 40
_TOKENS_PER_SEC = 20
_TOKENS_MAX = 200     # プロンプトと合わせてモデルの上限（448）を超えない


@dataclass(frozen=True)
class Recognition:
    """1 発話の書き起こし（`WhisperTranscriber.recognize`）。"""

    text: str
    confidence: Optional[float]
    no_speech: bool = False     # 声が無い（VAD）ので Whisper にかけなかった


class WhisperTranscriber:
    """faster-whisper を使ってマイク音声をテキストに変換するクラス。"""

    def __init__(
        self, model_size: str = "medium", language: str = "ja",
        beam_size: int = 5, temperature_fallback: bool = False,
        vad_threshold: float = 0.5,
    ) -> None:
        """
        Args:
            beam_size: ビーム幅（小さいほど速い）。config `audio.beam_size`。
            temperature_fallback: 自信の低い書き起こしを温度を上げて最大 5 回やり直すか（faster-whisper の
                既定）。雑音では毎回やり直しになり 1 発話に 10〜80 秒かかって認識が数分遅れたので、
                既定では行わない（店舗の実測, ADR-0063）。config `audio.temperature_fallback`。
            vad_threshold: 声か（Silero VAD, faster-whisper に同梱）の閾値。声が見つからない音（札を混ぜる音・
                チップの音）は Whisper にかけない（雑音にプロンプトを繰り返す幻聴と、その認識待ちを防ぐ）。
                0 で使わない。config `audio.vad_threshold`。
        """
        self._language = language
        self._beam_size = max(1, int(beam_size))
        self._temperature = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0) if temperature_fallback else 0.0
        self._vad_threshold = max(0.0, float(vad_threshold))
        # 読み込めなかった理由（CLI / audio_check が表示する）。読み込めたら None。
        self.load_error: Optional[str] = None
        logger.info("Loading Whisper model: %s", model_size)
        try:
            from faster_whisper import WhisperModel  # type: ignore[import]

            self._model = WhisperModel(model_size, device="cpu", compute_type="int8")
        except ImportError:
            logger.warning(
                "faster-whisper not installed. WhisperTranscriber will not function."
            )
            self._model = None
            self.load_error = "faster-whisper が入っていません"
        except Exception as e:
            # 初回のモデル取得（約 1.5 GB）がネットワーク不通で失敗した等。起動は止めない
            # （キーボードからの読み上げ文でアクションを入れられる）。
            logger.exception("Could not load Whisper model %r", model_size)
            self._model = None
            self.load_error = f"{type(e).__name__}: {e}"

    @property
    def ready(self) -> bool:
        """モデルを読み込めたか。"""
        return self._model is not None

    def transcribe(self, audio_bytes: bytes) -> str:
        """PCM16 音声バイト列をテキストに変換して返す（信頼度を捨てる後方互換版）。"""
        return self.transcribe_with_confidence(audio_bytes)[0]

    def transcribe_with_confidence(
        self, audio_bytes: bytes
    ) -> tuple[str, Optional[float]]:
        """PCM16 音声バイト列を (テキスト, 信頼度[0,1]) に変換する（`recognize` の後方互換版）。"""
        result = self.recognize(audio_bytes)
        return result.text, result.confidence

    def has_speech(self, audio_array) -> bool:
        """音に声が含まれるか（Silero VAD）。VAD を使わない・使えないときは True（Whisper にかける）。"""
        if self._vad_threshold <= 0:
            return True
        try:
            from faster_whisper.vad import VadOptions, get_speech_timestamps  # type: ignore[import]

            options = VadOptions(threshold=self._vad_threshold, min_speech_duration_ms=100)
            return bool(get_speech_timestamps(audio_array, options))
        except Exception:  # noqa: BLE001 — VAD が動かなければ従来どおり全部かける
            logger.warning("VAD が使えないので、有音の音をすべて Whisper にかけます", exc_info=True)
            self._vad_threshold = 0.0
            return True

    def recognize(self, audio_bytes: bytes) -> Recognition:
        """PCM16 音声バイト列を書き起こす。変換失敗・モデル未ロード時は空（クラッシュしない）。

        入力は 16kHz モノラル PCM16 固定を前提とする（faster-whisper は配列長から
        16kHz を仮定するため sample_rate は受け取らない）。
        confidence は各 segment の avg_logprob（対数確率）平均を exp で 0..1 に写像し、
        no_speech_prob が高い segment はその分減衰させたもの（ADR-C T4:
        プロンプト由来のオウム返しハルシネーションは無音区間で no_speech_prob が
        高く出るため、制御語ガードの入力として意味を持つ）。segment が無ければ None。
        """
        if self._model is None:
            return Recognition("", None)
        try:
            import math

            import numpy as np

            audio_array = (
                np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
            )
            if not self.has_speech(audio_array):
                return Recognition("", None, no_speech=True)
            seconds = len(audio_array) / 16000
            max_new_tokens = min(_TOKENS_MAX, _TOKENS_BASE + math.ceil(_TOKENS_PER_SEC * seconds))
            segments, _ = self._model.transcribe(
                audio_array,
                language=self._language,
                initial_prompt=WHISPER_PROMPT_JA,
                beam_size=self._beam_size,
                temperature=self._temperature,
                condition_on_previous_text=False,
                max_new_tokens=max_new_tokens,
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
            return Recognition(text, confidence)
        except Exception:
            logger.exception("Whisper transcription failed")
            return Recognition("", None)


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
      レイズの額は常にトータル（by 読み上げの疑いは立てない = オーナー確認 2026-09-26。採用は to 解釈 + snap）。
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

    # レイズの額は常にトータル（「レイズ 2500」= 2500 にする。上乗せ分は言わない = オーナー確認 2026-09-26）。
    # 最小レイズ未満は聞き違いとして、下の snap + review に任せる（「追加額」とは読み替えない）。

    # V4: チップの最小単位への round 寄せ（合法レンジ内に収まる場合のみ）。単位は SB と BB の最大公約数
    # （100/200 の卓の「2500」を 2400 にしない = 言った数字どおり, ADR-0062）。chip が無い（legacy）なら bb。
    # reason 名は画面の説明文（shared/hand_replay）と合わせて従来の rounded_to_bb のまま。
    unit = ctx.chip or bb
    if unit > 0 and val % unit != 0:
        rounded = int(round(val / unit)) * unit
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
