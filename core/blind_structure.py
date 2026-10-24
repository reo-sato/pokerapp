"""core/blind_structure.py

トーナメントのストラクチャー（ブラインドのレベルの並び）と時計（ADR-0071）。

- ``BlindLevel``: 1 つのレベル（通常のレベル / ブレイク）。アンテは **BB アンテで額は BB と別**（0 = 無し, オーナー
  2026-10-24。全員アンテは持たない）。
- ``BlindStructure``: 名前つきのレベルの並び。レベルの番号はブレイクを除いて 1 から振る。
- ``BlindStructureStore``: リポジトリ直下の ``blind_structures.json``（名前つきで複数, 店が編集・更新で保持）の読み書き。
  無ければコミット済みのサンプル ``blind_structures_default.json`` を読む（``config_default.json`` と同じ形）。``core/menu.py``
  と同じ形（ロック・変われば読み直す・検証・atomic な書き込み）。
- ``start_time``（任意, "HH:MM"）: レベル 1 を始める時刻。あれば時計は **時刻準拠**（その時刻からの経過でいまのレベルを
  決める = 起動が遅れても・起動し直しても、壁の時計どおりに上がる, オーナー 2026-10-24）。
- ``TournamentClock``: 締め切り方式で進む時計。時刻は呼ぶ側が渡す（ロガーの ``IntegrationThread`` の時計 = epoch 秒）。
  副作用を持たない（ブラインドを変えるのはロガー）。

5 月の未マージのブランチ（``claude/add-tournament-timer-n5OGx`` の ``core/tournament_timer.py``）のモデルを移した。
"""
from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from typing import Optional

from core.atomic_io import atomic_write_json

logger = logging.getLogger(__name__)

_DEFAULT_FILE = Path(__file__).parent.parent / "blind_structures.json"
_SAMPLE_FILE = Path(__file__).parent.parent / "blind_structures_default.json"

_MAX_NAME = 60
_MAX_LEVELS = 100
_MAX_MINUTES = 600
_START_TIME_RE = re.compile(r"^\s*(\d{1,2})\s*[:：]\s*(\d{2})\s*$")
# 開始時刻がこれより先なら前の日の時刻とみなす（日付をまたいで続くトーナメント）
_START_FUTURE_MAX_SEC = 12 * 3600


class StructureValidationError(ValueError):
    """ストラクチャーの検証の違反（error code: invalid_structure）。"""


@dataclass(frozen=True)
class BlindLevel:
    """1 つのレベル。ブレイクは ``is_break=True``（sb / bb / ante は 0）。``level`` はブレイクを除いた番号。"""

    level: Optional[int]
    sb: int
    bb: int
    ante: int
    minutes: int
    is_break: bool = False

    @property
    def duration_sec(self) -> int:
        return self.minutes * 60

    @property
    def label(self) -> str:
        return "ブレイク" if self.is_break else f"レベル {self.level}"

    def to_dict(self) -> dict:
        if self.is_break:
            return {"break": True, "minutes": self.minutes}
        return {"sb": self.sb, "bb": self.bb, "ante": self.ante, "minutes": self.minutes}


@dataclass(frozen=True)
class BlindStructure:
    name: str
    levels: tuple[BlindLevel, ...]
    starting_stack: Optional[int] = None
    start_time: Optional[str] = None       # "HH:MM" = レベル 1 を始める時刻（時刻準拠の時計）

    def to_dict(self) -> dict:
        out: dict = {"name": self.name, "levels": [lv.to_dict() for lv in self.levels]}
        if self.starting_stack:
            out["starting_stack"] = self.starting_stack
        if self.start_time:
            out["start_time"] = self.start_time
        return out

    def playing_index(self, level: int) -> Optional[int]:
        """レベルの番号（ブレイクを除く 1 から）→ 並びの位置。無ければ None。"""
        for i, lv in enumerate(self.levels):
            if not lv.is_break and lv.level == level:
                return i
        return None

    def blinds_at(self, index: int) -> BlindLevel:
        """並びの位置 ``index`` で使うブラインド: ブレイクはその前の通常のレベル（無ければ次）。"""
        for i in list(range(index, -1, -1)) + list(range(index + 1, len(self.levels))):
            if not self.levels[i].is_break:
                return self.levels[i]
        raise StructureValidationError("通常のレベルがありません")


def parse_start_time(value: object) -> Optional[str]:
    """開始時刻（"18:30" / "18：30" / "9:05"）を "HH:MM" にする。空は None。違反は ``StructureValidationError``。"""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    m = _START_TIME_RE.match(str(value))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise StructureValidationError(f"開始時刻は 18:30 のように時:分で入れてください（{value!r}）")
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def start_epoch(start_time: str, now: float, tz: Optional[tzinfo] = None) -> float:
    """開始時刻 "HH:MM" の、``now`` に近い回の epoch 秒（既定は PC の時間帯）。

    今日のその時刻。ただし 12 時間より先なら前の日（18:30 開始で日付をまたいだ 1:00 に起動 = 昨日の 18:30）。
    """
    hh, mm = (int(x) for x in (parse_start_time(start_time) or "").split(":"))
    base = datetime.fromtimestamp(now, tz) if tz is not None else datetime.fromtimestamp(now)
    at = base.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if at.timestamp() - now > _START_FUTURE_MAX_SEC:
        at -= timedelta(days=1)
    return at.timestamp()


def _int(raw: dict, key: str, where: str, *, minimum: int) -> int:
    value = raw.get(key, 0)
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise StructureValidationError(f"{where}: {key} は {minimum} 以上の整数にしてください（{value!r}）")
    return value


def parse_structure(raw: dict) -> BlindStructure:
    """保存の形（``to_dict``）を検証して ``BlindStructure`` にする。違反は ``StructureValidationError``。

    - 名前: 前後の空白を除いて 1〜60 文字。
    - レベル: 1〜100 行。通常のレベルは SB ≥ 1・SB ≤ BB・アンテ ≥ 0、時間は 1〜600 分。ブレイクは時間だけ。
    - 通常のレベルが 1 つ以上・BB は前のレベル以上（下がるストラクチャーは打ち間違い）。
    - 開始時刻（任意）: "HH:MM"。
    """
    if not isinstance(raw, dict):
        raise StructureValidationError("ストラクチャーは object にしてください")
    name = str(raw.get("name") or "").strip()
    if not name or len(name) > _MAX_NAME:
        raise StructureValidationError(f"名前は 1〜{_MAX_NAME} 文字にしてください")
    raw_levels = raw.get("levels")
    if not isinstance(raw_levels, list) or not raw_levels:
        raise StructureValidationError("レベルを 1 行以上入れてください")
    if len(raw_levels) > _MAX_LEVELS:
        raise StructureValidationError(f"レベルは {_MAX_LEVELS} 行までです")
    levels: list[BlindLevel] = []
    number = 0
    prev_bb = 0
    for i, lv in enumerate(raw_levels, start=1):
        if not isinstance(lv, dict):
            raise StructureValidationError(f"{i} 行目: object にしてください")
        where = f"{i} 行目"
        minutes = _int(lv, "minutes", where, minimum=1)
        if minutes > _MAX_MINUTES:
            raise StructureValidationError(f"{where}: 時間は {_MAX_MINUTES} 分までです")
        if lv.get("break") is True or lv.get("is_break") is True:
            levels.append(BlindLevel(level=None, sb=0, bb=0, ante=0, minutes=minutes, is_break=True))
            continue
        sb = _int(lv, "sb", where, minimum=1)
        bb = _int(lv, "bb", where, minimum=1)
        ante = _int(lv, "ante", where, minimum=0)
        if sb > bb:
            raise StructureValidationError(f"{where}: SB（{sb}）は BB（{bb}）以下にしてください")
        if bb < prev_bb:
            raise StructureValidationError(f"{where}: BB（{bb}）が前のレベル（{prev_bb}）より小さくなっています")
        number += 1
        prev_bb = bb
        levels.append(BlindLevel(level=number, sb=sb, bb=bb, ante=ante, minutes=minutes))
    if number == 0:
        raise StructureValidationError("通常のレベル（ブレイク以外）を 1 行以上入れてください")
    stack = raw.get("starting_stack")
    if stack in (None, "", 0):
        stack = None
    elif not isinstance(stack, int) or isinstance(stack, bool) or stack < 1:
        raise StructureValidationError(f"始めの持ち点は 1 以上の整数にしてください（{stack!r}）")
    return BlindStructure(name=name, levels=tuple(levels), starting_stack=stack,
                          start_time=parse_start_time(raw.get("start_time")))


class BlindStructureStore:
    """``blind_structures.json``（``{"structures": [...]}``）の読み書き（thread-safe / 変われば読み直す）。

    ファイルが無ければ ``sample``（既定はコミット済みのサンプル）を読む。保存は ``path`` に書く。壊れていれば空として
    続ける（壊れた個々のストラクチャーは飛ばして WARN）。
    """

    def __init__(self, path: str | Path | None = None, *, sample: str | Path | None = None) -> None:
        self._path = Path(path) if path is not None else _DEFAULT_FILE
        if sample is not None:
            self._sample: Optional[Path] = Path(sample)
        else:
            self._sample = _SAMPLE_FILE if path is None else None
        self._lock = threading.RLock()
        self._items: list[BlindStructure] = []
        self._loaded_mtime: Optional[float] = None
        self._load()

    @property
    def path(self) -> Path:
        return self._path

    def _source(self) -> Optional[Path]:
        if self._path.exists():
            return self._path
        return self._sample if self._sample is not None and self._sample.exists() else None

    def _load(self) -> None:
        self._items = []
        source = self._source()
        if source is None:
            self._loaded_mtime = None
            return
        try:
            mtime = self._path.stat().st_mtime if source == self._path else None
            data = json.loads(source.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("ストラクチャーのファイルを読めません（%s）— 空として続けます", e)
            return
        seen: set[str] = set()
        for raw in (data or {}).get("structures") or []:
            try:
                structure = parse_structure(raw)
            except StructureValidationError as e:
                logger.warning("ストラクチャーを飛ばします（%s）: %r", e, (raw or {}).get("name"))
                continue
            if structure.name in seen:
                logger.warning("同じ名前のストラクチャーを飛ばします: %s", structure.name)
                continue
            seen.add(structure.name)
            self._items.append(structure)
        self._loaded_mtime = mtime

    def _maybe_reload(self) -> None:
        try:
            mtime = self._path.stat().st_mtime if self._path.exists() else None
        except OSError:
            return
        if mtime != self._loaded_mtime:
            self._load()

    def list_structures(self) -> list[BlindStructure]:
        with self._lock:
            self._maybe_reload()
            return list(self._items)

    def get(self, name: str) -> Optional[BlindStructure]:
        with self._lock:
            self._maybe_reload()
            return next((s for s in self._items if s.name == name), None)

    def save(self, raw: dict, *, previous_name: Optional[str] = None) -> BlindStructure:
        """1 つを保存する（同じ名前は置き換え。``previous_name`` = 名前を変えたときの前の名前）。"""
        structure = parse_structure(raw)
        with self._lock:
            self._maybe_reload()
            items = [s for s in self._items if s.name not in (structure.name, previous_name)]
            at = next((i for i, s in enumerate(self._items) if s.name in (structure.name, previous_name)), len(items))
            items.insert(min(at, len(items)), structure)
            self._write(items)
            return structure

    def delete(self, name: str) -> bool:
        with self._lock:
            self._maybe_reload()
            items = [s for s in self._items if s.name != name]
            if len(items) == len(self._items):
                return False
            self._write(items)
            return True

    def _write(self, items: list[BlindStructure]) -> None:
        atomic_write_json(self._path, {"structures": [s.to_dict() for s in items]})
        self._items = items
        try:
            self._loaded_mtime = self._path.stat().st_mtime
        except OSError:
            self._loaded_mtime = None
        logger.info("ストラクチャーを保存しました: %d 件（%s）", len(items), self._path)


class TournamentClock:
    """ストラクチャーに沿って進む時計（締め切り方式）。

    状態は「いまの並びの位置」「そのレベルを始めた時刻（一時停止中は経過秒）」だけ。時刻は呼ぶ側が渡す。
    ``tick(now)`` は何度呼んでもよい（締め切りを過ぎた分だけ進み、複数のレベルをまたげる）。最後のレベルでは止まる。
    """

    def __init__(self, structure: BlindStructure, *, index: int = 0, started_at: float = 0.0,
                 paused_elapsed: Optional[float] = None) -> None:
        if not structure.levels:
            raise StructureValidationError("レベルがありません")
        if not 0 <= index < len(structure.levels):
            raise StructureValidationError(f"レベルの位置が範囲外です: {index}")
        self.structure = structure
        self.index = index
        self.level_started_at = float(started_at)
        # 一時停止中は、そのレベルの経過秒（None = 動いている）
        self.paused_elapsed: Optional[float] = None if paused_elapsed is None else float(paused_elapsed)

    @classmethod
    def start(cls, structure: BlindStructure, now: float, *, level: int = 1,
              elapsed_sec: float = 0.0) -> "TournamentClock":
        """レベルの番号 ``level``（ブレイクを除く 1 から）を、``elapsed_sec`` 経過した状態で始める（途中から再開）。

        経過がそのレベルの時間を越えていれば次へ進めた状態で返す。
        """
        index = structure.playing_index(level)
        if index is None:
            raise StructureValidationError(f"レベル {level} はありません")
        clock = cls(structure, index=index, started_at=now - max(0.0, float(elapsed_sec)))
        clock.tick(now)
        return clock

    @classmethod
    def start_at(cls, structure: BlindStructure, start: float, now: float) -> "TournamentClock":
        """時刻準拠: レベル 1 を時刻 ``start``（epoch 秒）に始めた時計を ``now`` まで進めて返す。

        始まる前（``now < start``）はレベル 1 のまま、始まる時刻を待つ（``status`` の ``starts_at``）。
        """
        clock = cls(structure, index=0, started_at=start)
        clock.tick(now)
        return clock

    # ――― 状態 ―――

    @property
    def current(self) -> BlindLevel:
        return self.structure.levels[self.index]

    @property
    def blinds(self) -> BlindLevel:
        """いま使うブラインド（ブレイク中はその前の通常のレベル）。"""
        return self.structure.blinds_at(self.index)

    @property
    def paused(self) -> bool:
        return self.paused_elapsed is not None

    @property
    def is_last(self) -> bool:
        return self.index >= len(self.structure.levels) - 1

    def elapsed(self, now: float) -> float:
        if self.paused_elapsed is not None:
            return self.paused_elapsed
        return max(0.0, now - self.level_started_at)

    def remaining(self, now: float) -> float:
        return max(0.0, self.current.duration_sec - self.elapsed(now))

    def ends_at(self) -> Optional[float]:
        """いまのレベルが終わる時刻（一時停止中は None）。"""
        if self.paused_elapsed is not None:
            return None
        return self.level_started_at + self.current.duration_sec

    def next_level(self) -> Optional[BlindLevel]:
        """次の通常のレベル（ブレイクは飛ばす）。無ければ None。"""
        return next((lv for lv in self.structure.levels[self.index + 1:] if not lv.is_break), None)

    def seconds_to_next_break(self, now: float) -> Optional[float]:
        """次のブレイクが始まるまでの秒（ブレイク中は 0、この先に無ければ None）。"""
        if self.current.is_break:
            return 0.0
        total = self.remaining(now)
        for lv in self.structure.levels[self.index + 1:]:
            if lv.is_break:
                return total
            total += lv.duration_sec
        return None

    def next_break_at(self) -> Optional[float]:
        """次のブレイクが始まる時刻（ブレイク中・一時停止中・この先に無ければ None）。絶対時刻なので呼ぶたびに同じ値。"""
        if self.paused_elapsed is not None or self.current.is_break:
            return None
        at = self.level_started_at + self.current.duration_sec
        for lv in self.structure.levels[self.index + 1:]:
            if lv.is_break:
                return at
            at += lv.duration_sec
        return None

    # ――― 操作 ―――

    def tick(self, now: float) -> bool:
        """締め切りを過ぎていれば次のレベルへ（複数またぐ）。進めたら True。"""
        if self.paused_elapsed is not None:
            return False
        moved = False
        while not self.is_last and now >= self.level_started_at + self.current.duration_sec:
            self.level_started_at += self.current.duration_sec
            self.index += 1
            moved = True
        return moved

    def pause(self, now: float) -> bool:
        if self.paused_elapsed is not None:
            return False
        self.tick(now)
        self.paused_elapsed = min(self.elapsed(now), float(self.current.duration_sec))
        return True

    def resume(self, now: float) -> bool:
        if self.paused_elapsed is None:
            return False
        self.level_started_at = now - self.paused_elapsed
        self.paused_elapsed = None
        return True

    def goto(self, index: int, now: float, *, elapsed_sec: float = 0.0) -> None:
        """並びの位置 ``index`` へ移す（手で次・前・指定）。一時停止中なら一時停止のまま。"""
        if not 0 <= index < len(self.structure.levels):
            raise StructureValidationError(f"レベルの位置が範囲外です: {index}")
        self.index = index
        elapsed = max(0.0, float(elapsed_sec))
        if self.paused_elapsed is not None:
            self.paused_elapsed = min(elapsed, float(self.current.duration_sec))
        else:
            self.level_started_at = now - elapsed

    # ――― 保存 ―――

    def to_dict(self) -> dict:
        return {
            "structure": self.structure.to_dict(),
            "index": self.index,
            "level_started_at": self.level_started_at,
            "paused_elapsed": self.paused_elapsed,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TournamentClock":
        return cls(parse_structure(data["structure"]), index=int(data["index"]),
                   started_at=float(data["level_started_at"]), paused_elapsed=data.get("paused_elapsed"))

    def status(self, now: float) -> dict:
        """表示用（卓モニタ・設定画面・CLI の ``clock``）。時刻は絶対時刻で持ち、残りは読む側が数える。"""
        cur = self.current
        blinds = self.blinds
        nxt = self.next_level()
        return {
            "structure": self.structure.name,
            "index": self.index,
            "level": blinds.level,
            "label": cur.label,
            "is_break": cur.is_break,
            "sb": blinds.sb, "bb": blinds.bb, "ante": blinds.ante,
            "minutes": cur.minutes,
            "ends_at": self.ends_at(),
            "paused": self.paused,
            "remaining_sec": round(self.remaining(now), 1) if self.paused else None,
            "next": ({"level": nxt.level, "sb": nxt.sb, "bb": nxt.bb, "ante": nxt.ante} if nxt is not None else None),
            "next_break_at": self.next_break_at(),
            "last": self.is_last,
            # 時刻準拠でまだ始まっていない（レベル 1 を始める時刻）
            "starts_at": (self.level_started_at if self.index == 0 and not self.paused
                          and now < self.level_started_at else None),
        }


def format_blinds(sb: int, bb: int, ante: int) -> str:
    """「200/400（アンテ 400）」の表示。"""
    return f"{sb}/{bb}" + (f"（アンテ {ante}）" if ante else "")


def format_remaining(sec: float) -> str:
    sec = max(0, int(sec))
    return f"{sec // 60}:{sec % 60:02d}"
