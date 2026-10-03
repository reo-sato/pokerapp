#!/usr/bin/env python3
"""tools/voice_style.py — 宣言の声と雑談の声を数値で見分けられるか（オーナーの問い, 2026-10-03）

オーナーの見立て: 配る人はアクションを宣言するときと雑談するときで声の使い方を切り替えていて、卓のプレイヤーは
それを聞き分けている。店舗のログ（書き起こし + 発話ごとの WAV）で、声の高さと大きさを**同じセッションの中で**比べる。

- 発話の分け方（Whisper の文字から）: D 宣言（賭けの言葉だけ・15 文字以内）/ A 進行の言葉（役の名前・ヘッズアップ・
  ポットなど、賭けではない配る人の言葉だけ）/ C 雑談（卓の言葉が無い文。読めた賭けの言葉から 15 秒以内）。
- 声の出し方ではないもので見かけ上分かれないように数える: セッションの中の組だけで比べる（マイクの経路・日による
  録音の大きさ）/ 長さをそろえた組（宣言は短く雑談は長い）/ 言葉の違う A 対 C（「コール」の音そのもの）/ 1 人 n 役の
  セッションを主に（誰が話したか）/ 録音の区切り方を全部同じ規則にそろえる（続きの断片・5 秒で切れた発話・短い発話）。
- 判定（数字を見る前に計画で固定）: 1 人 n 役のセッションで、指数 z(高さ) + z(大きさ) の AUC ≥ 0.70 かつ 95% 区間の
  下限 ≥ 0.60、長さをそろえた組で ≥ 0.65、A 対 C で ≥ 0.65。
- 雑談と宣言が 1 つの音声ファイルに入った発話（「こんなの入るの、いきなり? コール」。オーナーの指摘 2026-10-03）は
  雑談に数えない。文字の順から宣言が最後（または最初）と分かるものは、ファイルの中を声の切れ目で区切り、宣言の区切りと
  同じファイルの雑談の区切りを比べる（同じ人・同じマイク・同じ時点 = 声の切り替えのいちばん直接の確かめ）。判定:
  ファイルの中の AUC ≥ 0.70 かつ下限 ≥ 0.55、15 ファイル以上。

推定器の経路のファイル（`integration/estimator.py:CONTENT_FILES`）は読み込まない（推定の指紋は変わらない）。

    TZ=Asia/Tokyo python tools/voice_style.py <ログのフォルダ or zip>... --one-person 027e4b15,d0f055fb,...
    TZ=Asia/Tokyo python tools/voice_style.py <...> --one-person <...> --html voice.html --json voice.json
"""
from __future__ import annotations

import argparse
import base64
import html
import json
import logging
import math
import re
import shutil
import sys
import tempfile
import unicodedata
import wave
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional, Sequence

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from audio.recognizer import is_announcement, is_question, parse_actions  # noqa: E402
# 語の位置と残りの言葉はライブの読み取りと同じ見方で（会話の中の語として捨てた語も位置は分かる）
from audio.recognizer import _AMOUNT_TOKEN, _distinct_keywords, _keyword_matches, _residue, _to_katakana  # noqa: E402
from audio.second_ear import apply_ear  # noqa: E402
from tools.eval_store import _is_noise, _read_json, _read_jsonl, hand_windows, truth_hands  # noqa: E402

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
JST = timezone(timedelta(hours=9))

# 録音の区切り方（`audio/recorder.py` と同じ: 1024 サンプル = 64 ms の塊の RMS で声かを決め、前に 5 塊・後ろに 7 塊）
CHUNK = 1024
GATE_RMS = 300.0                 # `audio.speech_rms` の既定
MAX_CHUNKS = 79                  # 5 秒で切った発話（`_MAX_BUFFER_SECONDS = 5.0` → 79 塊 = 5.056 秒）
MIN_GATE_CHUNKS = 3              # 日によって短い発話の残し方の設定が違うので、全セッションを同じ規則にそろえる

# 枠（25 ms・10 ms ずつ）と声の高さ（YIN, 70〜450 Hz）
FRAME = 400
HOP = 160
F0_MIN, F0_MAX = 70.0, 450.0
TAU_MIN = int(SAMPLE_RATE / F0_MAX)              # 35
TAU_MAX = int(math.ceil(SAMPLE_RATE / F0_MIN))   # 229
FFT_N = 1024                                     # FRAME + TAU_MAX = 629 より長い（相互相関が循環しない）
YIN_THRESHOLD = 0.15
YIN_VOICED_MAX = 0.2             # これより上の谷はきしみ・雑音（高さにしない）
ACTIVE_DB = 10.0                 # 話している枠 = 最初の塊（前の無音）より 10 dB 以上
MIN_PITCH_FRAMES = 5
MIN_SLOPE_FRAMES = 10
END_SEC = 0.3                    # 終わりの下がり方を測る長さ
CLIP_LIMIT = 32767
CLIP_FRACTION = 0.001

# 発話の分け方
D_MAX_CHARS = 15
NEAR_SEC = 15.0                  # 読めた賭けの言葉からの近さ（推定器の `canned_near_sec`・51 の選び方と同じ）
BETTING = frozenset({"fold", "check", "call", "bet", "raise", "allin"})
DEALER_CALLS = frozenset({"showdown", "heads_up", "players_left", "end_hand", "new_hand", "winner"})
_STREET_CALL = re.compile(r"ターンカード|ラストカード|リバーカード|フロップ")
# 分け方の 2 版（数字を見たあとに直した。次のデータで前向きに確かめる）: 卓の言葉とつなぎの言葉だけの発話
# （「アクションです。」「ターンです。」「シート3です。」）も進行の言葉、雑談はひらがな・漢字の文だけ
_TABLE_TALK = re.compile(r"アクション|ターン|リバー|フロップ|ラストカード|シート|ボタン|ポット|サイド|メイン")
LABEL_RULES = (1, 2)
# Whisper の締めの定型文（`is_prompt_echo` が拾わない。無音への幻聴か本当の挨拶か分からないので雑談に入れない）
_OUTROS = frozenset({"どうもありがとうございました", "ありがとうございました", "お疲れ様でした", "おつかれさまでした",
                     "さようなら", "また会いましょう", "おやすみなさい", "ご清聴ありがとうございました"})
_NON_TEXT = re.compile(r"[\s、。,.・!?！？ー〜~…「」()（）]+")

# 数え方（計画で固定）
MATCH_RATIO = 1.5                # 長さをそろえた組 = 話した長さが 1.5 倍以内
SEED = 20261003
N_BOOT = 2000
N_PERM = 2000
H1_AUC, H1_LOWER, H1_MATCHED, H1_WORDS = 0.70, 0.60, 0.65, 0.65
MIN_MATCHED_PAIRS, MIN_A = 30, 15
H1B_AUC, H1B_LOWER, MIN_FUSED = 0.70, 0.55, 15
LAMBDA = 1.0

# 1 つの音声ファイルの中の区切り（声の切れ目 = 0.1 秒以上話していない枠）
SEGMENT_GAP_FRAMES = 10
PURE_RESIDUE = 2                 # 宣言だけ = アクションの語・額・席・つなぎの言葉を除いて 2 文字以下
FUSED_RESIDUE = 4                # 雑談の部分 = 4 文字以上

CLASS_NAMES = {"D": "宣言", "A": "進行の言葉", "C": "雑談"}
FUSED = ("M_end", "M_start")     # 雑談のあとに宣言 / 宣言のあとに雑談（1 つのファイル）
FEATURE_NAMES = {"f0": "高さ", "level": "大きさ", "duration": "長さ", "f0_range": "高さの幅",
                 "end_slope": "終わりの下がり方", "tilt": "高い音の比"}
_SKIP_NAMES = {"no_wav": "WAV なし", "continuation": "続きの断片", "hard_cut": "5 秒で切れた", "short": "短すぎ",
               "clipped": "音が割れた", "no_pitch": "高さが取れない", "noise": "決まり文句・雑音",
               "mixed": "長い・混ざった", "rescued": "耳が読んだ", "question": "質問", "outro": "締めの定型文",
               "far": "賭けの言葉から遠い", "one_segment": "区切れない（混ざったファイル）",
               "not_chat": "雑談の文ではない（片仮名の語・相づち）"}


# ───────────────────────── 音 ─────────────────────────

def read_wav(path: Path) -> np.ndarray:
    """PCM16 の WAV を int16 の尺度の float64（16 kHz モノラル）にする。"""
    with wave.open(str(path), "rb") as src:
        rate, channels, width = src.getframerate(), src.getnchannels(), src.getsampwidth()
        data = src.readframes(src.getnframes())
    if width != 2:
        raise ValueError(f"PCM16 ではありません（{width * 8} bit）")
    x = np.frombuffer(data, dtype="<i2").astype(np.float64)
    if channels > 1:
        x = x.reshape(-1, channels).mean(axis=1)
    if rate != SAMPLE_RATE and len(x):
        n = max(1, round(len(x) * SAMPLE_RATE / rate))
        x = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x)
    return x


@dataclass
class Gate:
    """録音のゲート（`audio/recorder.py`）が見た塊。"""

    chunk_rms: np.ndarray
    voiced_chunks: int
    starts_voiced: bool          # 最初の塊から声 = 5 秒で切れた発話の続きの断片（前の無音が無い）
    hard_cut: bool               # 5 秒で切った（雑談の長い発話が途中で切れている）


def gate_of(x: np.ndarray, rms_threshold: float = GATE_RMS) -> Gate:
    n = len(x) // CHUNK
    if n == 0:
        return Gate(np.zeros(0), 0, False, False)
    rms = np.sqrt(np.mean(x[: n * CHUNK].reshape(n, CHUNK) ** 2, axis=1))
    voiced = rms >= rms_threshold
    return Gate(rms, int(voiced.sum()), bool(voiced[0]), n >= MAX_CHUNKS)


def _db(power: "float | np.ndarray") -> "float | np.ndarray":
    return 10.0 * np.log10(np.maximum(power, 1e-12))


def frames(x: np.ndarray, length: int = FRAME, hop: int = HOP) -> np.ndarray:
    if len(x) < length:
        return np.zeros((0, length))
    n = 1 + (len(x) - length) // hop
    idx = np.arange(length)[None, :] + hop * np.arange(n)[:, None]
    return x[idx]


VadFn = Callable[[np.ndarray], list[tuple[int, int]]]


def silero_vad() -> Optional[VadFn]:
    """声の区間の検出（faster_whisper の Silero VAD）。前後の余白 0・0.1 秒の無音で区切る（既定のままだと全体が声に
    なる）。faster_whisper が無ければ None（エネルギーだけで決める）。"""
    try:
        from faster_whisper.vad import VadOptions, get_speech_timestamps
    except ImportError:
        return None
    options = VadOptions(threshold=0.5, min_speech_duration_ms=0, min_silence_duration_ms=100, speech_pad_ms=0)

    def run(x: np.ndarray) -> list[tuple[int, int]]:
        audio = (x / 32768.0).astype(np.float32)
        return [(int(s["start"]), int(s["end"])) for s in get_speech_timestamps(audio, options)]

    return run


def yin(x: np.ndarray, starts: np.ndarray) -> np.ndarray:
    """枠ごとの基本周波数（Hz、取れなければ NaN）。差の関数 = 累積和のエネルギー − 2 × 相互相関（ゼロ詰めの FFT で
    循環しない）を両側のエネルギーで正規化し、累積平均で割る。しきい値を下回った最初の谷から局所最小まで下り
    （2 倍周期の誤りを防ぐ）、放物線で補間する。先読みが足りない枠は NaN。"""
    out = np.full(len(starts), np.nan)
    ok = starts + FRAME + TAU_MAX <= len(x)
    if not ok.any():
        return out
    idx = np.arange(FRAME + TAU_MAX)[None, :] + starts[ok][:, None]
    seg = x[idx]
    seg = seg - seg.mean(axis=1, keepdims=True)
    a = np.fft.rfft(seg[:, :FRAME], FFT_N)
    b = np.fft.rfft(seg, FFT_N)
    r = np.fft.irfft(np.conj(a) * b, FFT_N)[:, : TAU_MAX + 1]
    cs = np.concatenate([np.zeros((len(seg), 1)), np.cumsum(seg ** 2, axis=1)], axis=1)
    taus = np.arange(TAU_MAX + 1)
    e0 = cs[:, FRAME][:, None]
    et = cs[:, taus + FRAME] - cs[:, taus]
    d = np.maximum(e0 + et - 2.0 * r, 0.0) / np.maximum(e0 + et, 1e-9)
    d[:, 0] = 0.0
    run = np.cumsum(d[:, 1:], axis=1)
    cmnd = np.ones_like(d)
    cmnd[:, 1:] = d[:, 1:] * taus[1:] / np.maximum(run, 1e-12)
    f0 = np.full(len(seg), np.nan)
    for i in range(len(seg)):
        c = cmnd[i]
        below = np.nonzero(c[TAU_MIN: TAU_MAX] < YIN_THRESHOLD)[0]
        if len(below):
            t = TAU_MIN + int(below[0])
            while t + 1 < TAU_MAX and c[t + 1] < c[t]:
                t += 1
        else:
            t = TAU_MIN + int(np.argmin(c[TAU_MIN: TAU_MAX]))
        if c[t] > YIN_VOICED_MAX or t <= TAU_MIN or t >= TAU_MAX:
            continue
        den = c[t - 1] - 2.0 * c[t] + c[t + 1]
        shift = 0.5 * (c[t - 1] - c[t + 1]) / den if den > 0 else 0.0
        f0[i] = SAMPLE_RATE / (t + max(-0.5, min(0.5, shift)))
    out[ok] = f0
    return out


def semitones(f0: "float | np.ndarray") -> "float | np.ndarray":
    """100 Hz からの半音。"""
    return 12.0 * np.log2(np.asarray(f0) / 100.0)


def _fold_octaves(st: np.ndarray) -> np.ndarray:
    """クリップの中央値から 1 オクターブ跳んだ枠を寄せる（寄せても離れていれば捨てる）。"""
    if len(st) == 0:
        return st
    med = float(np.median(st))
    out = st.copy()
    for i, v in enumerate(st):
        if abs(v - med) > 9.0:
            folded = v - 12.0 * round((v - med) / 12.0)
            out[i] = folded if abs(folded - med) <= 4.0 else np.nan
    return out[~np.isnan(out)]


def segments_of(active: np.ndarray, power: np.ndarray, f0: np.ndarray,
                gap: int = SEGMENT_GAP_FRAMES) -> list[dict]:
    """話している枠のかたまり（`gap` 枠未満の切れ目はつなぐ）ごとの高さと大きさ。高さが取れない区切りは f0 = None。"""
    idx = np.nonzero(active)[0]
    if not len(idx):
        return []
    runs, first, last = [], idx[0], idx[0]
    for i in idx[1:]:
        if i - last > gap:
            runs.append((first, last))
            first = i
        last = i
    runs.append((first, last))
    out = []
    for a, b in runs:
        mask = np.zeros(len(active), dtype=bool)
        mask[a: b + 1] = True
        mask &= active
        vals = f0[mask]
        vals = vals[~np.isnan(vals)]
        folded = _fold_octaves(semitones(vals)) if len(vals) else np.zeros(0)
        out.append({"t0": round(a * HOP / SAMPLE_RATE, 3), "t1": round((b * HOP + FRAME) / SAMPLE_RATE, 3),
                    "level": round(float(_db(power[mask].mean())), 3), "voiced": int(len(folded)),
                    "f0": round(float(np.median(folded)), 3) if len(folded) >= MIN_PITCH_FRAMES else None})
    return out


def clip_features(x: np.ndarray, vad: Optional[VadFn] = None) -> dict:
    """1 発話の WAV（int16 の尺度）から声の量。除く理由があれば `skip` に入れる（ほかの値は測れた分だけ）。"""
    gate = gate_of(x)
    feats: dict = {"chunks": len(gate.chunk_rms), "gate_chunks": gate.voiced_chunks}
    if gate.starts_voiced:
        return dict(feats, skip="continuation")
    if gate.hard_cut:
        return dict(feats, skip="hard_cut")
    if gate.voiced_chunks < MIN_GATE_CHUNKS:
        return dict(feats, skip="short")
    x = x - x.mean()
    clipped = float(np.mean(np.abs(x) >= CLIP_LIMIT))
    floor_db = float(_db(gate.chunk_rms[0] ** 2))
    fr = frames(x)
    starts = np.arange(len(fr)) * HOP
    power = np.mean(fr ** 2, axis=1)
    active = _db(power) >= floor_db + ACTIVE_DB
    if vad is not None:
        speech = np.zeros(len(fr), dtype=bool)
        centers = starts + FRAME // 2
        for s, e in vad(x):
            speech |= (centers >= s) & (centers < e)
        active &= speech
    feats.update(floor_db=round(floor_db, 2), clipped=round(clipped, 5), active_frames=int(active.sum()),
                 duration=round(float(active.sum()) * HOP / SAMPLE_RATE, 3))
    if clipped > CLIP_FRACTION:
        return dict(feats, skip="clipped")
    if active.sum() < MIN_PITCH_FRAMES:
        return dict(feats, skip="short")
    feats["level"] = round(float(_db(power[active].mean())), 3)
    win = np.hanning(FRAME + 1)[:-1]
    spec = np.mean(np.abs(np.fft.rfft(fr[active] * win, 512, axis=1)) ** 2, axis=0)
    freqs = np.fft.rfftfreq(512, 1.0 / SAMPLE_RATE)
    low = spec[(freqs >= 100) & (freqs < 1000)].sum()
    high = spec[(freqs >= 1000) & (freqs < 3400)].sum()
    feats["tilt"] = round(float(_db(high) - _db(low)), 3)
    f0 = yin(x, starts[active])
    times = starts[active] / SAMPLE_RATE
    voiced = ~np.isnan(f0)
    st = semitones(f0[voiced]) if voiced.any() else np.zeros(0)
    folded = _fold_octaves(st)
    feats["voiced_frames"] = int(len(folded))
    if len(folded) < MIN_PITCH_FRAMES:
        return dict(feats, skip="no_pitch")
    f0_all = np.full(len(fr), np.nan)
    f0_all[active] = f0
    feats["segments"] = segments_of(active, power, f0_all)
    feats["f0"] = round(float(np.median(folded)), 3)
    feats["f0_range"] = round(float(np.percentile(folded, 90) - np.percentile(folded, 10)), 3)
    vt = times[voiced]
    vst = semitones(f0[voiced])
    med = float(np.median(vst))
    keep = np.abs(vst - med) <= 9.0
    vt, vst = vt[keep], vst[keep]
    tail = vt >= (vt.max() - END_SEC) if len(vt) else np.zeros(0, dtype=bool)
    if tail.sum() >= MIN_SLOPE_FRAMES:
        feats["end_slope"] = round(float(np.polyfit(vt[tail], vst[tail], 1)[0]), 3)
    return feats


# ───────────────────────── 発話の分け方 ─────────────────────────

def _chars(text: str) -> int:
    return len(_NON_TEXT.sub("", unicodedata.normalize("NFKC", text)))


def _katakana(text: str) -> str:
    norm = unicodedata.normalize("NFKC", text)
    return "".join(chr(ord(c) + 0x60) if "ぁ" <= c <= "ゖ" else c for c in norm)


def is_outro(text: str) -> bool:
    core = _NON_TEXT.sub("", unicodedata.normalize("NFKC", text))
    return core in _OUTROS


def _wager_spans(text: str) -> tuple[str, list[tuple[int, int, str]]]:
    """賭けの言葉の位置（読み取りが会話の中の語として捨てた語も含む）と額（百・千・万を含む数・100 以上・「点」の前）。
    返り値: (読み取りと同じ正規化の文字, [(位置, 長さ, 種類)])。"""
    nfkc = unicodedata.normalize("NFKC", text)
    norm = _to_katakana(nfkc)
    spans = [k for k in _distinct_keywords(_keyword_matches(norm, nfkc)) if k[2] in BETTING]
    for m in _AMOUNT_TOKEN.finditer(norm):
        word = m.group()
        after = norm[m.end(): m.end() + 1]
        big = any(c in word for c in "百千万") or (word.isdigit() and int(word) >= 100)
        if big or after in ("点", "テ"):
            spans.append((m.start(), m.end() - m.start(), "amount"))
    return norm, sorted(spans)


_SEPARATORS = frozenset("、。,.!?！？・ 　…")


def _separated_before(norm: str, pos: int) -> bool:
    return pos == 0 or norm[pos - 1] in _SEPARATORS


def _separated_after(norm: str, pos: int) -> bool:
    """語のあとが区切り（読点・空白・終わり）か。「です」「ね」などの短い語尾は越えて見る。"""
    i = pos
    while i < len(norm) and i - pos < 3 and norm[i] in "デスネヨー":
        i += 1
    return i >= len(norm) or norm[i] in _SEPARATORS


def _natural(part: str) -> bool:
    """雑談の言葉か（ひらがな・漢字が 3 文字以上）。崩れて書き起こされた片仮名の語（「チェック アウンド」=
    チェックアラウンド、「チュック」= チェック）は雑談にしない。"""
    return sum(1 for ch in part if "ぁ" <= ch <= "ゖ" or "一" <= ch <= "鿿" or "㐀" <= ch <= "䶿") >= 3


def fused_position(text: str) -> Optional[str]:
    """雑談と宣言が 1 つの発話に入っているとき、宣言が最後（`M_end`）か最初（`M_start`）か。宣言と雑談の間に区切り
    （読点・空白）があるときだけ（「コールしたらさ…」のように会話の中の語は位置を決めない）。どちらとも言えなければ
    `mixed`、賭けの言葉が無いか宣言だけなら None。"""
    norm, spans = _wager_spans(text)
    if not spans:
        return None
    first, last = spans[0][0], max(s + n for s, n, _ in spans)
    before = len(_residue(norm[:first], []))
    middle = len(_residue(norm, spans)) - before - len(_residue(norm[last:], []))
    after = len(_residue(norm[last:], []))
    if before + max(middle, 0) + after <= PURE_RESIDUE:
        return None
    nfkc = unicodedata.normalize("NFKC", text)              # 片仮名にする前（同じ長さ）
    if (before >= FUSED_RESIDUE and after <= PURE_RESIDUE and middle <= PURE_RESIDUE
            and _separated_before(norm, first) and _natural(nfkc[:first])):
        return "M_end"
    if (before <= PURE_RESIDUE and middle <= PURE_RESIDUE and after >= FUSED_RESIDUE
            and _separated_after(norm, last) and _natural(nfkc[last:])):
        return "M_start"
    return "mixed"


def _table_talk(text: str) -> bool:
    """卓の言葉とつなぎの言葉だけの発話（席・ポジション・額・「です」を除くと何も残らない）。"""
    norm = _to_katakana(unicodedata.normalize("NFKC", text))
    return bool(_TABLE_TALK.search(norm)) and not _residue(norm, [])


def classify(row: dict, rules: int = 2) -> str:
    """D 宣言 / A 進行の言葉 / C 雑談 / 雑談と宣言が 1 つのファイル（`M_end`・`M_start`）、または数えない理由
    （`noise`・`mixed`・`rescued`・`question`・`outro`）。C の「賭けの言葉から 15 秒以内」はセッションの中で別に見る
    （`label_session`）。賭けの言葉が 1 つでもあれば雑談には数えない（読み取りが会話の中の語として捨てた語も）。
    `rules=1` は事前に決めた分け方（2026-10-03 の報告の主の数字）、`rules=2` は数字を見たあとに直した分け方（卓の言葉だけの
    発話を進行の言葉に・雑談はひらがな・漢字の文だけ = `not_chat`）。"""
    text = (row.get("text") or "").strip()
    if _is_noise(row, text):
        return "noise"
    if is_question(text):
        return "question"
    at = row.get("utterance_start_ts")
    events = parse_actions(text, confidence=row.get("confidence"), utterance_start_ts=at)
    actions = {e.action for e in events}
    short = _chars(text) <= D_MAX_CHARS
    _norm, spans = _wager_spans(text)
    words = [s for s in spans if s[2] != "amount"]          # 額はポットの読み上げにも出る
    if (actions & DEALER_CALLS or is_announcement(text) or _STREET_CALL.search(_katakana(text))
            or (rules >= 2 and not actions and not words and _table_talk(text))):
        return "A" if short and not actions & BETTING and not words else "mixed"
    fused = fused_position(text)
    if fused is not None:
        return fused
    if actions & BETTING:
        return "D" if short else "mixed"
    if actions or spans:
        return "mixed"
    rescued, _ = apply_ear([], text, row.get("ear"), question=False, utterance_start_ts=at,
                           confidence=row.get("confidence"))
    if rescued:
        return "rescued"
    if is_outro(text):
        return "outro"
    if rules >= 2 and not _natural(text):
        return "not_chat"
    return "C"


def betting_times(rows: Sequence[dict]) -> list[float]:
    """読めた賭けの言葉のある発話の時刻（Whisper の文字 + 第 2 の耳。推定器が選択点にする近さの基準）。"""
    out = []
    for row in rows:
        text = (row.get("text") or "").strip()
        at = row.get("utterance_start_ts")
        if at is None or _is_noise(row, text):
            continue
        events = parse_actions(text, confidence=row.get("confidence"), utterance_start_ts=at)
        events, _ = apply_ear(events, text, row.get("ear"), question=is_question(text), utterance_start_ts=at,
                              confidence=row.get("confidence"))
        if any(e.action in BETTING for e in events):
            out.append(float(at))
    return sorted(out)


def label_session(rows: Sequence[dict], rules: int = 2) -> dict[float, str]:
    """発話の時刻 → 分け方。雑談は読めた賭けの言葉から `NEAR_SEC` 以内だけ（それ以外は `far`）。"""
    near = betting_times(rows)
    out = {}
    for row in rows:
        at = row.get("utterance_start_ts")
        if at is None:
            continue
        label = classify(row, rules)
        if label == "C" and not any(abs(float(at) - t) <= NEAR_SEC for t in near):
            label = "far"
        out[float(at)] = label
    return out


def _kind(action: str) -> str:
    return "wager" if action in ("bet", "raise") else action


def gt_matched(rows: Sequence[dict], record: Optional[dict], gt: Optional[dict]) -> set[float]:
    """真のアクションと順に対応した宣言の発話（確かめ用）。ハンドの窓は記録のハンドの時刻（再生しない）、アクションの
    種類で対応（額の聞き違いは問わない・最後のフォールドとマックも含める）、最長共通部分列で語の持ち主を残す。"""
    windows = hand_windows((record or {}).get("hands") or [])
    truth = {h["hand_id"]: h for h in truth_hands(gt).get("hands") or []}
    matched: set[float] = set()
    ordered = sorted((r for r in rows if isinstance(r.get("utterance_start_ts"), (int, float))),
                     key=lambda r: r["utterance_start_ts"])
    for hid, (a, b) in windows.items():
        hand = truth.get(hid)
        if not hand:
            continue
        want = [_kind(x["action"]) for x in hand.get("actions") or []
                if isinstance(x, dict) and x.get("action") in BETTING]
        heard: list[tuple[str, float]] = []
        for row in ordered:
            at = float(row["utterance_start_ts"])
            text = (row.get("text") or "").strip()
            if not a <= at < b or _is_noise(row, text):
                continue
            for e in parse_actions(text, confidence=row.get("confidence"), utterance_start_ts=at):
                if e.action in BETTING:
                    heard.append((_kind(e.action), at))
        n, m = len(want), len(heard)
        dp = np.zeros((n + 1, m + 1), dtype=int)
        for i in range(1, n + 1):
            for j in range(1, m + 1):
                dp[i, j] = dp[i - 1, j - 1] + 1 if want[i - 1] == heard[j - 1][0] else max(dp[i - 1, j], dp[i, j - 1])
        i, j = n, m
        while i > 0 and j > 0:
            if want[i - 1] == heard[j - 1][0] and dp[i, j] == dp[i - 1, j - 1] + 1:
                matched.add(heard[j - 1][1])
                i, j = i - 1, j - 1
            elif dp[i - 1, j] >= dp[i, j - 1]:
                i -= 1
            else:
                j -= 1
    return matched


# ───────────────────────── 数え方 ─────────────────────────

def _ranks(values: np.ndarray) -> np.ndarray:
    """同点は順位の平均。"""
    order = np.argsort(values, kind="mergesort")
    ranks = np.empty(len(values))
    sorted_vals = values[order]
    i = 0
    while i < len(values):
        j = i
        while j + 1 < len(values) and sorted_vals[j + 1] == sorted_vals[i]:
            j += 1
        ranks[order[i: j + 1]] = 0.5 * (i + j) + 1.0
        i = j + 1
    return ranks


def pair_wins(pos: np.ndarray, neg: np.ndarray) -> tuple[float, int]:
    """pos の値が neg より大きい組の数（同点 0.5）と組の数。"""
    m, n = len(pos), len(neg)
    if m == 0 or n == 0:
        return 0.0, 0
    ranks = _ranks(np.concatenate([pos, neg]))
    return float(ranks[:m].sum() - m * (m + 1) / 2.0), m * n


def auc(pos: Sequence[float], neg: Sequence[float]) -> float:
    wins, pairs = pair_wins(np.asarray(pos, float), np.asarray(neg, float))
    return wins / pairs if pairs else float("nan")


Groups = dict[str, tuple[np.ndarray, np.ndarray]]


def within_auc(groups: Groups) -> tuple[float, int]:
    """同じセッションの中の組だけで数える AUC（別のセッションどうしの組は数えない）。"""
    wins = pairs = 0.0
    for pos, neg in groups.values():
        w, p = pair_wins(pos, neg)
        wins += w
        pairs += p
    return (wins / pairs if pairs else float("nan")), int(pairs)


def matched_auc(groups: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]],
                ratio: float = MATCH_RATIO) -> tuple[float, int]:
    """話した長さが `ratio` 倍以内の組だけの AUC。groups: セッション → (pos の値, neg の値, pos の長さ, neg の長さ)。"""
    wins = pairs = 0.0
    for pv, nv, pd, nd in groups.values():
        if len(pv) == 0 or len(nv) == 0:
            continue
        rel = np.maximum(pd[:, None], nd[None, :]) / np.maximum(np.minimum(pd[:, None], nd[None, :]), 1e-9)
        ok = rel <= ratio
        diff = pv[:, None] - nv[None, :]
        wins += float(((diff > 0) + 0.5 * (diff == 0))[ok].sum())
        pairs += float(ok.sum())
    return (wins / pairs if pairs else float("nan")), int(pairs)


def bootstrap_within(groups: Groups, n_boot: int = N_BOOT, seed: int = SEED) -> tuple[float, float]:
    """セッションの中で宣言と雑談をそれぞれ引き直す（片方が消えない）。95% 区間。"""
    rng = np.random.default_rng(seed)
    live = [(p, n) for p, n in groups.values() if len(p) and len(n)]
    stats = np.empty(n_boot)
    for b in range(n_boot):
        wins = pairs = 0.0
        for pos, neg in live:
            w, p = pair_wins(pos[rng.integers(0, len(pos), len(pos))], neg[rng.integers(0, len(neg), len(neg))])
            wins += w
            pairs += p
        stats[b] = wins / pairs if pairs else np.nan
    return float(np.nanpercentile(stats, 2.5)), float(np.nanpercentile(stats, 97.5))


def permutation_p(groups: Groups, n_perm: int = N_PERM, seed: int = SEED) -> float:
    """セッションの中でラベルを入れ替える並べ替え検定（片側: 宣言の方が大きい）。"""
    observed, _ = within_auc(groups)
    rng = np.random.default_rng(seed)
    live = [(np.concatenate([p, n]), len(p)) for p, n in groups.values() if len(p) and len(n)]
    hits = 0
    for _ in range(n_perm):
        wins = pairs = 0.0
        for pool, m in live:
            perm = rng.permutation(pool)
            w, p = pair_wins(perm[:m], perm[m:])
            wins += w
            pairs += p
        hits += (wins / pairs) >= observed - 1e-12
    return (hits + 1) / (n_perm + 1)


def leave_one_out_range(groups: Groups) -> tuple[float, float]:
    vals = [within_auc({k: v for k, v in groups.items() if k != drop})[0] for drop in groups
            if len(groups[drop][0]) and len(groups[drop][1])]
    vals = [v for v in vals if not math.isnan(v)]
    return (min(vals), max(vals)) if vals else (float("nan"), float("nan"))


def bootstrap_auc(pos: Sequence[float], neg: Sequence[float], n_boot: int = N_BOOT, seed: int = SEED,
                  ) -> tuple[float, float]:
    """組をまとめて引き直す 95% 区間（片方のラベルが消えた引き直しはやり直す）。"""
    rng = np.random.default_rng(seed)
    values = np.concatenate([np.asarray(pos, float), np.asarray(neg, float)])
    labels = np.concatenate([np.ones(len(pos)), np.zeros(len(neg))])
    stats = []
    while len(stats) < n_boot:
        pick = rng.integers(0, len(values), len(values))
        y = labels[pick]
        if y.min() == y.max():
            continue
        stats.append(auc(values[pick][y == 1], values[pick][y == 0]))
    return float(np.percentile(stats, 2.5)), float(np.percentile(stats, 97.5))


def holm(pvalues: dict[str, float]) -> dict[str, float]:
    order = sorted(pvalues, key=lambda k: pvalues[k])
    out, running = {}, 0.0
    for i, key in enumerate(order):
        running = max(running, min(1.0, (len(order) - i) * pvalues[key]))
        out[key] = running
    return out


def pair_median_difference(groups: Groups) -> float:
    """同じセッションの中の組の差（宣言 − 雑談）の中央値。"""
    diffs = [(p[:, None] - n[None, :]).ravel() for p, n in groups.values() if len(p) and len(n)]
    return float(np.median(np.concatenate(diffs))) if diffs else float("nan")


@dataclass
class FusedResult:
    """雑談と宣言が 1 つのファイルの発話: 宣言の区切りと同じファイルの雑談の区切りの比べ。"""

    auc: float                    # 宣言の区切りの方が高く大きい割合（ファイルごとに平均、ファイルを 1 つずつ数える）
    files: int
    ci: tuple[float, float]
    one_segment: int              # 区切れない・宣言の区切りの高さが取れない
    by_position: dict[str, int]
    f0_diff: float                # 宣言の区切り − 同じファイルの雑談の区切り（半音、ファイルごとの中央値）
    level_diff: float             # 同じく dB
    verdict: Optional[str] = None


def fused_parts(clip: "Clip") -> Optional[tuple[dict, list[dict]]]:
    """(宣言の区切り, 同じファイルの雑談の区切り)。宣言 = 文字の順で最後（`M_end`）・最初（`M_start`）の、高さが
    取れた区切り（札の音など声でない区切りは高さが取れない）。区切れなければ None。"""
    segs = [s for s in clip.feats.get("segments") or [] if s.get("z_index") is not None]
    if len(segs) < 2:
        return None
    if clip.label == "M_end":
        return segs[-1], segs[:-1]
    return segs[0], segs[1:]


def fused_compare(clips: Sequence["Clip"], n_boot: int = N_BOOT, seed: int = SEED) -> FusedResult:
    fracs, f0d, lvd = [], [], []
    one_segment = 0
    by_position = {k: 0 for k in FUSED}
    for c in clips:
        if c.label not in FUSED:
            continue
        parts = fused_parts(c)
        if parts is None:
            one_segment += 1
            continue
        decl, others = parts
        by_position[c.label] += 1
        wins = sum(1.0 if decl["z_index"] > o["z_index"] else 0.5 if decl["z_index"] == o["z_index"] else 0.0
                   for o in others)
        fracs.append(wins / len(others))
        f0d.append(decl["f0"] - float(np.mean([o["f0"] for o in others])))
        lvd.append(decl["level"] - float(np.mean([o["level"] for o in others])))
    if not fracs:
        return FusedResult(float("nan"), 0, (float("nan"), float("nan")), one_segment, by_position,
                           float("nan"), float("nan"))
    arr = np.array(fracs)
    rng = np.random.default_rng(seed)
    boots = [float(arr[rng.integers(0, len(arr), len(arr))].mean()) for _ in range(n_boot)]
    return FusedResult(float(arr.mean()), len(arr), (float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))),
                       one_segment, by_position, float(np.median(f0d)), float(np.median(lvd)))


def fused_verdict(r: FusedResult) -> str:
    if r.files < MIN_FUSED:
        return f"数が足りず判定できない（区切れたファイル {r.files}、基準は {MIN_FUSED} 以上）"
    if r.auc >= H1B_AUC and r.ci[0] >= H1B_LOWER:
        return "同じファイルの中でも宣言の区切りの方が高く大きい（声を切り替えている）"
    return (f"同じファイルの中では区別できない（AUC {r.auc:.2f}・下限 {r.ci[0]:.2f}、基準は {H1B_AUC:.2f} 以上・"
            f"下限 {H1B_LOWER:.2f} 以上）")


def fit_logistic(x: np.ndarray, y: np.ndarray, lam: float = LAMBDA, iters: int = 100) -> np.ndarray:
    """L2 正則化のロジスティック回帰（ニュートン法、切片は正則化しない）。x は標準化済み。[切片, 係数...]"""
    xa = np.hstack([np.ones((len(x), 1)), x])
    w = np.zeros(xa.shape[1])
    penalty = np.eye(xa.shape[1]) * lam
    penalty[0, 0] = 0.0
    for _ in range(iters):
        p = 1.0 / (1.0 + np.exp(-np.clip(xa @ w, -30, 30)))
        grad = xa.T @ (p - y) + penalty @ w
        hess = xa.T @ (xa * (p * (1 - p))[:, None]) + penalty
        step = np.linalg.solve(hess + 1e-9 * np.eye(len(w)), grad)
        w -= step
        if np.max(np.abs(step)) < 1e-10:
            break
    return w


def loso_auc(clips: list["Clip"], columns: Sequence[str]) -> tuple[float, int]:
    """1 セッションずつ外して学習（学習側で標準化）し、外したセッションの中の組で数える AUC（宣言 対 雑談）。"""
    usable = [c for c in clips if c.label in ("D", "C") and all(c.value(k) is not None for k in columns)]
    sessions = sorted({c.session for c in usable})
    groups: Groups = {}
    for held in sessions:
        train = [c for c in usable if c.session != held]
        test = [c for c in usable if c.session == held]
        ytr = np.array([c.label == "D" for c in train], float)
        if not len(test) or not len(ytr) or ytr.min() == ytr.max():
            continue
        xtr = np.array([[c.value(k) for k in columns] for c in train])
        mu, sd = xtr.mean(axis=0), xtr.std(axis=0)
        sd[sd == 0] = 1.0
        w = fit_logistic((xtr - mu) / sd, ytr)
        xte = (np.array([[c.value(k) for k in columns] for c in test]) - mu) / sd
        score = w[0] + xte @ w[1:]
        lab = np.array([c.label == "D" for c in test])
        groups[held] = (score[lab], score[~lab])
    return within_auc(groups)


def llr_from_posterior(p: "float | np.ndarray", prior: float) -> "float | np.ndarray":
    """学習側の割合を除いた尤度比の対数（予測の対数オッズ − 割合の対数オッズ）。"""
    p = np.clip(p, 1e-9, 1 - 1e-9)
    return np.log(p / (1 - p)) - math.log(prior / (1 - prior))


def r_from_llr(llr: "float | np.ndarray", base: float = 0.27, tau: float = 0.5,
               lo: float = 0.05, hi: float = 0.8) -> "float | np.ndarray":
    """決まり文句の下のアクションの見込み: odds(r_i) = odds(base) × 尤度比^tau（lo〜hi に収める）。"""
    z = math.log(base / (1 - base)) + tau * np.asarray(llr)
    return np.clip(1.0 / (1.0 + np.exp(-z)), lo, hi)


# ───────────────────────── データ ─────────────────────────

@dataclass
class Clip:
    session: str
    start: float
    text: str
    label: str
    feats: dict
    wav: Optional[Path] = None
    gt: bool = False
    z: dict = field(default_factory=dict)

    def value(self, key: str) -> Optional[float]:
        if key.startswith("z_"):
            return self.z.get(key[2:])
        if key == "log_duration":
            d = self.feats.get("duration")
            return math.log(d) if d else None
        return self.feats.get(key)


@dataclass
class Session:
    session_id: str
    folder: Path
    rows: list[dict]
    audio_dirs: tuple[Path, ...]
    record: Optional[dict] = None
    gt: Optional[dict] = None
    mic: Optional[str] = None

    def wav_of(self, row: dict) -> Optional[Path]:
        name = row.get("audio_file") or f"{int(float(row['utterance_start_ts']) * 1000)}.wav"
        for folder in self.audio_dirs:
            path = folder / name
            if path.is_file():
                return path
        return None


_MIC_LINE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ .*AudioThread started \(device_id=-?\d+ '([^']*)'")


def mic_starts(log: Path) -> list[tuple[datetime, str]]:
    out = []
    try:
        with log.open(encoding="utf-8", errors="replace") as src:
            for line in src:
                if "AudioThread started" in line:
                    m = _MIC_LINE.match(line)
                    if m:
                        out.append((datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"), m.group(2).strip()))
    except OSError:
        return []
    return out


def load_sessions(paths: Sequence[Path]) -> tuple[list[Session], list[Path]]:
    """ログのフォルダ・zip から、書き起こしと WAV のあるセッション（同じセッションは WAV の多い方）。台本は除く。"""
    tmp_dirs: list[Path] = []
    roots: list[Path] = []
    for p in paths:
        if p.is_file() and p.suffix.lower() == ".zip":
            tmp = Path(tempfile.mkdtemp(prefix="voice_style_"))
            with zipfile.ZipFile(p) as zf:
                zf.extractall(tmp)
            tmp_dirs.append(tmp)
            roots.append(tmp)
        else:
            roots.append(p)
    best: dict[str, tuple[int, Session]] = {}
    for root in roots:
        for tr in sorted(root.rglob("*.transcripts.jsonl")):
            sid = tr.name[: -len(".transcripts.jsonl")]
            if "script_voice" in sid:
                continue
            folder = tr.parent
            dirs = (folder / "audio" / sid, folder / "audio")
            rows = {}
            for row in _read_jsonl(tr):
                if isinstance(row.get("utterance_start_ts"), (int, float)):
                    rows.setdefault(float(row["utterance_start_ts"]), row)
            session = Session(sid, folder, [rows[k] for k in sorted(rows)], dirs,
                              _read_json(folder / f"{sid}.json"), _read_json(folder / f"{sid}.ground_truth.json"))
            n_wav = sum(1 for r in session.rows if session.wav_of(r) is not None)
            if n_wav and (sid not in best or n_wav > best[sid][0]):
                best[sid] = (n_wav, session)
    sessions = [s for _, s in best.values()]
    for s in sessions:
        starts: list[tuple[datetime, str]] = []
        for log in (s.folder / "pokerapp.log", s.folder.parent / "pokerapp.log"):
            if log.is_file():
                starts = mic_starts(log)
                break
        if s.rows and starts:
            first = datetime.fromtimestamp(float(s.rows[0]["utterance_start_ts"]), JST).replace(tzinfo=None)
            before = [name for when, name in starts if when <= first]
            s.mic = before[-1] if before else None
    return sorted(sessions, key=lambda s: float(s.rows[0]["utterance_start_ts"]) if s.rows else 0.0), tmp_dirs


def build_clips(sessions: Sequence[Session], vad: Optional[VadFn],
                rules: int = 2) -> tuple[list[Clip], dict[str, dict[str, int]]]:
    """発話ごとの分け方と声の量。返り値: (数える発話, セッション → 除いた理由の数)。"""
    clips: list[Clip] = []
    skipped: dict[str, dict[str, int]] = {}
    for s in sessions:
        labels = label_session(s.rows, rules)
        matched = gt_matched(s.rows, s.record, s.gt) if s.gt else set()
        counts: dict[str, int] = {}
        for row in s.rows:
            at = float(row["utterance_start_ts"])
            label = labels.get(at, "noise")
            wav = s.wav_of(row)
            if (label not in CLASS_NAMES and label not in FUSED) or wav is None:
                why = "no_wav" if label in CLASS_NAMES or label in FUSED else label
                counts[why] = counts.get(why, 0) + 1
                continue
            try:
                feats = clip_features(read_wav(wav), vad)
            except (OSError, ValueError, wave.Error) as exc:
                logger.warning("WAV を読めません %s: %s", wav, exc)
                counts["no_wav"] = counts.get("no_wav", 0) + 1
                continue
            if feats.get("skip"):
                counts[feats["skip"]] = counts.get(feats["skip"], 0) + 1
                continue
            clips.append(Clip(s.session_id, at, (row.get("text") or "").strip(), label, feats, wav, at in matched))
        skipped[s.session_id] = counts
    add_session_z(clips)
    return clips, skipped


def add_session_z(clips: Sequence[Clip], keys: Sequence[str] = ("f0", "level")) -> None:
    """セッションの中央値と散らばり（1.4826 × MAD）での z。ラベルは使わない（そのセッションの数える発話すべて）。"""
    by_session: dict[str, list[Clip]] = {}
    for c in clips:
        by_session.setdefault(c.session, []).append(c)
    for group in by_session.values():
        stats: dict[str, tuple[float, float]] = {}
        for key in keys:
            vals = np.array([c.feats[key] for c in group if c.feats.get(key) is not None], float)
            if not len(vals):
                continue
            med = float(np.median(vals))
            scale = 1.4826 * float(np.median(np.abs(vals - med)))
            if scale <= 0:
                scale = float(vals.std()) or 1.0
            stats[key] = (med, scale)
            for c in group:
                if c.feats.get(key) is not None:
                    c.z[key] = (c.feats[key] - med) / scale
        for c in group:
            if "f0" in c.z and "level" in c.z:
                c.z["index"] = c.z["f0"] + c.z["level"]
        # 区切り（雑談と宣言が 1 つのファイル）も同じセッションの物差しで
        for c in group:
            for seg in c.feats.get("segments") or []:
                if seg.get("f0") is not None and "f0" in stats and "level" in stats:
                    seg["z_index"] = ((seg["f0"] - stats["f0"][0]) / stats["f0"][1]
                                      + (seg["level"] - stats["level"][0]) / stats["level"][1])


# ───────────────────────── まとめ ─────────────────────────

def _groups(clips: Sequence[Clip], key: str, pos: str = "D", neg: str = "C",
            only_gt: bool = False) -> Groups:
    out: dict[str, tuple[list, list]] = {}
    for c in clips:
        v = c.value(key)
        if v is None or (only_gt and c.label == pos and not c.gt):
            continue
        if c.label == pos:
            out.setdefault(c.session, ([], []))[0].append(v)
        elif c.label == neg:
            out.setdefault(c.session, ([], []))[1].append(v)
    return {k: (np.array(p, float), np.array(n, float)) for k, (p, n) in out.items()}


def _matched_groups(clips: Sequence[Clip], key: str) -> dict:
    out: dict[str, tuple[list, list, list, list]] = {}
    for c in clips:
        v, d = c.value(key), c.feats.get("duration")
        if v is None or not d or c.label not in ("D", "C"):
            continue
        g = out.setdefault(c.session, ([], [], [], []))
        if c.label == "D":
            g[0].append(v)
            g[2].append(d)
        else:
            g[1].append(v)
            g[3].append(d)
    return {k: tuple(np.array(x, float) for x in v) for k, v in out.items()}


@dataclass
class GroupResult:
    name: str
    sessions: int
    counts: dict[str, int]
    index_auc: float
    pairs: int
    ci: tuple[float, float]
    p: float
    loo: tuple[float, float]
    matched_auc: float
    matched_pairs: int
    words_auc: float
    words_pairs: int
    a_count: int
    f0_diff: float
    level_diff: float
    features: dict[str, tuple[float, float]]      # 特徴 → (AUC, Holm の p)
    logistic: tuple[float, float]                  # (長さだけ, 長さ + 高さ + 大きさ)
    gt_auc: tuple[float, int]
    fused: Optional[FusedResult] = None
    verdict: Optional[str] = None


def summarize(name: str, clips: Sequence[Clip], decide: bool, n_boot: int = N_BOOT,
              n_perm: int = N_PERM) -> GroupResult:
    groups = _groups(clips, "z_index")
    index_auc, pairs = within_auc(groups)
    ci = bootstrap_within(groups, n_boot) if pairs else (float("nan"), float("nan"))
    p = permutation_p(groups, n_perm) if pairs else float("nan")
    m_auc, m_pairs = matched_auc(_matched_groups(clips, "z_index"))
    w_auc, w_pairs = within_auc(_groups(clips, "z_index", pos="A"))
    pvals, aucs = {}, {}
    for key in FEATURE_NAMES:
        g = _groups(clips, key)
        a, n = within_auc(g)
        if n:
            aucs[key] = a
            p_hi = permutation_p(g, n_perm)
            p_lo = permutation_p({k: (-x, -y) for k, (x, y) in g.items()}, n_perm)
            pvals[key] = min(1.0, 2 * min(p_hi, p_lo))
    adj = holm(pvals)
    result = GroupResult(
        name=name, sessions=len({c.session for c in clips}),
        counts={k: sum(1 for c in clips if c.label == k and "index" in c.z) for k in (*CLASS_NAMES, *FUSED)},
        index_auc=index_auc, pairs=pairs, ci=ci, p=p, loo=leave_one_out_range(groups),
        matched_auc=m_auc, matched_pairs=m_pairs, words_auc=w_auc, words_pairs=w_pairs,
        a_count=sum(1 for c in clips if c.label == "A" and "index" in c.z),
        f0_diff=pair_median_difference(_groups(clips, "f0")),
        level_diff=pair_median_difference(_groups(clips, "level")),
        features={k: (aucs[k], adj[k]) for k in aucs},
        logistic=(loso_auc(list(clips), ["log_duration"])[0],
                  loso_auc(list(clips), ["log_duration", "z_f0", "z_level"])[0]),
        gt_auc=within_auc(_groups(clips, "z_index", only_gt=True)),
        fused=fused_compare(clips, n_boot),
    )
    if decide:
        result.verdict = verdict(result)
        result.fused.verdict = fused_verdict(result.fused)
    return result


def verdict(r: GroupResult) -> str:
    """H1 の判定（計画で固定した基準）。"""
    if math.isnan(r.index_auc):
        return "判定できない（宣言と雑談の組が無い）"
    main = r.index_auc >= H1_AUC and r.ci[0] >= H1_LOWER
    if not main:
        return f"区別できない（AUC {r.index_auc:.2f}・下限 {r.ci[0]:.2f}、基準は {H1_AUC:.2f} 以上・下限 {H1_LOWER:.2f} 以上）"
    notes, failed = [], []
    if r.matched_pairs < MIN_MATCHED_PAIRS:
        notes.append(f"長さをそろえた組が {r.matched_pairs}（{MIN_MATCHED_PAIRS} 未満）")
    elif r.matched_auc < H1_MATCHED:
        failed.append(f"長さをそろえると {r.matched_auc:.2f}（基準 {H1_MATCHED:.2f}）")
    if r.a_count < MIN_A:
        notes.append(f"進行の言葉が {r.a_count}（{MIN_A} 未満）")
    elif r.words_auc < H1_WORDS:
        failed.append(f"言葉が違うと {r.words_auc:.2f}（基準 {H1_WORDS:.2f}）")
    if failed:
        return "大きさと高さは違うが、声の出し方とは言えない（" + "・".join(failed) + "）"
    if notes:
        return "大きさと高さは違う（長さ・言葉を外した確かめは数が足りず未: " + "・".join(notes) + "）"
    return "声の出し方で区別できる（主の AUC・下限・長さをそろえた組・言葉の違う組がすべて基準を超えた）"


def _fmt(v: float, digits: int = 2) -> str:
    return "—" if v is None or (isinstance(v, float) and math.isnan(v)) else f"{v:.{digits}f}"


def _fmt_p(p: float) -> str:
    if p is None or math.isnan(p):
        return "—"
    return "< 0.001" if p < 0.001 else f"{p:.3f}"


def format_group(r: GroupResult, skipped: dict[str, int]) -> list[str]:
    c = r.counts
    skip = "・".join(f"{_SKIP_NAMES.get(k, k)} {v}" for k, v in sorted(skipped.items(), key=lambda kv: -kv[1]))
    lines = [
        f"{r.name}（{r.sessions} セッション）",
        f"  発話: 宣言 {c['D']}・進行の言葉 {c['A']}・雑談 {c['C']}（除いた: {skip or 'なし'}）",
        f"  主の指数 z(高さ) + z(大きさ): 宣言 対 雑談 AUC {_fmt(r.index_auc)}（95% {_fmt(r.ci[0])}〜{_fmt(r.ci[1])}・"
        f"並べ替え p {_fmt_p(r.p)}・1 セッション外すと {_fmt(r.loo[0])}〜{_fmt(r.loo[1])}・組 {r.pairs}）",
        f"  長さをそろえた組（{MATCH_RATIO} 倍以内）: {_fmt(r.matched_auc)}（組 {r.matched_pairs}）／ "
        f"進行の言葉 対 雑談: {_fmt(r.words_auc)}（組 {r.words_pairs}）",
        f"  差（同じセッションの組の差の中央値）: 高さ {r.f0_diff:+.1f} 半音・大きさ {r.level_diff:+.1f} dB",
    ]
    feats = " / ".join(f"{FEATURE_NAMES[k]} {_fmt(a)}（p {_fmt_p(p)}）" for k, (a, p) in r.features.items())
    lines.append(f"  特徴ごと（宣言 対 雑談・参考・Holm で補正した p）: {feats}")
    lines.append(f"  長さに上乗せ（ロジスティック・1 セッションずつ外す）: 長さだけ {_fmt(r.logistic[0])} → "
                 f"長さ + 高さ + 大きさ {_fmt(r.logistic[1])}")
    lines.append(f"  真のアクションと対応した宣言だけ（確かめ）: {_fmt(r.gt_auc[0])}（組 {r.gt_auc[1]}）")
    if r.verdict:
        lines.append(f"  → 判定: {r.verdict}")
    f = r.fused
    if f is not None:
        lines.append(f"  雑談と宣言が 1 つのファイル（文字の順で宣言が最後 {f.by_position['M_end']}・最初 "
                     f"{f.by_position['M_start']}・区切れない {f.one_segment}）: 宣言の区切りの方が高く大きい割合 "
                     f"{_fmt(f.auc)}（95% {_fmt(f.ci[0])}〜{_fmt(f.ci[1])}・{f.files} ファイル）・差 高さ "
                     f"{_fmt(f.f0_diff, 1)} 半音・大きさ {_fmt(f.level_diff, 1)} dB")
        if f.verdict:
            lines.append(f"  → 判定（同じファイルの中）: {f.verdict}")
    return lines


def run(paths: Sequence[Path], one_person: Sequence[str], vad: Optional[VadFn] = None,
        n_boot: int = N_BOOT, n_perm: int = N_PERM, rules: int = 2) -> tuple[list[str], list[Clip], dict, list[Session]]:
    sessions, tmp_dirs = load_sessions(paths)
    try:
        clips, skipped = build_clips(sessions, vad, rules)
    finally:
        for tmp in tmp_dirs:
            shutil.rmtree(tmp, ignore_errors=True)

    def is_one(sid: str) -> bool:
        return any(sid.startswith(prefix) for prefix in one_person)

    lines = []
    groups = [("1 人 n 役（主・判定に使う）", True, lambda sid: is_one(sid)),
              ("本物のプレイヤーがいた・分からない（参考・判定に使わない）", False, lambda sid: not is_one(sid))]
    results = {}
    for name, decide, pick in groups:
        part = [c for c in clips if pick(c.session)]
        if not part:
            continue
        skip: dict[str, int] = {}
        for sid, counts in skipped.items():
            if pick(sid):
                for k, v in counts.items():
                    skip[k] = skip.get(k, 0) + v
        r = summarize(name, part, decide, n_boot, n_perm)
        results[name] = r
        lines += format_group(r, skip) + [""]
    lines.append("マイクの経路: " + " / ".join(f"{s.session_id[:8]} {s.mic or '?'}" for s in sessions))
    lines.append(f"分け方: {rules} 版" + ("（事前に決めた分け方）" if rules == 1 else
                                         "（卓の言葉だけの発話も進行の言葉・雑談はひらがな・漢字の文だけ）"))
    return lines, clips, results, sessions


# ───────────────────────── 静的なページ ─────────────────────────

def _wav_data_uri(path: Path) -> str:
    return "data:audio/wav;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def _centered(clips: Sequence[Clip], key: str) -> dict[str, list[float]]:
    """セッションの中央値を引いた値（クラスごと）。セッションをまたいで 1 つの図に並べるため。"""
    med: dict[str, float] = {}
    for sid in {c.session for c in clips}:
        vals = [c.feats[key] for c in clips if c.session == sid and c.feats.get(key) is not None]
        if vals:
            med[sid] = float(np.median(vals))
    out: dict[str, list[float]] = {k: [] for k in CLASS_NAMES}
    for c in clips:
        if c.label in out and c.feats.get(key) is not None and c.session in med:
            out[c.label].append(c.feats[key] - med[c.session])
    return out


def _svg_strip(values: dict[str, list[float]], unit: str, lo: float, hi: float) -> str:
    width, row_h, left, right = 640, 56, 96, 16
    height = row_h * len(values) + 36

    def xpos(v: float) -> float:
        return left + (min(max(v, lo), hi) - lo) / (hi - lo) * (width - left - right)

    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" class="chart">']
    step = (hi - lo) / 6
    for i in range(7):
        v = lo + i * step
        x = xpos(v)
        parts.append(f'<line x1="{x:.1f}" y1="8" x2="{x:.1f}" y2="{height - 24}" class="grid"/>')
        label = "0" if abs(v) < 1e-9 else f"{v:+.0f}" if lo < 0 else f"{v:.1f}"
        parts.append(f'<text x="{x:.1f}" y="{height - 8}" class="tick" text-anchor="middle">{label}</text>')
    rng = np.random.default_rng(SEED)
    for row, (label, vals) in enumerate(values.items()):
        y0 = 12 + row * row_h
        parts.append(f'<text x="8" y="{y0 + row_h / 2:.1f}" class="lab">{CLASS_NAMES[label]}（{len(vals)}）</text>')
        if not vals:
            continue
        arr = np.array(vals)
        for v, j in zip(arr[:400], rng.uniform(0.2, 0.8, min(len(arr), 400))):
            parts.append(f'<circle cx="{xpos(v):.1f}" cy="{y0 + j * (row_h - 12):.1f}" r="2.2" class="dot {label}"/>')
        q1, med, q3 = np.percentile(arr, [25, 50, 75])
        parts.append(f'<rect x="{xpos(q1):.1f}" y="{y0 + 4}" width="{max(1.0, xpos(q3) - xpos(q1)):.1f}" '
                     f'height="{row_h - 20}" class="box"/>')
        parts.append(f'<line x1="{xpos(med):.1f}" y1="{y0 + 2}" x2="{xpos(med):.1f}" y2="{y0 + row_h - 14}" class="med"/>')
    parts.append("</svg>")
    return "".join(parts)


def render_html(lines: Sequence[str], clips: Sequence[Clip], one_person: Sequence[str], examples: int = 5) -> str:
    main = [c for c in clips if any(c.session.startswith(p) for p in one_person)]
    charts = []
    for key, unit, lo, hi in (("f0", "半音、セッションの中央値から", -8, 8),
                              ("level", "dB、セッションの中央値から", -18, 18),
                              ("duration", "話した秒数", 0, 3)):
        vals = _centered(main, key) if key != "duration" else {
            k: [c.feats["duration"] for c in main if c.label == k] for k in CLASS_NAMES}
        charts.append(f"<h3>{FEATURE_NAMES[key]}（{html.escape(unit)}）</h3>" + _svg_strip(vals, unit, lo, hi))
    scored = [c for c in main if "index" in c.z]
    odd_c = sorted((c for c in scored if c.label == "C"), key=lambda c: -c.z["index"])[:examples]
    odd_d = sorted((c for c in scored if c.label == "D"), key=lambda c: c.z["index"])[:examples]

    def card(c: Clip) -> str:
        when = datetime.fromtimestamp(c.start, JST).strftime("%m/%d %H:%M:%S")
        audio = f'<audio controls preload="none" src="{_wav_data_uri(c.wav)}"></audio>' if c.wav else ""
        return (f'<li><div class="said">「{html.escape(c.text)}」</div><div class="meta">{c.session[:8]} {when}・'
                f'指数 {c.z["index"]:+.1f}・高さ {c.z.get("f0", 0):+.1f}・大きさ {c.z.get("level", 0):+.1f}</div>{audio}</li>')

    def fused_card(c: Clip) -> str:
        parts = fused_parts(c)
        when = datetime.fromtimestamp(c.start, JST).strftime("%m/%d %H:%M:%S")
        segs = " ／ ".join(
            f"{s['t0']:.1f}〜{s['t1']:.1f} 秒 {s['z_index']:+.1f}" + ("（宣言）" if parts and s is parts[0] else "")
            for s in c.feats.get("segments") or [] if s.get("z_index") is not None)
        audio = f'<audio controls preload="none" src="{_wav_data_uri(c.wav)}"></audio>' if c.wav else ""
        return (f'<li><div class="said">「{html.escape(c.text)}」</div><div class="meta">{c.session[:8]} {when}・'
                f'区切りの指数: {html.escape(segs) or "区切れない"}</div>{audio}</li>')

    fused = sorted((c for c in clips if c.label in FUSED), key=lambda c: (fused_parts(c) is None, c.start))[:12]
    body = "\n".join(html.escape(line) for line in lines)
    return f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>宣言の声と雑談の声</title>
<style>
:root {{ --bg:#f6f7f9; --fg:#1d2430; --muted:#5d6878; --line:#d5dae2; --card:#ffffff;
  --D:#c2410c; --A:#7c3aed; --C:#0f766e; color-scheme: light; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#14181f; --fg:#e6e9ee; --muted:#9aa4b2; --line:#2c3440;
  --card:#1b2029; --D:#fb923c; --A:#a78bfa; --C:#2dd4bf; color-scheme: dark; }} }}
body {{ margin:0; background:var(--bg); color:var(--fg); font:15px/1.6 system-ui, "Hiragino Sans", "Noto Sans JP", sans-serif; }}
main {{ max-width:760px; margin:0 auto; padding:24px 16px 48px; }}
h1 {{ font-size:1.4rem; margin:0 0 4px; }} h2 {{ font-size:1.1rem; margin:28px 0 8px; }} h3 {{ font-size:.95rem; margin:16px 0 4px; }}
.lead {{ color:var(--muted); margin:0 0 16px; }}
pre {{ background:var(--card); border:1px solid var(--line); border-radius:8px; padding:12px; overflow-x:auto;
  white-space:pre-wrap; font:13px/1.55 ui-monospace, Menlo, monospace; }}
.chart {{ width:100%; height:auto; background:var(--card); border:1px solid var(--line); border-radius:8px; }}
.grid {{ stroke:var(--line); }} .tick, .lab {{ fill:var(--muted); font-size:12px; }}
.dot {{ opacity:.55; }} .dot.D {{ fill:var(--D); }} .dot.A {{ fill:var(--A); }} .dot.C {{ fill:var(--C); }}
.box {{ fill:none; stroke:var(--fg); stroke-width:1.2; }} .med {{ stroke:var(--fg); stroke-width:2.5; }}
ul {{ list-style:none; padding:0; display:grid; gap:10px; }}
li {{ background:var(--card); border:1px solid var(--line); border-radius:8px; padding:10px 12px; }}
.said {{ font-weight:600; }} .meta {{ color:var(--muted); font-size:13px; margin:2px 0 6px; font-variant-numeric:tabular-nums; }}
audio {{ width:100%; }}
</style></head><body><main>
<h1>宣言の声と雑談の声</h1>
<p class="lead">店舗の録音から、配る人の宣言（賭けの言葉だけの発話）と雑談の声の高さ・大きさを同じセッションの中で比べた結果。
この画面には店舗の音声が入っているので、公開しないでください。</p>
<h2>結果</h2><pre>{body}</pre>
<h2>1 人 n 役のセッションの分布</h2>{"".join(charts)}
<h2>いちばん宣言らしい雑談</h2><ul>{"".join(card(c) for c in odd_c)}</ul>
<h2>いちばん雑談らしい宣言</h2><ul>{"".join(card(c) for c in odd_d)}</ul>
<h2>雑談と宣言が 1 つのファイル</h2>
<p class="lead">声の切れ目で区切った区切りごとの指数（高さ + 大きさ、セッションの中央から）。「（宣言）」が文字の順で宣言の区切り。</p>
<ul>{"".join(fused_card(c) for c in fused)}</ul>
</main></body></html>
"""


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="宣言の声と雑談の声を数値で見分けられるか（店舗のログの WAV）")
    ap.add_argument("logs", nargs="+", type=Path, help="ログのフォルダ・pack_logs の zip（複数可）")
    ap.add_argument("--one-person", default="", help="1 人 n 役のセッション（セッション ID の頭、カンマ区切り）")
    ap.add_argument("--html", type=Path, help="分布の図と例の音のページ（JS なし。店舗の音声を含む）")
    ap.add_argument("--json", type=Path, help="発話ごとの分け方と声の量")
    ap.add_argument("--no-vad", action="store_true", help="Silero VAD を使わない（エネルギーだけ）")
    ap.add_argument("--labels", type=int, choices=LABEL_RULES, default=2,
                    help="発話の分け方の版（1 = 2026-10-03 に事前に決めた分け方、2 = その後に直した分け方）")
    ap.add_argument("--boot", type=int, default=N_BOOT)
    ap.add_argument("--perm", type=int, default=N_PERM)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    one = [p.strip() for p in args.one_person.split(",") if p.strip()]
    vad = None if args.no_vad else silero_vad()
    if vad is None and not args.no_vad:
        print("faster_whisper が無いので声の区間はエネルギーだけで決めます")
    lines, clips, _results, _sessions = run(args.logs, one, vad, args.boot, args.perm, args.labels)
    print("\n".join(lines))
    if args.json:
        args.json.write_text(json.dumps([{"session": c.session, "start": c.start, "text": c.text, "label": c.label,
                                          "gt": c.gt, "feats": c.feats, "z": c.z} for c in clips],
                                        ensure_ascii=False, indent=1), encoding="utf-8")
    if args.html:
        args.html.write_text(render_html(lines, clips, one), encoding="utf-8")
        print(f"ページ: {args.html}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
