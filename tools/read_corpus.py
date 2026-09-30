"""tools/read_corpus.py — 読み上げ集（正解が先に分かっている発話）を録る（テスト方針 週 1, 2026-09-29）

店舗の真のアクションは記録を見ながら入れるので、聞き取りの誤りと入れる側の誤りが混ざり、数も少ない
（`docs/worklog/2026-09-29-test-strategy.md`）。読み上げ集は、画面に出した句（「レイズ 1300」「フォールド、
フォールド、コール」「ポット 3000」…）をディーラーが読み、句ごとの音声を**正解**（読み取りが返すべきアクション）と
一緒に残す。聞き取りの方式の比較・推定器の確からしさ・マイクの選択に使う。

- 真のアクション入力の画面（`tools/ground_truth_ui.py`, ポート 8791）の「読み上げ集」（`/corpus`）から使う。
  スマホ / iPad に句を 1 つずつ大きく出し、読んだら「次へ」。PC のマイクで録り続け、句を出した時刻から「次へ」を
  押した時刻までを切り出す（前後に少し余白）。
- マイクは 2 本まで同時に録れる（同じ発話でマイクを比べる, `m1` / `m2`）。
- 録りながら、本番と同じ聞き取り（発話の切り出し → Whisper → 読み取り → 第 2 の耳）を裏で回し、
  `transcripts.jsonl` に書く（読み終わってから数分で追いつく）。
- 出力: `logs/corpus/<日時>_<名前>/` に `meta.json`・`labels.jsonl`（句ごとの正解・時刻・ファイル）・
  `<句>_t<回>_m1.wav`・`transcripts.jsonl`・`full_m1.wav`（ずっと録った音声。送付には入れない）。
  「ログをまとめる」が直近の読み上げ集も zip に入れる。

使い方（開発側）:

    python tools/read_corpus.py phrases                         # 句の一覧と正解
    python tools/read_corpus.py eval pokerlogs_20261001_1of3.zip pokerlogs_20261001_2of3.zip
    python tools/read_corpus.py transcribe logs/corpus/<フォルダ>   # 聞き取りを書く（途中からでよい）
    python tools/read_corpus.py serve                           # この画面だけ（http://127.0.0.1:8792/corpus）
"""
from __future__ import annotations

import argparse
import json
import logging
import queue
import random
import re
import shutil
import sys
import threading
import time
import wave
from bisect import bisect_left
from collections import Counter, deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from audio.recognizer import is_implausibly_long, is_prompt_echo, is_question, parse_actions  # noqa: E402
from audio.recorder import _calc_rms  # noqa: E402

logger = logging.getLogger(__name__)

# 句の組の版（句を変えたら上げる。meta.json に残す）
PHRASE_SET = "2026-09-30"
CORPUS_DIR = "corpus"
META = "meta.json"
LABELS = "labels.jsonl"
TRANSCRIPTS = "transcripts.jsonl"
# 句を出した時刻の少し前から、「次へ」を押した少しあとまでを切り出す（押すのが読み終わりより早くても語尾を残す。
# 読み取った時刻は録った時刻より少し遅れるので、実際の余白はこれより 0.1 秒ほど前にずれる）
PRE_ROLL = 0.3
POST_ROLL = 0.6
# 切り出しのために残しておく音声（秒）。1 つの句にこれより長くかかることはない
RING_SEC = 120.0
CHUNK = 1024
MAX_MICS = 2
# 画面のレベル表示: 直近この秒数の最大
LEVEL_WINDOW = 0.3
_FOLDER_RE = re.compile(r"^[0-9]{8}_[0-9]{6}_[A-Za-z0-9_\-]{1,40}$")
_WAV_RE = re.compile(r"^[a-z]{2,4}[0-9]{2}_t[0-9]{1,3}_m[12]\.wav$")

# ───────────────────────── 句 ─────────────────────────

KIND_LABELS = {
    "action": "アクション",
    "bet": "ベット",
    "raise": "レイズ",
    "amount": "額だけ",
    "wording": "ほかの言い方",
    "sequence": "続けて（ふだんの間で）",
    "position": "ポジション",
    "seat": "席",
    "control": "進行",
    "hand_name": "役の名前",
    "restate": "言い直し",
    "none": "アクションではない",
}


@dataclass(frozen=True)
class Phrase:
    id: str
    text: str                    # 画面に出す言葉（読む言葉）
    kind: str
    expect: tuple[str, ...]      # 読み取りが返すべきアクション（`event_key` の形）。空 = アクションではない
    note: str = ""               # 読み方の注意（小さく出す）
    known_gap: bool = False      # 書いたとおりの文でも、いまの読み取りでは正解にならない（言い直し）

    def label(self) -> dict:
        return {"id": self.id, "text": self.text, "kind": self.kind, "expect": list(self.expect)}

    def view(self) -> dict:
        return {"id": self.id, "text": self.text, "kind": self.kind, "hint": KIND_LABELS.get(self.kind, ""),
                "note": self.note}


def amount_text(n: int) -> str:
    """画面に出す額（1 万以上は「1万2千」の形 = 読み上げに近い）。"""
    if n < 10000:
        return str(n)
    man, rest = divmod(n, 10000)
    if rest == 0:
        return f"{man}万"
    if rest % 1000 == 0:
        return f"{man}万{rest // 1000}千"
    return f"{man}万{rest}"


def build_phrases() -> list[Phrase]:
    """読み上げ集の句（約 160）。額は店舗の実際の額（100 / 200 のブラインド）と、聞き違えた額（6千・8千・2千・
    1200 など, 店舗の書き起こし）を多めに。"""
    out: list[Phrase] = []
    counts: Counter = Counter()

    def add(prefix: str, kind: str, text: str, expect: list[str], note: str = "", gap: bool = False) -> None:
        counts[prefix] += 1
        out.append(Phrase(f"{prefix}{counts[prefix]:02d}", text, kind, tuple(expect), note, gap))

    for word, key, times in (("チェック", "check", 5), ("コール", "call", 5), ("フォールド", "fold", 5),
                             ("オールイン", "allin", 4)):
        for _ in range(times):
            add("act", "action", word, [key])
    for n in (300, 500, 600, 800, 900, 1000, 1200, 1500, 2000, 2500, 3000, 4000, 5000, 6000, 8000, 10000, 12000):
        add("bet", "bet", f"ベット {amount_text(n)}", [f"bet {n}"])
    for n in (400, 500, 600, 700, 800, 1100, 1200, 1300, 1600, 1800, 2200, 2400, 3500, 4500, 6000, 8000, 9000,
              15000, 20000, 30000):
        add("rai", "raise", f"レイズ {amount_text(n)}", [f"raise {n}"])
    for text, n in (("600", 600), ("900点", 900), ("1100", 1100), ("1300", 1300), ("2000", 2000), ("2500", 2500),
                    ("3千点", 3000), ("4000", 4000), ("6000", 6000), ("8000", 8000), ("1万", 10000),
                    ("1万2500", 12500)):
        add("amt", "amount", text, [f"amount {n}"])
    for text, key in (("スリーベット 1800", "raise 1800"), ("リレイズ 4000", "raise 4000"),
                      ("チェックレイズ 2000", "raise 2000"), ("レイズ トータル 2400", "raise 2400"),
                      ("ベット 1000点", "bet 1000"), ("レイズ 2千点", "raise 2000")):
        add("wrd", "wording", text, [key])
    for text, keys in (
        ("フォールド、フォールド、コール", ["fold", "fold", "call"]),
        ("チェック、チェック", ["check", "check"]),
        ("コール、コール", ["call", "call"]),
        ("フォールド、レイズ 1200", ["fold", "raise 1200"]),
        ("チェック、ベット 800", ["check", "bet 800"]),
        ("コール、フォールド、フォールド", ["call", "fold", "fold"]),
        ("レイズ 2000、フォールド、コール", ["raise 2000", "fold", "call"]),
        ("フォールド、フォールド、フォールド", ["fold", "fold", "fold"]),
        ("チェック、チェック、チェック", ["check", "check", "check"]),
        ("ベット 1500、コール、フォールド", ["bet 1500", "call", "fold"]),
        ("フォールド、コール、コール", ["fold", "call", "call"]),
        ("オールイン、コール", ["allin", "call"]),
        ("コール、オールイン", ["call", "allin"]),
        ("レイズ 600、コール、コール、フォールド", ["raise 600", "call", "call", "fold"]),
        ("チェック、ベット 2000、コール", ["check", "bet 2000", "call"]),
        ("フォールド、フォールド、レイズ 900", ["fold", "fold", "raise 900"]),
        ("コール、レイズ 3000", ["call", "raise 3000"]),
        ("ベット 500、レイズ 1500、フォールド", ["bet 500", "raise 1500", "fold"]),
        ("フォールド、オールイン、フォールド", ["fold", "allin", "fold"]),
        ("チェック、チェック、ベット 1200、フォールド", ["check", "check", "bet 1200", "fold"]),
    ):
        add("seq", "sequence", text, keys, note="プレイヤーが順に動くときの間で")
    for text, key in (
        ("ボタン コール", "call @BTN"), ("ボタン レイズ 1500", "raise 1500 @BTN"), ("スモール フォールド", "fold @SB"),
        ("スモールブラインド コール", "call @SB"), ("ビッグ チェック", "check @BB"),
        ("ビッグブラインド レイズ 1200", "raise 1200 @BB"), ("UTG レイズ 600", "raise 600 @UTG"),
        ("UTG フォールド", "fold @UTG"), ("カットオフ コール", "call @CO"), ("カットオフ レイズ 1100", "raise 1100 @CO"),
        ("ハイジャック フォールド", "fold @HJ"), ("ハイジャック コール", "call @HJ"), ("ボタン オールイン", "allin @BTN"),
        ("ビッグ コール", "call @BB"),
    ):
        add("pos", "position", text, [key], note="UTG は「ユーティージー」" if text.startswith("UTG") else "")
    for text, key in (
        ("シート3 コール", "call @3"), ("シート5 フォールド", "fold @5"), ("シート7 レイズ 1500", "raise 1500 @7"),
        ("シート2 オールイン", "allin @2"), ("シート1 チェック", "check @1"), ("シート6 ベット 800", "bet 800 @6"),
        ("シート4 コール", "call @4"), ("シート9 フォールド", "fold @9"),
    ):
        add("seat", "seat", text, [key])
    for text, key, times in (
        ("ハンド開始", "new_hand", 3), ("ショーダウン", "showdown", 3), ("ハンド終了", "end_hand", 2),
        ("チョップ", "winner", 2), ("ヘッズアップ", "heads_up", 2), ("チェックアラウンド", "check_around", 2),
    ):
        for _ in range(times):
            add("ctl", "control", text, [key])
    for seat in (3, 5, 1):
        add("ctl", "control", f"シート{seat} ウィナー", [f"winner @{seat}"])
    for text, name in (("フルハウス", "Full house"), ("フラッシュ", "Flush"), ("ストレート", "Straight"),
                       ("ツーペア", "Two pair"), ("ワンペア", "One pair"), ("スリーカード", "Three of a kind"),
                       ("ハイカード", "High card")):
        add("hn", "hand_name", text, [f"end_hand:{name}"], note="ショーダウンで勝った役")
    add("rst", "restate", "レイズ 1500、トータル 1500", ["raise 1500"])
    add("rst", "restate", "ベット、2000", ["bet 2000"], note="「ベット」と「2000」の間を 1 秒あける")
    add("rst", "restate", "コール、はい、コール", ["call"], gap=True)
    add("rst", "restate", "オールイン、オールインです", ["allin"], gap=True)
    add("rst", "restate", "コール 3ウェイ", ["call"], note="3 人でポットに入った")
    for text in ("ポット 3000", "ポット 1万2千です", "100点 お釣りです", "ターンです", "リバー", "ラストカード",
                 "フロップです", "3プレイヤー", "ナイスハンド", "お願いします", "少々お待ちください", "次のハンドです",
                 "スタック 8000 です", "ブラインド 200 400 です", "アクションどうぞ",
                 "コールですか？", "レイズですか？", "オールインですか？"):
        add("neg", "none", text, [], note="確かめる言い方で" if text.endswith("？") else "")
    return out


PHRASES = build_phrases()
PHRASES_BY_ID = {p.id: p for p in PHRASES}


def reading_order(phrases: list[Phrase], round_no: int) -> list[Phrase]:
    """読む順（周ごとに決まった並べ替え。同じ言葉が続かないようにする = 続けて読むと 2 回目は読み方が変わる）。"""
    order = list(phrases)
    random.Random(f"{PHRASE_SET}:{round_no}").shuffle(order)
    for _ in range(4):
        moved = False
        for i in range(1, len(order)):
            if order[i].text != order[i - 1].text:
                continue
            for j in range(i + 1, len(order)):
                if order[j].text != order[i - 1].text and (i + 1 >= len(order) or order[j].text != order[i + 1].text):
                    order[i], order[j] = order[j], order[i]
                    moved = True
                    break
        if not moved:
            break
    return order


def event_key(event: Any) -> str:
    """読み取ったアクション 1 つを比べる形にする（例 "raise 1300" / "call @BTN" / "amount 600" / "winner @3"）。
    コール・オールインなどの額は比べない（ベット / レイズ / 額だけの額だけ比べる）。"""
    flags = tuple(getattr(event, "parse_flags", ()) or ())
    if "amount_only" in flags:
        key = f"amount {event.amount}"
    elif "check_around" in flags:
        key = "check_around"
    elif getattr(event, "hand_name", None):
        key = f"end_hand:{event.hand_name}"
    elif event.action in ("bet", "raise") and event.amount:
        key = f"{event.action} {event.amount}"
    else:
        key = str(event.action)
    if getattr(event, "seat", None) is not None:
        key += f" @{event.seat}"
    elif getattr(event, "position", None):
        key += f" @{event.position}"
    return key


def parse_keys(text: str) -> list[str]:
    return [event_key(e) for e in parse_actions(text)]


_WAGER = re.compile(r"^(?:bet|raise|amount) (\d+)")


def engine_key(key: str) -> str:
    """ベット / レイズ / 額だけは、どれになるかをエンジンが卓の状態で決めるので同じに扱う（"wager 1300"）。"""
    return _WAGER.sub(r"wager \1", key)


# ───────────────────────── 録音 ─────────────────────────


def write_wav(path: Path, pcm: bytes, rate: int) -> None:
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(pcm)


def peak_rms(pcm: bytes, chunk: int = CHUNK) -> float:
    """チャンク（本番の有音ゲートと同じ 1024 サンプル）ごとの RMS の最大。"""
    step = chunk * 2
    return max((_calc_rms(pcm[i:i + step]) for i in range(0, len(pcm), step)), default=0.0)


class DeviceRecorder:
    """1 本のマイクの録音。ずっと録り続け、句ごとに時刻で切り出す（直近 `RING_SEC` 秒だけ残す）。

    時刻 → サンプルの対応は、チャンクを受け取った時刻とそれまでのサンプル数の組で持つ（マイクの時計と PC の時計の
    ずれが 30 分で積み重ならないように、切り出すたびにいちばん近い組から数える）。
    """

    def __init__(self, label: str, index: int, name: str, rate: int, *, ring_sec: float = RING_SEC,
                 full_path: Optional[Path] = None) -> None:
        self.label = label
        self.index = index
        self.name = name
        self.rate = rate
        self._ring_samples = int(ring_sec * rate)
        self._lock = threading.Lock()
        self._buf = bytearray()
        self._buf_start = 0                    # _buf の先頭のサンプル番号
        self._total = 0                        # これまでに受け取ったサンプル数
        self._marks: deque[tuple[float, int]] = deque()   # (受け取った時刻, そのときの総サンプル数)
        self._levels: deque[tuple[float, float]] = deque()
        self.last_at: Optional[float] = None
        self.error: Optional[str] = None
        self.alive = True
        self._full: Optional[wave.Wave_write] = None
        self.full_path = full_path
        if full_path is not None:
            try:
                self._full = wave.open(str(full_path), "wb")
                self._full.setnchannels(1)
                self._full.setsampwidth(2)
                self._full.setframerate(rate)
            except OSError:
                logger.exception("ずっと録る音声のファイルを作れません: %s", full_path)
                self._full = None

    def feed(self, data: bytes, at: float) -> None:
        n = len(data) // 2
        if n <= 0:
            return
        rms = _calc_rms(data)
        with self._lock:
            self._buf += data[: n * 2]
            self._total += n
            self._marks.append((at, self._total))
            extra = self._total - self._buf_start - self._ring_samples
            if extra > self.rate * 5:          # 5 秒ぶん超えてからまとめて捨てる
                del self._buf[: extra * 2]
                self._buf_start += extra
                while len(self._marks) > 2 and self._marks[1][1] <= self._buf_start:
                    self._marks.popleft()
            self._levels.append((at, rms))
            while self._levels and self._levels[0][0] < at - LEVEL_WINDOW:
                self._levels.popleft()
            self.last_at = at
        if self._full is not None:
            try:
                self._full.writeframes(data[: n * 2])
            except (OSError, ValueError):
                logger.exception("ずっと録る音声を書けません（以降は書きません）")
                self._full = None

    def level(self, now: Optional[float] = None) -> float:
        with self._lock:
            if now is None:
                return max((r for _, r in self._levels), default=0.0)
            return max((r for t, r in self._levels if t >= now - LEVEL_WINDOW), default=0.0)

    def _sample_at(self, t: float) -> Optional[int]:
        if not self._marks:
            return None
        times = [m[0] for m in self._marks]
        i = bisect_left(times, t)
        best = min((j for j in (i - 1, i) if 0 <= j < len(times)), key=lambda j: abs(times[j] - t))
        tm, n = self._marks[best]
        return int(round(n - (tm - t) * self.rate))

    def cut(self, t0: float, t1: float) -> bytes:
        """時刻 t0〜t1 の音声（残っている範囲だけ）。"""
        with self._lock:
            a, b = self._sample_at(t0), self._sample_at(t1)
            if a is None or b is None:
                return b""
            a = max(a, self._buf_start)
            b = min(b, self._total)
            if b <= a:
                return b""
            return bytes(self._buf[(a - self._buf_start) * 2:(b - self._buf_start) * 2])

    def close(self) -> None:
        self.alive = False
        if self._full is not None:
            try:
                self._full.close()
            except (OSError, ValueError):
                pass
            self._full = None


def _capture(recorder: DeviceRecorder, stream: Any, stop: threading.Event, clock: Callable[[], float]) -> None:
    """マイクを読み続ける（録音のスレッド）。読めない状態が続いたら止める。"""
    failures = 0
    try:
        while not stop.is_set():
            try:
                data = stream.read(CHUNK, exception_on_overflow=False)
            except OSError as e:
                failures += 1
                recorder.error = f"読めません: {e}"
                if failures >= 20:
                    break
                time.sleep(0.05)
                continue
            if not data:
                break
            failures = 0
            recorder.feed(data, clock())
    finally:
        for method in ("stop_stream", "close"):
            try:
                getattr(stream, method, lambda: None)()
            except Exception:  # noqa: BLE001 — 閉じられなくても録った分は残る
                pass
        recorder.alive = False


def _device_hint(devices: list[dict]) -> Optional[str]:
    """マイクの一覧に添える注意（WDM-KS 方式しか見えない = RDP の音声が接続元に回っている）。"""
    from tools.audio_check import RDP_AUDIO_HINT, only_wdm_ks

    return RDP_AUDIO_HINT if only_wdm_ks(devices) else None


class PyAudioBackend:
    """PC のマイク（PyAudio）。テストでは同じ形の偽物を渡す。"""

    def __init__(self) -> None:
        import pyaudio  # type: ignore[import]

        self._mod = pyaudio
        self._pa = pyaudio.PyAudio()

    def devices(self, rate: int) -> list[dict]:
        from tools.audio_check import list_input_devices

        return [{"index": d.index, "name": d.name, "api": d.api, "rate_ok": d.rate_ok, "default": d.default}
                for d in list_input_devices(self._pa, self._mod, rate)]

    def open(self, index: int, rate: int) -> Any:
        return self._pa.open(format=self._mod.paInt16, channels=1, rate=rate, input=True,
                             input_device_index=index, frames_per_buffer=CHUNK)

    def close(self) -> None:
        try:
            self._pa.terminate()
        except Exception:  # noqa: BLE001
            pass


# ───────────────────────── 読み上げの 1 回（フォルダ） ─────────────────────────


def corpus_root(log_dir: Path) -> Path:
    return Path(log_dir) / CORPUS_DIR


def _slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_\-]+", "", name)[:24] or "speaker"


def new_folder(log_dir: Path, speaker: str, now: datetime) -> Path:
    base = corpus_root(log_dir) / f"{now:%Y%m%d_%H%M%S}_{_slug(speaker)}"
    folder, k = base, 2
    while folder.exists():
        folder = base.with_name(f"{base.name}_{k}")
        k += 1
    folder.mkdir(parents=True)
    return folder


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _append_jsonl(path: Path, row: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_meta(folder: Path) -> Optional[dict]:
    try:
        data = json.loads((folder / META).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def write_meta(folder: Path, meta: dict) -> None:
    tmp = folder / (META + ".tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(folder / META)


def read_labels(folder: Path) -> list[dict]:
    return _read_jsonl(folder / LABELS)


def latest_labels(folder: Path) -> dict[str, dict]:
    """句ごとの最後の行（読み直したら新しい方、飛ばした句は飛ばした行）。"""
    out: dict[str, dict] = {}
    for row in read_labels(folder):
        if row.get("id"):
            out[row["id"]] = row
    return out


@dataclass
class _Take:
    phrase: Phrase
    take: int
    position: int          # 読む順の何番目か（0 始まり）
    t0: float
    t1: float


class CorpusSession:
    """1 人が 1 周読む間の状態（いまの句・切り出し待ち・保存）。操作は HTTP のスレッドから、切り出しは裏のスレッドから。"""

    def __init__(self, folder: Path, meta: dict, phrases: list[Phrase], recorders: list[DeviceRecorder], *,
                 start_index: int = 0, takes: Optional[dict[str, int]] = None, speech_rms: float = 300.0,
                 clock: Callable[[], float] = time.time,
                 on_saved: Optional[Callable[[Path, dict, dict[str, bytes]], None]] = None) -> None:
        self.folder = folder
        self.meta = meta
        self.phrases = phrases
        self.recorders = recorders
        self.index = start_index
        self.takes: dict[str, int] = dict(takes or {})
        self.speech_rms = speech_rms
        self.clock = clock
        self.on_saved = on_saved
        self.t0 = clock()
        self.pending: list[_Take] = []
        self.saved = 0
        self.skipped = 0
        self.quiet = 0
        self.last_saved: Optional[dict] = None
        self.finished = False
        self.stop = threading.Event()
        self._lock = threading.RLock()

    # ――― 読む人の操作 ―――

    def current(self) -> Optional[Phrase]:
        return self.phrases[self.index] if 0 <= self.index < len(self.phrases) else None

    @property
    def reading_done(self) -> bool:
        return self.index >= len(self.phrases)

    def _expected(self, index: Any) -> bool:
        """押した画面の句が、いまの句か（2 回押し・古い画面からの操作で句を飛ばさない）。"""
        return index is None or index == self.index

    def next(self, index: Any = None) -> bool:
        with self._lock:
            phrase = self.current()
            if self.finished or phrase is None or not self._expected(index):
                return False
            now = self.clock()
            take = self.takes.get(phrase.id, 0) + 1
            self.takes[phrase.id] = take
            self.pending.append(_Take(phrase, take, self.index, self.t0, now))
            self.index += 1
            self.t0 = now
            return True

    def redo(self) -> bool:
        """いまの句を読み直す（ここまでの音声は使わない）。"""
        with self._lock:
            if self.finished or self.current() is None:
                return False
            self.t0 = self.clock()
            return True

    def back(self) -> bool:
        """前の句に戻る（読み直した音声を新しい回として残す。前の回のファイルも残る）。"""
        with self._lock:
            if self.finished or self.index <= 0:
                return False
            self.index -= 1
            self.t0 = self.clock()
            return True

    def skip(self, index: Any = None) -> bool:
        with self._lock:
            phrase = self.current()
            if self.finished or phrase is None or not self._expected(index):
                return False
            now = self.clock()
            take = self.takes.get(phrase.id, 0) + 1
            self.takes[phrase.id] = take
            _append_jsonl(self.folder / LABELS, {
                **phrase.label(), "take": take, "round": self.meta.get("round"), "position": self.index + 1,
                "skipped": True, "t": round(now, 3),
            })
            self.skipped += 1
            self.index += 1
            self.t0 = now
            return True

    # ――― 切り出し ―――

    def process_pending(self, now: float, force: bool = False) -> list[dict]:
        """音声がそろった句を切り出して保存する（「次へ」から `POST_ROLL` 秒たって、どのマイクもそこまで読めたら）。"""
        with self._lock:
            ready = [t for t in self.pending if force or self._covered(t.t1 + POST_ROLL)]
            for t in ready:
                self.pending.remove(t)
        return [self._save(t) for t in ready]

    def _covered(self, until: float) -> bool:
        live = [r for r in self.recorders if r.alive]
        return all((r.last_at or 0.0) >= until for r in live)

    def _save(self, take: _Take) -> dict:
        files: dict[str, str] = {}
        peaks: dict[str, int] = {}
        pcms: dict[str, bytes] = {}
        for rec in self.recorders:
            pcm = rec.cut(take.t0 - PRE_ROLL, take.t1 + POST_ROLL)
            if not pcm:
                continue
            name = f"{take.phrase.id}_t{take.take}_{rec.label}.wav"
            try:
                write_wav(self.folder / name, pcm, rec.rate)
            except OSError:
                logger.exception("句の音声を保存できません: %s", name)
                continue
            files[rec.label] = name
            peaks[rec.label] = int(round(peak_rms(pcm)))
            pcms[rec.label] = pcm
        quiet = bool(peaks) and all(v < self.speech_rms for v in peaks.values())
        row = {
            **take.phrase.label(), "take": take.take, "round": self.meta.get("round"), "position": take.position + 1,
            "t0": round(take.t0, 3), "t1": round(take.t1, 3), "sec": round(take.t1 - take.t0, 2),
            "files": files, "peak": peaks, "quiet": quiet, "mics": {r.label: r.name for r in self.recorders},
        }
        if not files:
            row["error"] = "録音がありません"
        with self._lock:
            _append_jsonl(self.folder / LABELS, row)
            self.saved += 1
            self.quiet += 1 if quiet else 0
            self.last_saved = row
        if self.on_saved is not None and pcms:
            try:
                self.on_saved(self.folder, row, pcms)
            except Exception:  # noqa: BLE001 — 聞き取りに回せなくても録音は残る
                logger.exception("聞き取りに回せませんでした: %s", row.get("id"))
        return row

    def finish(self, wait_sec: float = POST_ROLL + 1.0) -> None:
        """読むのを終える: 切り出し待ちを保存し（語尾が入るまで少し待つ）、録音を止め、meta を閉じる。"""
        with self._lock:
            if self.finished:
                return
            self.finished = True
        deadline = time.monotonic() + wait_sec
        while self.pending and time.monotonic() < deadline:
            self.process_pending(self.clock())
            time.sleep(0.05)
        self.process_pending(self.clock(), force=True)
        self.stop.set()
        for rec in self.recorders:
            rec.close()
        meta = read_meta(self.folder) or self.meta
        meta.update({
            "finished": self.reading_done, "stopped_at": datetime.now().isoformat(timespec="seconds"),
            "saved": sum(1 for r in latest_labels(self.folder).values() if r.get("files")),
            "skipped": sum(1 for r in latest_labels(self.folder).values() if r.get("skipped")),
        })
        write_meta(self.folder, meta)
        self.meta = meta

    def view(self, now: float) -> dict:
        with self._lock:
            phrase = self.current()
            nxt = self.phrases[self.index + 1] if self.index + 1 < len(self.phrases) else None
            last = self.last_saved
            last_files = (last or {}).get("files") or {}
            return {
                "folder": self.folder.name, "speaker": self.meta.get("speaker"), "round": self.meta.get("round"),
                "index": self.index, "total": len(self.phrases), "phrase": phrase.view() if phrase else None,
                "next": nxt.text if nxt else None, "take_sec": round(max(0.0, now - self.t0), 1),
                "saved": self.saved, "skipped": self.skipped, "quiet": self.quiet, "pending": len(self.pending),
                "mics": [{"label": r.label, "name": r.name, "level": int(round(r.level(now))), "error": r.error,
                          "alive": r.alive} for r in self.recorders],
                "last": None if last is None else {
                    "id": last["id"], "text": last["text"], "take": last["take"], "quiet": last.get("quiet"),
                    "file": last_files.get("m1") or next(iter(last_files.values()), None),
                },
                "reading_done": self.reading_done, "finished": self.finished,
            }


def resume_point(folder: Path, order: list[Phrase]) -> tuple[int, dict[str, int]]:
    """続きから読むときの位置（まだ行の無い最初の句）と、句ごとの回数。"""
    takes: dict[str, int] = {}
    for row in read_labels(folder):
        pid = row.get("id")
        if pid:
            takes[pid] = max(takes.get(pid, 0), int(row.get("take") or 0))
    index = next((i for i, p in enumerate(order) if p.id not in takes), len(order))
    return index, takes


# ───────────────────────── 聞き取り（本番と同じ経路） ─────────────────────────


class _NoAsr:
    ready = True

    def recognize(self, audio_bytes: bytes):  # pragma: no cover — 切り出しだけに使うので呼ばれない
        raise RuntimeError("聞き取りは使いません")


def _hearer_class():
    """`AudioThread` を借りて、録った音声に本番と同じ発話の切り出しと聞き取りをかける（import を遅らせる）。"""
    from audio.recorder import AudioThread
    from core.event_queue import make_audio_queue

    class Hearer(AudioThread):
        def __init__(self, transcriber: Any, *, rate: int, speech_rms: float, min_speech_sec: float) -> None:
            super().__init__(audio_queue=make_audio_queue(), sample_rate=rate, transcriber=transcriber,
                             min_speech_sec=min_speech_sec, speech_rms=speech_rms)
            self._segments: list[bytes] = []
            self._drops: list[float] = []
            self._on_dropped = self._drops.append

        def segments(self, pcm: bytes) -> tuple[list[bytes], list[float]]:
            self._segments, self._drops = [], []
            self._on_dropped = self._drops.append
            step = CHUNK * 2
            chunks = iter([pcm[i:i + step] for i in range(0, len(pcm), step)])
            self._stop_event.clear()
            self._capture_loop(lambda: next(chunks, b""), CHUNK)
            return self._segments, self._drops

        def _enqueue_utterance(self, audio_bytes: bytes, utterance_start_ts: float) -> None:
            self._segments.append(audio_bytes)

        def hear(self, segment: bytes):
            got: list = []
            self._on_transcript = got.append
            self._process_chunk(segment, None)
            self._on_transcript = None
            while not self._audio_queue.empty():        # 本番ではエンジンが受け取るアクション（ここでは使わない）
                self._audio_queue.get_nowait()
            return got[0] if got else None

    return Hearer


def live_events(transcript: Any, ear: Optional[dict]) -> list:
    """本番の経路で 1 つの発話から読むアクション（`AudioThread._process_chunk`: Whisper の読みに第 2 の耳を
    `second_ear.apply_ear` の規則で重ねる = 読めない発話の聞き直し・額の無いベット / レイズの額）。"""
    from audio.second_ear import apply_ear

    if transcript is not None and transcript.no_speech:
        return []
    events = list(transcript.events) if transcript is not None else []
    text = transcript.text if transcript is not None else ""
    question = bool(transcript is not None and transcript.question)
    return apply_ear(events, text, ear, question=question)[0]


def transcribe_take(pcm: bytes, hearer: Any, ear: Any = None, *, rate: int = 16000,
                    asr: bool = True) -> dict:
    """1 つの句の音声を本番と同じ経路で聞き取る。発話ごとの Whisper の文・第 2 の耳（読めた発話も聞く = 比べるため）・
    本番の経路で読んだアクション。"""
    from audio.second_ear import pcm16_samples

    segments, drops = hearer.segments(pcm)
    rows, heard = [], []
    for seg in segments:
        t = hearer.hear(seg) if asr else None
        row: dict[str, Any] = {"sec": round(len(seg) / 2 / rate, 2)}
        if t is not None:
            row.update(text=t.text, confidence=None if t.confidence is None else round(t.confidence, 3),
                       no_speech=bool(t.no_speech), noise=bool(t.noise), question=bool(t.question),
                       infer_sec=round(t.infer_sec, 2), whisper=[event_key(e) for e in t.events])
        else:
            row.update(text="", whisper=[])
        ear_dict = None
        if ear is not None and not (t is not None and t.no_speech):
            started = time.perf_counter()
            try:
                ear_dict = ear.hear(pcm16_samples(seg, rate)).to_dict()
                ear_dict["sec"] = round(time.perf_counter() - started, 3)
            except Exception:  # noqa: BLE001 — 第 2 の耳が失敗しても Whisper の結果は残す
                logger.exception("第 2 の耳で聞けませんでした")
        row["ear"] = ear_dict
        row["heard"] = [event_key(e) for e in live_events(t, ear_dict)]
        heard.extend(row["heard"])
        rows.append(row)
    return {"segments": rows, "heard": heard, "dropped": [round(d, 2) for d in drops]}


def _load_audio_cfg(config: Optional[str] = None) -> dict:
    from tools.audio_check import load_audio_config

    try:
        return load_audio_config(config)
    except (OSError, json.JSONDecodeError):
        return {}


class Listener:
    """聞き取りに使うモデル（Whisper と第 2 の耳）と、発話の切り出し（本番の設定 = config の audio）。"""

    def __init__(self, cfg: dict, *, root: Path = ROOT, model: Optional[str] = None, ear: bool = True,
                 make_transcriber: Optional[Callable[[], Any]] = None, make_ear: Optional[Callable[[], Any]] = None
                 ) -> None:
        from audio.recorder import _MIN_BUFFER_SECONDS

        self.rate = int(cfg.get("sample_rate", 16000))
        self.model = model or str(cfg.get("whisper_model", "medium"))
        if make_transcriber is not None:
            self.transcriber = make_transcriber()
        else:
            from audio.recognizer import WhisperTranscriber

            self.transcriber = WhisperTranscriber(
                model_size=self.model, language=cfg.get("language", "ja"), beam_size=int(cfg.get("beam_size", 5)),
                temperature_fallback=bool(cfg.get("temperature_fallback", False)),
                vad_threshold=float(cfg.get("vad_threshold", 0.5)), cpu_threads=int(cfg.get("cpu_threads", 0) or 0))
        self.asr = bool(getattr(self.transcriber, "ready", True))
        self.load_error = getattr(self.transcriber, "load_error", None)
        self.ear = None
        self.ear_message = "第 2 の耳は使いません"
        if make_ear is not None:
            self.ear = make_ear()
        elif ear:
            from audio.second_ear import load_live

            self.ear, self.ear_message = load_live(root, cfg)
        hearer_cls = _hearer_class()
        self.hearer = hearer_cls(self.transcriber if self.asr else _NoAsr(), rate=self.rate,
                                 speech_rms=float(cfg.get("speech_rms", 300)),
                                 min_speech_sec=float(cfg.get("min_speech_sec", _MIN_BUFFER_SECONDS)))

    def take_row(self, label: dict, mic: str, pcm: bytes) -> dict:
        result = transcribe_take(pcm, self.hearer, self.ear, rate=self.rate, asr=self.asr)
        return {"id": label.get("id"), "take": label.get("take"), "mic": mic,
                "file": (label.get("files") or {}).get(mic), "model": self.model if self.asr else None,
                "ear_model": bool(self.ear), "at": datetime.now().isoformat(timespec="seconds"), **result}


class AsrWorker:
    """録りながら裏で聞き取る（モデルは最初の句が来る前から読み込む）。読み込めなければ録音だけ続ける
    （あとで `transcribe` で書ける）。"""

    def __init__(self, cfg: dict, *, root: Path = ROOT, listener_factory: Optional[Callable[[], Listener]] = None
                 ) -> None:
        self.cfg = cfg
        self.root = root
        self._factory = listener_factory or (lambda: Listener(cfg, root=root))
        self._q: "queue.Queue[Optional[tuple[Path, dict, dict[str, bytes]]]]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._busy = 0
        self.state = "idle"          # idle | loading | ready | unavailable
        self.message = ""
        self.done = 0

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True, name="CorpusASR")
            self._thread.start()

    def submit(self, folder: Path, label: dict, pcms: dict[str, bytes]) -> None:
        self._q.put((folder, label, pcms))
        self.start()

    def backlog(self) -> int:
        return self._q.qsize() + self._busy

    def status(self) -> dict:
        return {"state": self.state, "message": self.message, "backlog": self.backlog(), "done": self.done}

    def close(self) -> None:
        self._q.put(None)

    def _run(self) -> None:
        self.state = "loading"
        self.message = "聞き取りのモデルを読み込んでいます"
        try:
            listener = self._factory()
        except Exception as e:  # noqa: BLE001 — 読み込めなくても録音は続ける
            logger.exception("聞き取りのモデルを読み込めません")
            listener, self.state, self.message = None, "unavailable", f"{type(e).__name__}: {e}"
        if listener is not None and not listener.asr:
            self.state, self.message = "unavailable", f"Whisper を読み込めません（{listener.load_error}）"
            if listener.ear is None:
                listener = None
        elif listener is not None:
            self.state, self.message = "ready", listener.ear_message
        while True:
            item = self._q.get()
            if item is None:
                break
            if listener is None:
                continue                     # 録音だけ（あとで transcribe）
            folder, label, pcms = item
            self._busy = 1
            try:
                for mic, pcm in pcms.items():
                    _append_jsonl(folder / TRANSCRIPTS, listener.take_row(label, mic, pcm))
                self.done += 1
            except Exception:  # noqa: BLE001
                logger.exception("聞き取りに失敗しました: %s", label.get("id"))
            finally:
                self._busy = 0


# ───────────────────────── 画面のサーバ（真のアクション入力の画面に載せる） ─────────────────────────


class CorpusApp:
    """読み上げ集の画面の中身（`/api/corpus/...`）。真のアクション入力のサーバが持つ（`ground_truth_ui`）。"""

    def __init__(self, log_dir: Path, *, root: Path = ROOT, audio_cfg: Optional[dict] = None,
                 backend_factory: Optional[Callable[[], Any]] = None, asr: Optional[AsrWorker] = None,
                 use_asr: bool = True, clock: Callable[[], float] = time.time) -> None:
        self.log_dir = Path(log_dir)
        self.root = root
        self.cfg = audio_cfg if audio_cfg is not None else _load_audio_cfg()
        self.rate = int(self.cfg.get("sample_rate", 16000))
        self.speech_rms = float(self.cfg.get("speech_rms", 300))
        self.configured_device = int(self.cfg.get("device_id", 0) or 0)
        self._backend_factory = backend_factory or PyAudioBackend
        self._backend: Any = None
        self.backend_error: Optional[str] = None
        self._devices: Optional[list[dict]] = None
        self.asr = asr if asr is not None else (AsrWorker(self.cfg, root=root) if use_asr else None)
        self.clock = clock
        self.session: Optional[CorpusSession] = None
        self._lock = threading.RLock()
        self._summary_cache: dict[str, tuple[tuple, dict]] = {}

    # ――― マイク ―――

    def devices(self, refresh: bool = False) -> list[dict]:
        with self._lock:
            if refresh and not self.recording:
                if self._backend is not None:
                    self._backend.close()
                self._backend, self._devices = None, None
            if self._backend is None:
                try:
                    self._backend = self._backend_factory()
                    self.backend_error = None
                except Exception as e:  # noqa: BLE001 — PyAudio が無い・開けない
                    self.backend_error = f"マイクを使えません（{type(e).__name__}: {e}）"
                    return []
            if self._devices is None:
                try:
                    self._devices = [dict(d, configured=d["index"] == self.configured_device)
                                     for d in self._backend.devices(self.rate)]
                except Exception as e:  # noqa: BLE001
                    self.backend_error = f"マイクの一覧を取れません（{type(e).__name__}: {e}）"
                    return []
            return list(self._devices)

    @property
    def recording(self) -> bool:
        return self.session is not None and not self.session.finished

    # ――― 状態 ―――

    def state(self) -> dict:
        now = self.clock()
        out: dict[str, Any] = {
            "asr": self.asr.status() if self.asr is not None else {"state": "off", "backlog": 0},
            "recording": self.recording, "total_phrases": len(PHRASES),
            "session": self.session.view(now) if self.session is not None else None,
        }
        if self.asr is not None and self.asr.state == "idle":
            self.asr.start()                 # 画面を開いたらモデルを読み込み始める（最初の句までに間に合うように）
        if not self.recording:
            out["devices"] = self.devices()
            out["device_error"] = self.backend_error
            out["device_hint"] = _device_hint(out["devices"])
            out["recent"] = self.recent()
        return out

    def recent(self, limit: int = 8) -> list[dict]:
        root = corpus_root(self.log_dir)
        if not root.is_dir():
            return []
        folders = sorted((d for d in root.iterdir() if d.is_dir() and (d / META).is_file()),
                         key=lambda d: d.name, reverse=True)[:limit]
        return [self._folder_summary(d) for d in folders]

    def _folder_summary(self, folder: Path) -> dict:
        stamp = tuple(((folder / n).stat().st_mtime if (folder / n).exists() else 0) for n in (META, LABELS, TRANSCRIPTS))
        cached = self._summary_cache.get(folder.name)
        if cached and cached[0] == stamp:
            return cached[1]
        meta = read_meta(folder) or {}
        latest = latest_labels(folder)
        order = meta.get("order") or []
        result = evaluate_folder(folder) if (folder / TRANSCRIPTS).is_file() else None
        m1 = (result or {}).get("mics", {}).get("m1") or {}
        summary = {
            "folder": folder.name, "speaker": meta.get("speaker"), "round": meta.get("round"),
            "saved": sum(1 for r in latest.values() if r.get("files")),
            "skipped": sum(1 for r in latest.values() if r.get("skipped")),
            "total": len(order), "finished": bool(meta.get("finished")),
            "done": len(latest), "transcribed": (result or {}).get("transcribed", 0),
            "match": m1.get("live", {}).get("rate"),
        }
        self._summary_cache[folder.name] = (stamp, summary)
        return summary

    # ――― 操作 ―――

    def start(self, body: dict) -> tuple[int, dict]:
        speaker = str(body.get("speaker") or "").strip()[:40]
        mics = body.get("mics")
        resume = body.get("resume")
        try:
            round_no = int(body.get("round") or 1)
        except (TypeError, ValueError):
            round_no = 1
        if not speaker and not resume:
            return 400, {"code": "invalid", "message": "読む人の名前を入れてください"}
        if (not isinstance(mics, list) or not mics or len(mics) > MAX_MICS
                or not all(isinstance(m, int) for m in mics) or len(set(mics)) != len(mics)):
            return 400, {"code": "invalid", "message": f"マイクを 1〜{MAX_MICS} 本選んでください"}
        with self._lock:
            if self.recording:
                return 409, {"code": "busy", "message": "読み上げの途中です（先に「ここで終わる」）"}
            devices = {d["index"]: d for d in self.devices()}
            unknown = [m for m in mics if m not in devices]
            if unknown:
                return 400, {"code": "invalid", "message": f"マイク {unknown} が見つかりません（探し直してください）"}
            if resume:
                folder = corpus_root(self.log_dir) / str(resume)
                meta = read_meta(folder) if _FOLDER_RE.match(str(resume)) else None
                if meta is None:
                    return 404, {"code": "not_found", "message": "続きの読み上げが見つかりません"}
                order = [PHRASES_BY_ID[i] for i in meta.get("order") or [] if i in PHRASES_BY_ID]
                start_index, takes = resume_point(folder, order)
            else:
                folder = new_folder(self.log_dir, speaker, datetime.now())
                order = reading_order(PHRASES, round_no)
                start_index, takes = 0, {}
                meta = {
                    "tool": "read_corpus", "phrase_set": PHRASE_SET, "speaker": speaker, "round": round_no,
                    "created_at": datetime.now().isoformat(timespec="seconds"), "rate": self.rate,
                    "pre_roll": PRE_ROLL, "post_roll": POST_ROLL, "order": [p.id for p in order],
                    "audio_config": {k: self.cfg.get(k) for k in (
                        "device_id", "whisper_model", "speech_rms", "min_speech_sec", "vad_threshold", "beam_size")},
                    "runs": [], "finished": False,
                }
            recorders: list[DeviceRecorder] = []
            streams: list[Any] = []
            stamp = datetime.now().strftime("%H%M%S")
            for k, m in enumerate(mics):
                label = f"m{k + 1}"
                try:
                    streams.append(self._backend.open(m, self.rate))
                except Exception as e:  # noqa: BLE001
                    for s in streams:
                        try:
                            s.close()
                        except Exception:  # noqa: BLE001
                            pass
                    for r in recorders:
                        r.close()
                    from tools.audio_check import open_error_hint

                    hint = open_error_hint(devices[m])
                    return 400, {"code": "mic", "message": f"マイク {m}（{devices[m]['name']}）を開けません: {e}"
                                                        + (f"。{hint}" if hint else "")}
                full = folder / f"full_{label}.wav"
                if full.exists():
                    full = folder / f"full_{label}_{stamp}.wav"
                recorders.append(DeviceRecorder(label, m, devices[m]["name"], self.rate, full_path=full))
            meta.setdefault("runs", []).append({
                "started_at": datetime.now().isoformat(timespec="seconds"), "start_index": start_index,
                "mics": [{"label": r.label, "index": r.index, "name": r.name,
                          "api": devices[r.index].get("api")} for r in recorders],
            })
            write_meta(folder, meta)
            session = CorpusSession(folder, meta, order, recorders, start_index=start_index, takes=takes,
                                    speech_rms=self.speech_rms, clock=self.clock,
                                    on_saved=self.asr.submit if self.asr is not None else None)
            for rec, stream in zip(recorders, streams):
                threading.Thread(target=_capture, args=(rec, stream, session.stop, self.clock), daemon=True,
                                 name=f"CorpusMic-{rec.label}").start()
            threading.Thread(target=self._cut_loop, args=(session,), daemon=True, name="CorpusCut").start()
            if self.asr is not None:
                self.asr.start()
            self.session = session
        return 200, self.state()

    def _cut_loop(self, session: CorpusSession) -> None:
        while not session.finished:
            try:
                session.process_pending(self.clock())
                if session.reading_done and not session.pending:
                    session.finish(0)
                    break
            except Exception:  # noqa: BLE001 — 切り出しの失敗で画面を止めない
                logger.exception("読み上げの切り出しに失敗しました")
            time.sleep(0.05)

    def act(self, verb: str, body: Optional[dict]) -> tuple[int, dict]:
        body = body if isinstance(body, dict) else {}
        session = self.session
        if session is None or session.finished:
            return 409, {"code": "not_running", "message": "読み上げを始めていません"}
        if verb == "next":
            session.next(body.get("index"))
        elif verb == "skip":
            session.skip(body.get("index"))
        elif verb == "redo":
            session.redo()
        elif verb == "back":
            session.back()
        elif verb == "finish":
            session.finish()
        else:
            return 404, {"code": "not_found", "message": verb}
        return 200, self.state()

    def audio_path(self, folder: str, name: str) -> Optional[Path]:
        if not _FOLDER_RE.match(folder) or not _WAV_RE.match(name):
            return None
        path = corpus_root(self.log_dir) / folder / name
        return path if path.is_file() else None

    def route(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        """`/api/corpus/...` を処理する。音声は (200, Path) を返す（呼ぶ側が送る）。"""
        rest = path[len("/api/corpus/"):].strip("/") if path.startswith("/api/corpus/") else ""
        if method == "GET":
            if rest == "state":
                return 200, self.state()
            if rest == "devices":
                devices = self.devices(refresh=True)
                return 200, {"devices": devices, "device_error": self.backend_error,
                             "device_hint": _device_hint(devices)}
            m = re.match(r"^audio/([^/]+)/([^/]+)$", rest)
            if m:
                audio = self.audio_path(m.group(1), m.group(2))
                return (200, audio) if audio is not None else (404, {"code": "not_found", "message": "音声がありません"})
            return 404, {"code": "not_found", "message": path}
        if method == "POST":
            if rest == "start":
                return self.start(body if isinstance(body, dict) else {})
            if rest in ("next", "skip", "redo", "back", "finish"):
                return self.act(rest, body)
        return 404, {"code": "not_found", "message": path}

    def close(self) -> None:
        session = self.session
        if session is not None and not session.finished:
            session.finish(0)
        if self.asr is not None:
            self.asr.close()
        if self._backend is not None:
            self._backend.close()
            self._backend = None


# ───────────────────────── 評価 ─────────────────────────


def reread_segment(seg: dict, route: str = "live") -> list[str]:
    """保存した 1 発話を、いまの読み取りで読む。live = 本番の経路（Whisper の読みに第 2 の耳を
    `second_ear.apply_ear` の規則で重ねる）/ whisper = Whisper だけ / ear = 第 2 の耳だけ（候補と自由に聞いた文が
    同じアクション = 本番と同じ厳しめ）。"""
    from audio.second_ear import agreed_candidate, apply_ear

    if route == "ear":
        text = agreed_candidate(seg.get("ear"))
        return parse_keys(text) if text else []
    text = (seg.get("text") or "").strip()
    if seg.get("no_speech"):
        return []
    noise = bool(text) and (is_prompt_echo(text) or is_implausibly_long(text, float(seg.get("sec") or 0.0)))
    question = bool(text) and not noise and is_question(text)
    events = [] if noise or not text else parse_actions(text)
    if route == "live":
        events, _ = apply_ear(events, text, seg.get("ear"), question=question)
    return [event_key(e) for e in events]


ROUTES = (("live", "本番の経路"), ("whisper", "Whisper だけ"), ("ear", "第 2 の耳だけ"))


def evaluate_folder(folder: Path) -> dict:
    """読み上げ集 1 つを評価する: 句（最後に読んだ回）ごとに、マイク × 方式で正解と比べる。

    一致 = ベット / レイズ / 額だけを同じに扱って（エンジンが卓の状態で決める）アクションの列が同じ。
    完全一致 = そのまま同じ。
    """
    meta = read_meta(folder) or {}
    latest = {pid: row for pid, row in latest_labels(folder).items() if row.get("files") and not row.get("skipped")}
    rows: dict[tuple[str, int, str], dict] = {}
    for row in _read_jsonl(folder / TRANSCRIPTS):
        rows[(row.get("id"), row.get("take"), row.get("mic"))] = row
    mics = sorted({mic for label in latest.values() for mic in (label.get("files") or {})})
    out: dict[str, Any] = {"folder": folder.name, "speaker": meta.get("speaker"), "round": meta.get("round"),
                           "phrases": len(latest), "transcribed": 0, "mics": {}}
    for mic in mics:
        per_route: dict[str, dict] = {r: {"ok": 0, "exact": 0, "n": 0} for r, _ in ROUTES}
        by_kind: dict[str, Counter] = {}
        misses: list[dict] = []
        name = None
        for pid, label in latest.items():
            if mic not in (label.get("files") or {}):
                continue
            name = name or (label.get("mics") or {}).get(mic)
            row = rows.get((pid, label.get("take"), mic))
            if row is None:
                continue
            expect = list(label.get("expect") or [])
            kind = label.get("kind") or "?"
            counter = by_kind.setdefault(kind, Counter())
            for route, _ in ROUTES:
                got = [k for seg in row.get("segments") or [] for k in reread_segment(seg, route)]
                stat = per_route[route]
                stat["n"] += 1
                exact = got == expect
                ok = [engine_key(k) for k in got] == [engine_key(k) for k in expect]
                stat["exact"] += int(exact)
                stat["ok"] += int(ok)
                if route == "live":
                    counter["n"] += 1
                    counter["ok"] += int(ok)
                    if not ok:
                        misses.append({
                            "id": pid, "text": label.get("text"), "kind": kind, "expect": expect, "got": got,
                            "heard": " / ".join((s.get("text") or "") for s in row.get("segments") or []),
                            "ear": " / ".join(((s.get("ear") or {}).get("text") or "") for s in row.get("segments") or []),
                            "segments": len(row.get("segments") or []),
                        })
        for stat in per_route.values():
            stat["rate"] = round(stat["ok"] / stat["n"], 3) if stat["n"] else None
        out["transcribed"] = max(out["transcribed"], per_route["live"]["n"])
        out["mics"][mic] = {
            "name": name, **per_route,
            "by_kind": {k: {"ok": c["ok"], "n": c["n"]} for k, c in sorted(by_kind.items())},
            "misses": sorted(misses, key=lambda m: m["id"]),
        }
    return out


def find_corpus_folders(root: Path) -> list[Path]:
    root = Path(root)
    if (root / META).is_file():
        return [root]
    return sorted(p.parent for p in root.rglob(META) if (p.parent / LABELS).is_file() or p.parent.name[:8].isdigit())


def format_report(result: dict, show_misses: int = 40) -> list[str]:
    lines = [f"読み上げ集 {result['folder']}（{result.get('speaker') or '?'}・{result.get('round') or '?'} 周目）: "
             f"句 {result['phrases']}・聞き取り済み {result['transcribed']}"]
    if not result["mics"]:
        lines.append("  録った句がありません。")
    for mic, stat in result["mics"].items():
        parts = []
        for route, label in ROUTES:
            s = stat[route]
            if s["n"]:
                parts.append(f"{label} {s['ok']}/{s['n']}（{s['ok'] / s['n']:.0%}・完全一致 {s['exact']}）")
        lines.append(f"  マイク {mic}（{stat.get('name') or '?'}）: " + (" / ".join(parts) or "聞き取りがまだありません"))
        if stat["by_kind"]:
            kinds = " · ".join(f"{KIND_LABELS.get(k, k)} {c['ok']}/{c['n']}" for k, c in stat["by_kind"].items())
            lines.append(f"    種類ごと（本番の経路）: {kinds}")
        for miss in stat["misses"][:show_misses]:
            ear = f" · 第 2 の耳「{miss['ear']}」" if miss["ear"].strip(" /") else ""
            lines.append(f"    {miss['id']}「{miss['text']}」→ 聞こえた「{miss['heard']}」{ear} → "
                         f"{', '.join(miss['got']) or '（読めず）'}（正解 {', '.join(miss['expect']) or 'なし'}）")
        if len(stat["misses"]) > show_misses:
            lines.append(f"    ほか {len(stat['misses']) - show_misses} 句")
    return lines


# ───────────────────────── コマンド ─────────────────────────


def _cmd_phrases(args: argparse.Namespace) -> int:
    for p in PHRASES:
        gap = "（いまの読み取りでは違う）" if p.known_gap else ""
        print(f"{p.id:6} {KIND_LABELS[p.kind]:14} {p.text:28} → {', '.join(p.expect) or 'なし'}{gap}")
    print(f"\n{len(PHRASES)} 句（{PHRASE_SET}）")
    return 0


def _inputs(paths: list[str]) -> tuple[list[Path], Optional[Path]]:
    from tools.eval_store import open_input

    root, tmp = open_input([Path(p) for p in paths])
    folders: list[Path] = []
    if tmp is not None:
        folders = find_corpus_folders(root)
    else:
        for p in paths:
            folders.extend(find_corpus_folders(Path(p)))
    return folders, tmp


def _cmd_eval(args: argparse.Namespace) -> int:
    folders, tmp = _inputs(args.inputs)
    try:
        if not folders:
            print("読み上げ集が見つかりません（logs/corpus/<フォルダ> か、pack_logs の zip を渡してください）。")
            return 1
        results = [evaluate_folder(f) for f in folders]
        if args.json:
            print(json.dumps(results, ensure_ascii=False, indent=2))
        else:
            for result in results:
                for line in format_report(result, args.misses):
                    print(line)
                print()
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)
    return 0


def transcribe_folder(folder: Path, listener: Any, *, redo: bool = False,
                      log: Callable[[str], None] = print) -> int:
    """読み上げ集 1 つの、まだ聞き取っていない句（最後に読んだ回 × マイク）を聞き取って `transcripts.jsonl` に足す。
    `redo` なら前の結果を日時つきの名前に移してから全部。聞き取った数を返す。"""
    from tools.audio_check import read_pcm16

    target = folder / TRANSCRIPTS
    if redo and target.is_file():
        target.replace(folder / f"transcripts_{datetime.now():%Y%m%d_%H%M%S}.jsonl")
    done = {(r.get("id"), r.get("take"), r.get("mic")) for r in _read_jsonl(target)}
    todo = [(label, mic, name) for label in latest_labels(folder).values()
            for mic, name in (label.get("files") or {}).items()
            if (label.get("id"), label.get("take"), mic) not in done and (folder / name).is_file()]
    log(f"{folder.name}: {len(todo)} 件")
    for i, (label, mic, name) in enumerate(todo, 1):
        _append_jsonl(target, listener.take_row(label, mic, read_pcm16(folder / name, listener.rate)))
        if i % 10 == 0 or i == len(todo):
            log(f"  {i}/{len(todo)}")
    return len(todo)


def _cmd_transcribe(args: argparse.Namespace) -> int:
    folders = []
    for p in args.inputs or [str(corpus_root(Path(args.log_dir)))]:
        folders.extend(find_corpus_folders(Path(p)))
    if not folders:
        print("読み上げ集が見つかりません。")
        return 1
    cfg = _load_audio_cfg(args.config)
    print(f"聞き取りのモデル（{args.model or cfg.get('whisper_model', 'medium')}）を読み込んでいます…", flush=True)
    listener = Listener(cfg, model=args.model, ear=not args.no_ear)
    if not listener.asr:
        print(f"Whisper を読み込めません（{listener.load_error}）。第 2 の耳だけで聞きます。" if listener.ear else
              f"Whisper を読み込めません（{listener.load_error}）。")
        if listener.ear is None:
            return 1
    print(listener.ear_message)
    for folder in folders:
        transcribe_folder(folder, listener, redo=args.redo, log=lambda line: print(line, flush=True))
        for line in format_report(evaluate_folder(folder), args.misses):
            print(line)
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    """読み上げ集の画面だけを開く（真のアクション入力の画面を使わないとき・開発用）。"""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    app = CorpusApp(Path(args.log_dir), use_asr=not args.no_asr)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *a: Any) -> None:     # noqa: A003
            logger.debug(fmt, *a)

        def _reply(self, status: int, payload: Any) -> None:
            if isinstance(payload, Path):
                body, ctype = payload.read_bytes(), "audio/wav"
            elif isinstance(payload, str):
                body, ctype = payload.encode("utf-8"), "text/html; charset=utf-8"
            else:
                body, ctype = json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8"
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> Any:
            try:
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n).decode("utf-8")) if 0 < n <= 100_000 else None
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
                return None

        def do_GET(self) -> None:          # noqa: N802
            path = self.path.split("?", 1)[0]
            if path in ("/", "/corpus", "/corpus/"):
                self._reply(200, CORPUS_PAGE)
            else:
                self._reply(*app.route("GET", path))

        def do_POST(self) -> None:         # noqa: N802
            self._reply(*app.route("POST", self.path.split("?", 1)[0], self._body()))

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    print(f"[corpus] http://{args.host}:{args.port}/corpus  (logs: {Path(args.log_dir).resolve()})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.close()
        server.server_close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="読み上げ集（正解つきの発話）を録る・聞き取る・評価する")
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("phrases", help="句の一覧と正解")
    p.set_defaults(func=_cmd_phrases)
    p = sub.add_parser("eval", help="句ごとの正解と聞き取りを比べる（フォルダ・logs・pack_logs の zip）")
    p.add_argument("inputs", nargs="+")
    p.add_argument("--json", action="store_true")
    p.add_argument("--misses", type=int, default=40, help="違った句を何件出すか（既定 40）")
    p.set_defaults(func=_cmd_eval)
    p = sub.add_parser("transcribe", help="録った句を本番と同じ経路で聞き取る（まだの分だけ）")
    p.add_argument("inputs", nargs="*", help="読み上げ集のフォルダ（既定: logs/corpus の全部）")
    p.add_argument("--log-dir", default=str(ROOT / "logs"))
    p.add_argument("--config", default=None)
    p.add_argument("--model", default=None, help="Whisper のモデル（既定: config の audio.whisper_model）")
    p.add_argument("--no-ear", action="store_true", help="第 2 の耳を使わない")
    p.add_argument("--redo", action="store_true", help="全部聞き取り直す（前の結果は日時つきの名前で残す）")
    p.add_argument("--misses", type=int, default=20)
    p.set_defaults(func=_cmd_transcribe)
    p = sub.add_parser("serve", help="読み上げ集の画面だけを開く（既定 http://127.0.0.1:8792/corpus）")
    p.add_argument("--log-dir", default="./logs")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8792)
    p.add_argument("--no-asr", action="store_true", help="録りながら聞き取らない")
    p.set_defaults(func=_cmd_serve)
    return ap


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    return args.func(args)


# ───────────────────────── 画面 ─────────────────────────

CORPUS_PAGE = r"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>読み上げ集</title>
<style>
 :root { color-scheme: dark; }
 * { box-sizing: border-box; }
 body { margin:0; padding:12px 16px 40px; font:16px/1.5 system-ui,-apple-system,"Hiragino Sans",sans-serif;
        background:#12151a; color:#e8eaed; }
 h1 { font-size:18px; margin:0 0 8px; }
 h2 { font-size:15px; margin:18px 0 6px; color:#c9d1d9; }
 a { color:#58a6ff; }
 .muted { color:#9aa0a6; } .small { font-size:13px; }
 .bar { display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin:8px 0; }
 input[type=text] { font:inherit; color:#e8eaed; background:#1e232b; border:1px solid #3a414d; border-radius:8px;
        padding:8px 10px; min-height:44px; width:100%; max-width:320px; }
 button { font:inherit; color:#e8eaed; background:#2d333b; border:1px solid #3a414d; border-radius:8px;
        padding:8px 14px; min-height:44px; cursor:pointer; }
 button.primary { background:#1f6feb; border-color:#1f6feb; }
 button.ok { background:#238636; border-color:#238636; }
 button.danger { background:#6e3b3b; border-color:#6e3b3b; }
 button:disabled { opacity:.45; cursor:default; }
 .panel { background:#161a20; border:1px solid #2d333b; border-radius:12px; padding:12px 14px; margin:10px 0; }
 .dev { display:flex; gap:10px; align-items:center; padding:8px 0; border-bottom:1px solid #2d333b; }
 .dev:last-child { border-bottom:0; }
 .dev input { width:22px; height:22px; flex:none; }
 .dev .nm { flex:1; min-width:0; overflow-wrap:anywhere; }
 .tag { display:inline-block; font-size:12px; padding:1px 8px; border-radius:999px; margin-left:4px; white-space:nowrap;
        background:#23262b; color:#9aa0a6; }
 .tag.ok { background:#1f3a24; color:#7ee787; } .tag.bad { background:#3a1f1f; color:#ff7b72; }
 .tag.m { background:#1f2f3a; color:#58a6ff; }
 .top { display:flex; flex-wrap:wrap; justify-content:space-between; gap:8px; align-items:center; }
 .levels { display:flex; gap:10px; flex-wrap:wrap; }
 .lv { display:flex; align-items:center; gap:6px; font-size:13px; color:#9aa0a6; }
 .lv .track { width:90px; height:10px; background:#23262b; border-radius:5px; overflow:hidden; }
 .lv .fill { height:100%; background:#3fb950; width:0; transition:width .15s; }
 .lv .fill.hot { background:#e3b341; }
 .stage { text-align:center; padding:28px 8px 18px; }
 .hint { color:#9aa0a6; font-size:14px; letter-spacing:.04em; }
 .phrase { font-size:44px; font-weight:700; line-height:1.25; margin:10px 0 6px; word-break:keep-all;
           overflow-wrap:anywhere; text-wrap:balance; }
 @media (max-width: 480px) { .phrase { font-size:36px; } }
 .note { color:#e3b341; font-size:15px; min-height:22px; }
 .nextp { color:#6e7681; font-size:14px; margin-top:12px; }
 .big { width:100%; min-height:84px; font-size:28px; font-weight:700; border-radius:14px; }
 .row3 { display:grid; grid-template-columns:repeat(3, 1fr); gap:8px; margin-top:10px; }
 .row3 button { min-height:52px; }
 .stats { display:flex; flex-wrap:wrap; gap:12px; font-size:14px; color:#c9d1d9; margin-top:12px; justify-content:center;
        font-variant-numeric:tabular-nums; }
 .warn { color:#e3b341; }
 .err { color:#ff7b72; }
 table { border-collapse:collapse; width:100%; font-size:14px; }
 td, th { padding:6px 8px; border-bottom:1px solid #2d333b; text-align:left; white-space:nowrap; }
 th { color:#9aa0a6; font-weight:400; }
 .tbl { overflow-x:auto; }
 .hidden { display:none !important; }
 #toast { position:fixed; left:50%; bottom:24px; transform:translateX(-50%); background:#b62324; color:#fff;
        padding:10px 18px; border-radius:999px; z-index:20; max-width:90vw; }
</style></head>
<body>
<div id="app">読み込み中…</div>
<div id="toast" class="hidden"></div>
<script>
const S = {st:null, picked:[], speaker:"", round:1, confirmEnd:false, busy:false, timer:null, audio:null, dismissed:null,
           pending:false};
const $ = (id) => document.getElementById(id);
// 文字の欄・ロールダウンを触っている間は画面を作り直さない（作り直すとキーボードやロールダウンが閉じ、打ちかけの文字も
// 消える。店舗 2026-09-30: 読む人の名前が打てなかった）。離れたら作り直す
function editing(){
  const a = document.activeElement, app = $("app");
  if (!a || !app || !app.contains(a)) return false;
  if (a.tagName === "SELECT" || a.tagName === "TEXTAREA") return true;
  return a.tagName === "INPUT" && !["checkbox", "radio", "button", "submit", "range"].includes((a.type || "").toLowerCase());
}
function renderWhenFree(){ if (editing()) S.pending = true; else { S.pending = false; render(); } }
document.addEventListener("focusout", () => setTimeout(() => { if (S.pending && !editing()) renderWhenFree(); }, 0));
function esc(s){ return String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }
function toast(msg){ const t = $("toast"); t.textContent = msg; t.classList.remove("hidden");
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.add("hidden"), 6000); }
async function api(path, body){
  const opts = body === undefined ? {} : {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)};
  const r = await fetch("/api/corpus/" + path, opts);
  let d = null; try { d = await r.json(); } catch (e) {}
  if (!r.ok) throw new Error((d && d.message) || ("HTTP " + r.status));
  return d;
}
try { S.speaker = localStorage.getItem("corpus_speaker") || ""; } catch (e) {}
async function poll(){
  clearTimeout(S.timer);
  try { S.st = await api("state"); renderWhenFree(); } catch (e) { if (!editing()) $("app").innerHTML = `<p class="err">読み込めません: ${esc(e.message)}</p>`; }
  S.timer = setTimeout(poll, S.st && S.st.recording ? 400 : 2000);
}
async function send(verb){
  if (S.busy) return;
  const s = S.st && S.st.session; if (!s) return;
  S.busy = true;
  try { S.st = await api(verb, {index: s.index}); S.confirmEnd = false; render(); }
  catch (e) { toast(e.message); }
  finally { S.busy = false; }
}
async function start(resume){
  const name = (($("speaker") || {}).value || S.speaker).trim();
  if (!resume && !name) { toast("読む人の名前を入れてください"); return; }
  if (!S.picked.length) { toast("マイクを選んでください"); return; }
  S.speaker = name; try { localStorage.setItem("corpus_speaker", name); } catch (e) {}
  try {
    S.st = await api("start", {speaker:name, mics:S.picked, round:S.round, resume:resume || null});
    try { if (navigator.wakeLock) await navigator.wakeLock.request("screen"); } catch (e) {}
    render(); poll();
  } catch (e) { toast(e.message); }
}
async function rescan(){ try { const d = await api("devices"); S.st.devices = d.devices; S.st.device_error = d.device_error; S.st.device_hint = d.device_hint; render(); } catch (e) { toast(e.message); } }
function togglePick(i){
  const k = S.picked.indexOf(i);
  if (k >= 0) S.picked.splice(k, 1); else if (S.picked.length < 2) S.picked.push(i); else toast("マイクは 2 本までです");
  render();
}
function playLast(){
  const l = S.st && S.st.session && S.st.session.last; if (!l || !l.file) return;
  if (S.audio) S.audio.pause();
  S.audio = new Audio("/api/corpus/audio/" + encodeURIComponent(S.st.session.folder) + "/" + encodeURIComponent(l.file));
  S.audio.play().catch(e => toast("再生できません: " + e.message));
}
function asrLine(a){
  if (!a || a.state === "off") return "録りながらの聞き取りはしません";
  const left = a.backlog ? `・残り ${a.backlog} 句（約 ${Math.max(1, Math.round(a.backlog * 6 / 60))} 分）` : "";
  if (a.state === "loading" || a.state === "idle") return "聞き取りのモデルを読み込み中…" + left;
  if (a.state === "unavailable") return "聞き取りは使えません（録音だけします。あとで計算できます）" + (a.message ? `: ${esc(a.message)}` : "");
  return "聞き取り: 録りながら計算します" + (a.backlog ? left : "・待ちなし");
}
function levelBars(s){
  return `<div class="levels">${s.mics.map(m => {
    const w = Math.min(100, Math.round(m.level / 8000 * 100));
    return `<div class="lv"><span>${esc(m.label)}</span><div class="track"><div class="fill ${m.level < 300 ? "" : "hot"}" style="width:${w}%"></div></div>
      ${m.error ? `<span class="err">${esc(m.error)}</span>` : ""}${m.alive ? "" : '<span class="err">止まった</span>'}</div>`;
  }).join("")}</div>`;
}
function renderSetup(st){
  if (!S.picked.length && st.devices) {
    const c = st.devices.find(d => d.configured && d.rate_ok) || st.devices.find(d => d.default && d.rate_ok);
    if (c) S.picked = [c.index];
  }
  const devs = (st.devices || []).map(d => {
    const k = S.picked.indexOf(d.index);
    return `<label class="dev"><input type="checkbox" ${k >= 0 ? "checked" : ""} ${d.rate_ok ? "" : "disabled"} onchange="togglePick(${d.index})">
      <span class="nm">${d.index}  ${esc(d.name)} <span class="muted small">${esc(d.api)}</span>
      ${d.rate_ok ? "" : '<span class="tag bad">16kHz 不可</span>'}${d.configured ? '<span class="tag">いまの設定</span>' : ""}
      ${k >= 0 ? `<span class="tag m">m${k + 1}</span>` : ""}</span></label>`;
  }).join("") || `<p class="err">${esc(st.device_error || "録音できるマイクがありません")}</p>`;
  const recent = st.recent || [];
  const resumable = recent.filter(r => !r.finished && r.done < r.total);
  const when = (f) => `${f.slice(4, 6)}/${f.slice(6, 8)} ${f.slice(9, 11)}:${f.slice(11, 13)}`;
  const rows = recent.map(r => `<tr><td>${esc(when(r.folder))}</td><td>${esc(r.speaker || "")}</td><td>${r.round || ""}</td>
      <td>${r.saved}/${r.total}${r.skipped ? `（飛ばし ${r.skipped}）` : ""}</td>
      <td>${r.transcribed ? (r.match != null ? Math.round(r.match * 100) + "%" : "—") + `（${r.transcribed}）` : "—"}</td></tr>`).join("");
  $("app").innerHTML = `<div class="top"><h1>読み上げ集</h1><a class="small" href="/">← 真のアクション入力</a></div>
    <p class="muted small">画面の言葉を、卓で配るときと同じ声・同じ速さで読んでください。読んだら「次へ」。読み間違えたら「やり直し」、
      前の句を読み直すなら「戻る」。1 周 ${st.total_phrases} 句・15 分ほどです。</p>
    <div class="panel">
      <div class="small muted">読む人</div>
      <input type="text" id="speaker" value="${esc(S.speaker)}" placeholder="例: オーナー / 配り手の名前" oninput="S.speaker=this.value" onchange="S.speaker=this.value">
      <div class="small muted" style="margin-top:12px">マイク（2 本まで。選んだ順に m1・m2）</div>
      ${devs}
      ${st.device_hint ? `<p class="err">${esc(st.device_hint)}</p>` : ""}
      <div class="bar"><button class="sm" onclick="rescan()">マイクを探し直す</button></div>
      <div class="small muted" style="margin-top:8px">周</div>
      <div class="bar">
        <label><input type="radio" name="rnd" ${S.round === 1 ? "checked" : ""} onchange="S.round=1"> 1 周目</label>
        <label><input type="radio" name="rnd" ${S.round === 2 ? "checked" : ""} onchange="S.round=2"> 2 周目（順番が変わります）</label>
      </div>
      <div class="bar" style="margin-top:12px"><button class="primary big" onclick="start()">始める</button></div>
      ${resumable.map(r => `<div class="bar"><button onclick="start('${esc(r.folder)}')">続きから: ${esc(r.speaker || "")}・${r.round} 周目（${r.done}/${r.total}）</button></div>`).join("")}
      <p class="small muted">${asrLine(st.asr)}</p>
    </div>
    ${rows ? `<h2>これまで</h2><div class="tbl"><table><tr><th>日時</th><th>読む人</th><th>周</th><th>句</th><th>一致（m1）</th></tr>${rows}</table></div>` : ""}`;
}
function renderRecording(st){
  const s = st.session, p = s.phrase;
  if (!p) {
    $("app").innerHTML = `<div class="top"><h1>読み上げ集</h1>${levelBars(s)}</div><p>保存しています…</p>`;
    return;
  }
  const endBtns = S.confirmEnd
    ? `<div class="bar" style="justify-content:center"><span class="warn small">いまの句は保存しません。</span>
        <button class="danger" onclick="send('finish')">終わる</button><button onclick="S.confirmEnd=false;render()">続ける</button></div>`
    : `<div class="bar" style="justify-content:center"><button class="sm" onclick="S.confirmEnd=true;render()">ここで終わる</button></div>`;
  const last = s.last ? `<button class="sm" onclick="playLast()">▶ 前の句を聞く（${esc(s.last.text)}）</button>
      ${s.last.quiet ? '<span class="warn small">前の句は声が小さかったかもしれません</span>' : ""}` : "";
  $("app").innerHTML = `<div class="top"><span class="small muted">${esc(s.speaker)}・${s.round} 周目・<b>${s.index + 1}</b> / ${s.total}</span>${levelBars(s)}</div>
    <div class="stage">
      <div class="hint">${esc(p.hint)}</div>
      <div class="phrase">${esc(p.text).replace(/、/g, "、<wbr>")}</div>
      <div class="note">${esc(p.note)}</div>
      ${s.next ? `<div class="nextp">次: ${esc(s.next)}</div>` : '<div class="nextp">これが最後の句です</div>'}
    </div>
    <button class="ok big" onclick="send('next')">次へ</button>
    <div class="row3"><button onclick="send('redo')">やり直し</button><button onclick="send('back')" ${s.index ? "" : "disabled"}>戻る</button><button onclick="send('skip')">飛ばす</button></div>
    <div class="stats"><span>この句 ${s.take_sec.toFixed(1)} 秒</span><span>保存 ${s.saved}</span>
      ${s.skipped ? `<span>飛ばした ${s.skipped}</span>` : ""}${s.quiet ? `<span class="warn">声が小さい ${s.quiet}</span>` : ""}
      <span>${st.asr && st.asr.backlog ? `聞き取り待ち ${st.asr.backlog}` : ""}</span></div>
    <div class="bar" style="justify-content:center;margin-top:10px">${last}</div>
    ${endBtns}`;
}
function renderDone(st){
  const s = st.session;
  const a = st.asr || {};
  $("app").innerHTML = `<div class="top"><h1>読み上げ集</h1><a class="small" href="/">← 真のアクション入力</a></div>
    <div class="panel">
      <p><b>${s.reading_done ? "読み終わりました" : "ここで終わりました"}</b>（保存 ${s.saved}${s.skipped ? `・飛ばした ${s.skipped}` : ""}${s.quiet ? `・声が小さかった ${s.quiet}` : ""}）</p>
      <p class="${a.backlog ? "warn" : "muted"} small">${a.backlog
        ? `聞き取りの計算が残っています（${a.backlog} 句・約 ${Math.max(1, Math.round(a.backlog * 6 / 60))} 分）。0 になってから次のテスト（ハンドロガー）を始めてください（同時に動かすと聞き取りが遅れます）。`
        : asrLine(a)}</p>
      <div class="bar"><button class="primary" onclick="S.dismissed='${esc(s.folder)}';S.confirmEnd=false;render()">別の人・次の周を始める</button></div>
    </div>`;
}
function render(){
  const st = S.st; if (!st) return;
  if (st.recording && st.session) renderRecording(st);
  else if (st.session && st.session.finished && S.dismissed !== st.session.folder) renderDone(st);
  else renderSetup(st);
}
document.addEventListener("keydown", (e) => {
  if (!S.st || !S.st.recording || e.target.tagName === "INPUT") return;
  if (e.key === " " || e.key === "Enter") { e.preventDefault(); send("next"); }
  else if (e.key === "r") send("redo");
  else if (e.key === "b") send("back");
});
poll();
</script>
</body></html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
