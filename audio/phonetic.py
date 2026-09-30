"""音の近さでアクションの語を読む（ADR-0056 追記 1 の S2）。

店舗の書き起こしには、辞書（`ACTION_KEYWORDS`）に無いゆれが毎回のように新しく出る（「ヘッドアップ」「ヘッドホップ」
「ヘッドロップ」「ヘッドゾップ」= ヘッズアップ、「ソーダウン」= ショーダウン、「チッカーランド」= チェックアラウンド）。
見つけるたびに語を足す代わりに、書き起こしの片仮名の語とアクションの語の **音の近さ** で読む。

近さ = モーラ（拍）単位の編集距離を、アクションの語のモーラ数で割ったもの。Whisper の聞き違いは音の近い方へずれる
（濁点の有無・フ/ホ・シ/ス・チ/ト・ウ/オ・イ/エ、長音・促音・撥音の有無、語尾の脱落）ので、それらは安く数え、
無関係な音への置き換え（とくに語頭）は高く数える。短い語ほど 1 音の違いで別の語になる（ベット / ネット）ので、
しきい値を厳しくする。どの語とも近くない・意味の違う 2 つの語に同じくらい近い（「ゴールド」は ホールド にも
ゴール にも近い）・アクションでない語（フロップ・オーライ）にいちばん近いなら読まない。
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Optional

from core.constants import ACTION_KEYWORDS, HAND_NAME_KEYWORDS

# ---------------------------------------------------------------------------
# モーラ

# (子音, 母音, 種類)。種類: "" = ふつう / "long" = 長音（ー、エイ・オウの 2 つ目）/ "Q" = 促音 / "N" = 撥音
Mora = tuple[str, str, str]

_ROWS = {
    "": "アイウエオ", "k": "カキクケコ", "g": "ガギグゲゴ", "s": "サシスセソ", "z": "ザジズゼゾ",
    "t": "タチツテト", "d": "ダヂヅデド", "n": "ナニヌネノ", "h": "ハヒフヘホ", "b": "バビブベボ",
    "p": "パピプペポ", "m": "マミムメモ", "r": "ラリルレロ",
}
_BASE: dict[str, tuple[str, str]] = {
    ch: (cons, vowel) for cons, chars in _ROWS.items() for ch, vowel in zip(chars, "aiueo")
}
_BASE.update({
    "シ": ("sh", "i"), "ジ": ("j", "i"), "チ": ("ch", "i"), "ツ": ("ts", "u"), "ヂ": ("j", "i"),
    "ヅ": ("z", "u"), "フ": ("f", "u"), "ヤ": ("y", "a"), "ユ": ("y", "u"), "ヨ": ("y", "o"),
    "ワ": ("w", "a"), "ヰ": ("", "i"), "ヱ": ("", "e"), "ヲ": ("", "o"), "ヴ": ("v", "u"),
    "ヵ": ("k", "a"), "ヶ": ("k", "e"),
})
_SMALL_VOWELS = {"ァ": "a", "ィ": "i", "ゥ": "u", "ェ": "e", "ォ": "o", "ヮ": "a"}
_SMALL_Y = {"ャ": "a", "ュ": "u", "ョ": "o"}


def _combine(base: str, cons: str, small: str) -> tuple[str, str]:
    """小さい仮名を前の仮名と 1 モーラにする（フォ = f+o、チェ = ch+e、キャ = ky+a）。"""
    if small in _SMALL_Y:
        vowel = _SMALL_Y[small]
        if cons in ("sh", "ch", "j", "y"):
            return cons, vowel
        return (cons + "y" if cons else "y"), vowel
    vowel = _SMALL_VOWELS[small]
    if cons == "":
        if base == "ウ":
            return "w", vowel                    # ウィ・ウェ・ウォ
        if base == "イ" and vowel == "e":
            return "y", vowel                    # イェ
        return "", vowel
    return cons, vowel                           # フォ・チェ・ティ・ドゥ・ヴァ


def morae(text: str) -> list[Mora]:
    """カタカナ（ひらがなも可）の語をモーラの列にする。仮名以外の文字は無視する。

    「ー」は前の母音を伸ばした音、エイ・オウの 2 つ目（レイズ = レーズ、ショウ = ショー）と同じ母音の続き
    （カア = カー）も長音にする（書き起こしで表記が揺れても同じ音として比べる）。
    """
    text = unicodedata.normalize("NFKC", text)
    out: list[list[str]] = []                   # [子音, 母音, 種類, 元の仮名]
    for ch in text:
        if "ぁ" <= ch <= "ゖ":
            ch = chr(ord(ch) + 0x60)
        if ch == "ッ":
            out.append(["Q", "", "Q", ch])
        elif ch == "ン":
            out.append(["N", "", "N", ch])
        elif ch == "ー":
            prev = out[-1][1] if out else ""
            out.append(["", prev, "long", ch])
        elif ch in _SMALL_VOWELS or ch in _SMALL_Y:
            if out and out[-1][2] == "" and out[-1][3] in _BASE:
                cons, vowel = _combine(out[-1][3], out[-1][0], ch)
                out[-1][0], out[-1][1], out[-1][3] = cons, vowel, out[-1][3] + ch
            elif ch in _SMALL_Y:
                out.append(["y", _SMALL_Y[ch], "", ch])
            else:
                out.append(["", _SMALL_VOWELS[ch], "", ch])
        elif ch in _BASE:
            cons, vowel = _BASE[ch]
            prev = out[-1] if out else None
            if (cons == "" and prev is not None and prev[2] in ("", "long") and prev[1]
                    and (vowel == prev[1] or (prev[1], vowel) in (("e", "i"), ("o", "u")))):
                out.append(["", prev[1], "long", ch])
            else:
                out.append([cons, vowel, "", ch])
    return [(c, v, k) for c, v, k, _ in out]


# ---------------------------------------------------------------------------
# モーラの距離

# 有声・無声の違いだけ（コ/ゴ、ペ/ベ、ス/ズ）
_VOICING = {frozenset(p) for p in (
    ("k", "g"), ("s", "z"), ("sh", "j"), ("t", "d"), ("ch", "j"), ("ts", "z"), ("p", "b"),
    ("f", "v"), ("ky", "gy"), ("py", "by"),
)}
# 聞き違えやすい近い子音（フォ/ホ、ショ/ソ、チェ/テ、ズ/ド、ド/ロ、ア/ワ）
_CLOSE = {frozenset(p) for p in (
    ("h", "f"), ("s", "sh"), ("z", "j"), ("t", "ch"), ("t", "ts"), ("ch", "sh"), ("ch", "ts"),
    ("d", "z"), ("d", "r"), ("b", "v"), ("", "w"), ("", "y"), ("", "h"),
    ("k", "ky"), ("g", "gy"), ("n", "ny"), ("h", "hy"), ("b", "by"), ("p", "py"), ("m", "my"),
    ("r", "ry"),
)}
# 近い母音（ウ/オ、イ/エ）
_CLOSE_VOWELS = {frozenset(("u", "o")), frozenset(("i", "e"))}

_SUB_VOICING = 0.3
_SUB_CLOSE = 0.4
_SUB_OTHER = 0.8
_INDEL_WEAK = 0.5          # 長音・促音・撥音の有無
_INDEL_VOWEL = 0.6         # 母音だけの拍（アラウンド → アランド の「ウ」）
_INDEL_TRUNCATED = 0.6     # 語尾の 1 拍が落ちた（フォールド → フォール）
_INITIAL_PENALTY = 1.5     # 語頭の無関係な音への置き換え・脱落は高く数える


def _consonant_cost(a: str, b: str) -> float:
    if a == b:
        return 0.0
    pair = frozenset((a, b))
    if pair in _VOICING:
        return _SUB_VOICING
    if pair in _CLOSE:
        return _SUB_CLOSE
    return _SUB_OTHER


def _vowel_cost(a: str, b: str) -> float:
    if a == b:
        return 0.0
    return _SUB_CLOSE if frozenset((a, b)) in _CLOSE_VOWELS else _SUB_OTHER


def substitution_cost(a: Mora, b: Mora) -> float:
    """1 拍を別の 1 拍に置き換える重さ（0 = 同じ音、1 = 無関係な音）。"""
    special_a, special_b = a[2] in ("Q", "N"), b[2] in ("Q", "N")
    if special_a or special_b:
        if a[2] == b[2]:
            return 0.0
        return _INDEL_WEAK if special_a and special_b else 1.0
    if a[:2] == b[:2]:
        return 0.0
    return min(1.0, _consonant_cost(a[0], b[0]) + _vowel_cost(a[1], b[1]))


def _indel_cost(seq: list[Mora], i: int) -> float:
    kind = seq[i][2]
    if kind in ("Q", "N", "long"):
        return _INDEL_WEAK
    if seq[i][0] == "" and i > 0 and seq[i - 1][1]:
        return _INDEL_VOWEL
    return 1.0


def distance(heard: list[Mora], word: list[Mora]) -> float:
    """聞こえた語（モーラ列）とアクションの語の距離 = 編集の重さの合計 / アクションの語のモーラ数。"""
    n, m = len(heard), len(word)
    if m == 0:
        return float("inf")
    d = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        cost = _indel_cost(heard, i - 1)
        d[i][0] = d[i - 1][0] + (cost * _INITIAL_PENALTY if i == 1 and cost >= 1.0 else cost)
    for j in range(1, m + 1):
        cost = _indel_cost(word, j - 1)
        d[0][j] = d[0][j - 1] + (cost * _INITIAL_PENALTY if j == 1 and cost >= 1.0 else cost)
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            sub = substitution_cost(heard[i - 1], word[j - 1])
            if i == 1 and j == 1 and sub >= _SUB_OTHER:
                sub *= _INITIAL_PENALTY
            missing = _indel_cost(word, j - 1)
            if i == n and j == m and word[j - 1][2] == "":
                missing = min(missing, _INDEL_TRUNCATED)     # 語尾の脱落
            d[i][j] = min(
                d[i - 1][j - 1] + sub,
                d[i - 1][j] + _indel_cost(heard, i - 1),     # 聞こえた語に余分な拍
                d[i][j - 1] + missing,                       # 聞こえた語に拍が足りない
            )
    return d[n][m] / m


# ---------------------------------------------------------------------------
# 照合する語

# 音の近さで読むアクション（ほかのアクション = 勝者・チョップ・マック・役名・ハンド開始/終了は、聞き違えたときの
# 害が大きいか、ありふれた語と近いので読まない。近ければ「読まない」側の語として働く）
_CANONICAL = {
    "bet": "ベット", "call": "コール", "raise": "レイズ", "check": "チェック", "fold": "フォールド",
    "allin": "オールイン", "showdown": "ショーダウン", "heads_up": "ヘッズアップ",
}
_CHECK_AROUND = "チェックアラウンド"
# アクションの語（正準）。事後の採点（`tools/rescore_audio.py`）で、発話ごとにこれらの確からしさも測る。
ACTION_WORDS = (*_CANONICAL.values(), _CHECK_AROUND)
_REWRITE_OVERRIDES = {"チッカーランド": _CHECK_AROUND, "チェックアラウンド": _CHECK_AROUND}
_NOT_FUZZY_KEYWORDS = frozenset({"マック"})   # 3 拍でありふれた音（バック・ハック）に近い
# アクションでない語（卓でよく言う語・相づち）。アクションの語よりこれらに近ければ読まない。
_DECOY_WORDS = (
    "フロップ", "ターン", "リバー", "ラストカード", "ボード", "ポット", "サイドポット", "メインポット",
    "ボタン", "ブラインド", "スモール", "ビッグ", "アンティ", "ディーラー", "チップ", "カード",
    "ハンド", "シート", "アクション", "ミニマム", "シャッフル", "エース", "キング", "クイーン",
    "ジャック", "オーライ", "オッケー", "オーケー", "サンキュー", "ナイス", "ラッキー", "ドンマイ",
    "ストップ", "スタート", "ラスト", "ゲーム", "タイム", "オーバー", "トップ",
)
# そのままの音で聞こえたらアクションでない語。上の語と違い、近い語の判定には加えない（オープンを加えると、
# 1 音違いの店舗のゆれ = オーイン・オーリンもオールインと読めなくなる）。
_EXACT_NOT_ACTIONS = (
    "オープン",   # 札を見せる（店舗 2026-09-29 9d1d8536 ハンド 4: 「フォールド オープン」をオールインと読み、ターンがオールインに）
)
_MIN_HEARD_MORAE = 3
_MARGIN = 0.15


def _threshold(word_morae: int) -> float:
    """アクションの語のモーラ数ごとの距離の上限（短い語ほど厳しい: 3 拍は近い音 1 つまで）。"""
    if word_morae <= 3:
        return 0.2
    if word_morae == 4:
        return 0.35
    return 0.45


def _is_katakana_word(word: str) -> bool:
    return bool(word) and all("ァ" <= ch <= "ヺ" or ch == "ー" for ch in word)


@dataclass(frozen=True)
class _Target:
    word: str
    morae: tuple[Mora, ...]
    rewrite: Optional[str]      # 読み替える語（None = アクションでない語）


def _build_targets() -> tuple[_Target, ...]:
    targets: dict[str, _Target] = {}
    for keyword, action in ACTION_KEYWORDS.items():
        word = unicodedata.normalize("NFKC", keyword)
        if not _is_katakana_word(word):
            continue
        rewrite = None
        if action in _CANONICAL and word not in _NOT_FUZZY_KEYWORDS and keyword not in HAND_NAME_KEYWORDS:
            rewrite = _REWRITE_OVERRIDES.get(word, _CANONICAL[action])
        seq = tuple(morae(word))
        if rewrite is not None and len(seq) < _MIN_HEARD_MORAE:
            continue                              # 2 拍の語（ベト）は辞書の完全一致だけで読む
        targets[word] = _Target(word, seq, rewrite)
    targets[_CHECK_AROUND] = _Target(_CHECK_AROUND, tuple(morae(_CHECK_AROUND)), _CHECK_AROUND)
    for word in _DECOY_WORDS:
        targets.setdefault(word, _Target(word, tuple(morae(word)), None))
    return tuple(targets.values())


_TARGETS = _build_targets()
_EXACT_NOT_ACTION_MORAE = frozenset(tuple(morae(word)) for word in _EXACT_NOT_ACTIONS)


@dataclass(frozen=True)
class PhoneticMatch:
    """音の近さで読んだ結果。"""

    heard: str          # 書き起こしの語
    word: str           # いちばん近いアクションの語（辞書のゆれを含む）
    rewrite: str        # 読み替える語（正準のアクションの語）
    distance: float
    runner_up: float    # 意味の違う語のうち、いちばん近い語の距離
    word_morae: int = 0  # いちばん近い語のモーラ数（4 拍以下の短い語は前後が区切りのときだけ読む）


def rank(heard: str) -> list[tuple[float, str, Optional[str]]]:
    """書き起こしの語と照合する語すべての (距離, 語, 読み替える語) を近い順に返す（事後推定・調査用）。"""
    seq = morae(heard)
    return sorted((distance(seq, list(t.morae)), t.word, t.rewrite) for t in _TARGETS)


def sounds_like(heard: str, word: str, after: str = "") -> bool:
    """書き起こしの語 heard が、音の近さで word と読めるか（アクションでない語 = ターン・ハンド…のほうが近ければ
    読まない）。

    アクションの語（辞書で読めた語 `after`）に続けて言う部分 = 「チェックアラウンド」の「アラウンド」、「チェックレイズ」の
    「レイズ」を、書き起こしゆれ（「ラウンド」「アランド」「アウンド」「ランド」「レース」）を並べずに照合する。
    - `after` の終わりの音に続けて比べる（「チェック」の「ク」に続く「アラウンド」の「ア」は、続けて言うと消えやすい
      = 語の頭の音として重く数えない）。
    - 前の語が確かなので、意味の違う語との差（`_MARGIN`）は求めず、いちばん近いことだけを求める。
    """
    context = morae(after)[-1:]
    seq, target = context + morae(heard), morae(word)
    if len(seq) == len(context) or not target:
        return False

    def cost(to: tuple[Mora, ...] | list[Mora]) -> float:
        full = context + list(to)
        return distance(seq, full) * len(full) / len(to)

    d = cost(target)
    return d <= _threshold(len(target)) and all(d < cost(t.morae) for t in _TARGETS if t.rewrite is None)


def match_keyword(heard: str) -> Optional[PhoneticMatch]:
    """書き起こしの語（片仮名）が、音の近さでアクションの語と読めるか。読めなければ None。"""
    seq = morae(heard)
    if len(seq) < _MIN_HEARD_MORAE or tuple(seq) in _EXACT_NOT_ACTION_MORAE:
        return None
    scored = sorted(
        ((distance(seq, list(t.morae)), t) for t in _TARGETS), key=lambda x: (x[0], x[1].word),
    )
    best_distance, best = scored[0]
    if best.rewrite is None or best_distance > _threshold(len(best.morae)):
        return None
    runner_up = next((dist for dist, t in scored[1:] if t.rewrite != best.rewrite), float("inf"))
    if runner_up - best_distance < _MARGIN:
        return None
    return PhoneticMatch(heard, best.word, best.rewrite, best_distance, runner_up, len(best.morae))
