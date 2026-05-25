"""core/tournament_timer.py

Tournament の blind level 構造と時計を扱う pure ロジック。

- ``BlindLevel``: 1 つの level (通常 / break 共通)
- ``TournamentStructure``: 全 level + メタ情報 (starting_stack, late_reg ...)
- ``TournamentTimer``: 現在 level の進行を deadline ベースで追う state machine

副作用は ``on_level_changed`` callback 1 本のみ。GUI / IntegrationThread に
依存しない (= 単体テスト容易)。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import logging

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BlindLevel:
    """1 つの blind level (通常 level または break)。

    - 通常 level: ``is_break=False``、``level`` / ``sb`` / ``bb`` 必須
    - break:      ``is_break=True``、``level`` / ``sb`` / ``bb`` は None でも可
    """

    level: Optional[int]
    label: str
    sb: Optional[int]
    bb: Optional[int]
    ante: int
    duration_sec: int
    is_break: bool


@dataclass
class TournamentStructure:
    name: str
    starting_stack: int
    addon_chips: int
    # この level の **終了時点** で late reg close (= 次 level 開始の瞬間)。
    # 0 / 負数なら late reg 無し扱いで ``time_to_late_reg_close_sec`` は常に None。
    late_reg_closes_after_level: int
    levels: list[BlindLevel]


# ───────────────────── loader ─────────────────────


def _parse_level(idx: int, raw: dict) -> BlindLevel:
    is_break = bool(raw.get("is_break", False))
    duration = raw.get("duration_sec")
    if not isinstance(duration, int) or duration <= 0:
        raise ValueError(
            f"levels[{idx}]: duration_sec must be positive int, got {duration!r}"
        )

    if is_break:
        label = str(raw.get("label", "Break"))
        return BlindLevel(
            level=raw.get("level"),  # break でも明示してあれば保持
            label=label,
            sb=raw.get("sb"),
            bb=raw.get("bb"),
            ante=int(raw.get("ante", 0)),
            duration_sec=duration,
            is_break=True,
        )

    level = raw.get("level")
    sb = raw.get("sb")
    bb = raw.get("bb")
    if not isinstance(level, int) or level <= 0:
        raise ValueError(f"levels[{idx}]: level must be positive int, got {level!r}")
    if not isinstance(sb, int) or sb <= 0:
        raise ValueError(f"levels[{idx}]: sb must be positive int, got {sb!r}")
    if not isinstance(bb, int) or bb <= 0:
        raise ValueError(f"levels[{idx}]: bb must be positive int, got {bb!r}")
    if sb >= bb:
        raise ValueError(f"levels[{idx}]: sb ({sb}) must be < bb ({bb})")

    label = str(raw.get("label", f"Level {level}"))
    return BlindLevel(
        level=level,
        label=label,
        sb=sb,
        bb=bb,
        ante=int(raw.get("ante", 0)),
        duration_sec=duration,
        is_break=False,
    )


def load_structure_from_file(path: Path) -> TournamentStructure:
    """``tournament_structure.json`` を読んで ``TournamentStructure`` を返す。

    不正なら ``ValueError`` を上げる (呼び出し側で warning ログ + timer OFF
    フォールバックする想定)。
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    raw_levels = data.get("levels")
    if not isinstance(raw_levels, list) or not raw_levels:
        raise ValueError("levels must be a non-empty list")
    levels = [_parse_level(i, lv) for i, lv in enumerate(raw_levels)]

    starting_stack = data.get("starting_stack", 0)
    if not isinstance(starting_stack, int) or starting_stack <= 0:
        raise ValueError(f"starting_stack must be positive int, got {starting_stack!r}")
    addon_chips = data.get("addon_chips", 0)
    if not isinstance(addon_chips, int) or addon_chips < 0:
        raise ValueError(f"addon_chips must be non-negative int, got {addon_chips!r}")

    late_reg = data.get("late_reg_closes_after_level", 0)
    if not isinstance(late_reg, int):
        raise ValueError(
            f"late_reg_closes_after_level must be int, got {late_reg!r}"
        )

    return TournamentStructure(
        name=str(data.get("name", "Tournament")),
        starting_stack=starting_stack,
        addon_chips=addon_chips,
        late_reg_closes_after_level=late_reg,
        levels=levels,
    )


# ───────────────────── timer ─────────────────────


class TournamentTimer:
    """deadline ベースで進行する level state machine。

    状態:
      - 未開始 (``_started=False``): 残り時間は ``levels[0].duration_sec`` 固定
      - 進行中 (``_paused=False``):  ``_level_deadline`` までカウントダウン
      - 一時停止 (``_paused=True``): ``_paused_remaining`` を保持

    ``tick(now)`` は idempotent。GUI の 100ms poll から繰り返し呼んで良い。
    """

    def __init__(
        self,
        structure: TournamentStructure,
        *,
        on_level_changed: Optional[Callable[[BlindLevel], None]] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not structure.levels:
            raise ValueError("structure must have at least one level")
        self._structure = structure
        self._on_level_changed = on_level_changed
        self._clock = clock

        self._level_index: int = 0
        self._started: bool = False
        self._paused: bool = False
        # _level_deadline: clock() 時刻基準。is_running 中のみ有効
        self._level_deadline: float = 0.0
        # _paused_remaining: pause 中だけ有効。resume で deadline を再構築
        self._paused_remaining: float = float(structure.levels[0].duration_sec)

    # ───── state queries ─────

    @property
    def structure(self) -> TournamentStructure:
        return self._structure

    def current_level(self) -> BlindLevel:
        return self._structure.levels[self._level_index]

    def next_level(self) -> Optional[BlindLevel]:
        nxt = self._level_index + 1
        if nxt >= len(self._structure.levels):
            return None
        return self._structure.levels[nxt]

    def remaining_sec(self) -> float:
        if not self._started:
            return float(self.current_level().duration_sec)
        if self._paused:
            return max(0.0, self._paused_remaining)
        return max(0.0, self._level_deadline - self._clock())

    def is_running(self) -> bool:
        return self._started and not self._paused

    def is_paused(self) -> bool:
        return self._started and self._paused

    def is_finished(self) -> bool:
        """最終 level で残り時間 0 に到達済みか (= 以降 tick は no-op)。"""
        last = self._level_index == len(self._structure.levels) - 1
        return last and self._started and self.remaining_sec() <= 0.0

    # ───── derived time queries ─────

    def time_to_late_reg_close_sec(self) -> Optional[float]:
        """``late_reg_closes_after_level`` の **終了時刻** までの残り秒。

        間に挟まる break level の duration も含めて加算する。当該 level を
        既に過ぎていれば None。0 以下なら 0.0 で返す。
        """
        target_level = self._structure.late_reg_closes_after_level
        if target_level <= 0:
            return None
        # 「target_level の終了」= target_level (通常 level) を最後に含む levels の
        # duration 合計から、現在経過した分を引く。
        target_idx: Optional[int] = None
        for i, lv in enumerate(self._structure.levels):
            if not lv.is_break and lv.level == target_level:
                target_idx = i
                break
        if target_idx is None:
            return None
        # 現在地が target_idx より既に先 → 過去 → None
        if self._level_index > target_idx:
            return None
        # 残り = (今 level の残り) + (current_index+1 .. target_idx の duration 合計)
        total = self.remaining_sec()
        for i in range(self._level_index + 1, target_idx + 1):
            total += float(self._structure.levels[i].duration_sec)
        return max(0.0, total)

    def time_to_next_break_sec(self) -> Optional[float]:
        """次の break level の **開始時刻** までの残り秒。

        現在 break 中なら 0.0 を返し (= now is the break)、以降 break が無ければ None。
        """
        # 現在 break 中
        if self.current_level().is_break:
            return 0.0
        # 次の break index を探す
        break_idx: Optional[int] = None
        for i in range(self._level_index + 1, len(self._structure.levels)):
            if self._structure.levels[i].is_break:
                break_idx = i
                break
        if break_idx is None:
            return None
        total = self.remaining_sec()
        for i in range(self._level_index + 1, break_idx):
            total += float(self._structure.levels[i].duration_sec)
        return max(0.0, total)

    # ───── commands ─────

    def start(self) -> None:
        """未開始なら level 0 を開始。既に開始済みなら no-op。"""
        if self._started:
            return
        self._started = True
        self._paused = False
        self._level_deadline = self._clock() + float(self.current_level().duration_sec)

    def pause(self) -> None:
        if not self._started or self._paused:
            return
        self._paused_remaining = max(0.0, self._level_deadline - self._clock())
        self._paused = True

    def resume(self) -> None:
        if not self._started or not self._paused:
            return
        self._level_deadline = self._clock() + self._paused_remaining
        self._paused = False

    def advance_level(self) -> None:
        """次 level に即時遷移。GUI から呼ぶ場合は confirmation 越しに使うこと。

        - 最終 level に居る場合は no-op
        - 未開始なら先に start() 扱いにし、その後 level 1 に進む
        """
        if self._level_index >= len(self._structure.levels) - 1:
            return
        if not self._started:
            self.start()
        self._goto_level(self._level_index + 1)

    def tick(self, now: Optional[float] = None) -> None:
        """deadline 越えなら次 level に進める。multi-level 跨ぎにも対応。

        idempotent: 同じ deadline を何度跨いでも callback は 1 level につき 1 回。
        """
        if not self._started or self._paused:
            return
        cur = self._clock() if now is None else float(now)
        while self._level_index < len(self._structure.levels) - 1 and cur >= self._level_deadline:
            self._goto_level(self._level_index + 1, base_time=self._level_deadline)
        # 最終 level で deadline 越えても callback は出さない (居座る)

    # ───── internal ─────

    def _goto_level(self, new_index: int, *, base_time: Optional[float] = None) -> None:
        self._level_index = new_index
        new_level = self._structure.levels[new_index]
        start = self._clock() if base_time is None else base_time
        self._level_deadline = start + float(new_level.duration_sec)
        self._paused_remaining = float(new_level.duration_sec)
        # 通常 level (sb/bb 揃い) のときのみ callback
        if (
            self._on_level_changed is not None
            and not new_level.is_break
            and new_level.sb is not None
            and new_level.bb is not None
        ):
            try:
                self._on_level_changed(new_level)
            except Exception as e:  # noqa: BLE001
                logger.warning("on_level_changed callback raised: %s", e)
