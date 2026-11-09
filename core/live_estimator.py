"""core/live_estimator.py — プレー中にハンドごとに推定する（ADR-0072 段階 1, 2026-11-09）

店舗サーバー（`--serve`）の中の常駐スレッド。いまのセッションの記録（`logs/<sid>.json`）を 5 秒ごとに見て、
推定できるようになったハンドを 1 つずつ推す。推した結果は `logs/<sid>.estimate.json` に書き足し、読む側
（お客さん・スタッフの画面・クラウドの同期）は今までどおりそれをライブの記録に重ねる（`core/hand_estimate.py`）。

- **いつ推すか**: ハンド N の窓（推定器が読む発話と札）は次のハンドの始まりで閉じる。次のハンドが始まったら推す。
  次のハンドが無いまま確定から `IDLE_SEC` たったときも推す（休憩・セッションの終わり）。
- **推し直す**: そのハンドの窓の中の書き起こしの行・events の数、窓が閉じたかどうか（`hand_marker`）が、推した
  ときと変わったら推し直す（API につながらず後で書き起こした発話・あとから始まった次のハンド・セッションの後の
  ハンドの間の発話）。1 ハンド `MAX_RUNS` 回まで。失敗したハンドは、入力が変わるまで推し直さない（推定は決定的）。
- **重くしない**: 推定は子プロセス（`tools/estimate_logs.py --hands N --write`）で、低い優先度で 1 本ずつ動かす。
  会計の API や卓モニタの応答を邪魔しない。落ちても店舗サーバーは止めない。

`hand_marker` は `tools/estimate_logs.py` も使う（推したときの目印 `inputs` を推定のファイルに残す）。両方が同じ
関数・同じファイルから数えるので、入力が変わらなければ目印は一致する。
"""
from __future__ import annotations

import bisect
import json
import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from core.hand_estimate import ESTIMATE_SUFFIX

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
RECHECK_SEC = 5.0
IDLE_SEC = 60.0               # 次のハンドが無いまま確定からこの秒数たったら推す
MAX_RUNS = 3                  # 1 ハンドを推す回数の上限（入力が変わり続けても CPU を使い続けない）
RUN_TIMEOUT_SEC = 900.0       # 重いハンド（11/05 のハンド 4 など）でも打ち切らない長さ
ACTIVE_SEC = 12 * 3600.0      # events を最後に書いてからこの秒数のうちは「いまのセッション」
ROW_BEFORE_SEC = 0.5          # 推定器の窓（`SessionEstimator._window_inputs`）と同じ幅
EVENT_BEFORE_SEC = 2.0

SHADOW_SUFFIX = ".estimate.shadow.json"     # `tools/estimate_logs.py` と同じ


def _epoch(iso: Optional[str]) -> Optional[float]:
    try:
        return datetime.fromisoformat(iso).timestamp() if iso else None
    except (TypeError, ValueError):
        return None


# ――― 窓の中の入力の目印 ―――

@dataclass
class Marks:
    """セッションの書き起こしの行（発話の始まり）と events の時刻（どちらも昇順）。"""
    rows: list[float] = field(default_factory=list)
    events: list[float] = field(default_factory=list)


def _times(path: Path, key: str) -> list[float]:
    out = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        value = _time_of(line, key)
        if value is not None:
            out.append(value)
    return out


def _time_of(line: str, key: str) -> Optional[float]:
    line = line.strip().strip("\x00")
    if not line:
        return None
    try:
        value = json.loads(line).get(key)
    except (ValueError, AttributeError):
        return None                      # 電源断の読めない末尾の行
    return float(value) if isinstance(value, (int, float)) else None


def read_marks(folder: Path, session_id: str) -> Marks:
    """ファイルから全部読む（`tools/estimate_logs.py` が推したときの目印に）。"""
    rows = _times(folder / f"{session_id}.transcripts.jsonl", "utterance_start_ts")
    rows += _times(folder / f"{session_id}.offplay.jsonl", "utterance_start_ts")
    return Marks(sorted(rows), sorted(_times(folder / f"{session_id}.events.jsonl", "timestamp")))


def next_starts(hands: list[dict], live_hand: Optional[dict]) -> dict:
    """ハンドごとの次のハンドの始まり（epoch。まだ無ければ None）。進行中のハンド（`live_hand.json`）も数える。"""
    starts = sorted((t, h.get("hand_id")) for h in hands if (t := _epoch(h.get("started_at"))) is not None)
    live = _epoch((live_hand or {}).get("started_at"))
    out = {}
    for i, (t, hid) in enumerate(starts):
        nxt = starts[i + 1][0] if i + 1 < len(starts) else None
        if nxt is None and live is not None and live > t:
            nxt = live
        out[hid] = nxt
    return out


def hand_marker(started: float, next_start: Optional[float], marks: Marks) -> dict:
    """ハンドの窓の中の書き起こしの行と events の数・窓が閉じたか。変われば推し直す。"""
    end = next_start if next_start is not None else float("inf")
    rows = bisect.bisect_left(marks.rows, end) - bisect.bisect_left(marks.rows, started - ROW_BEFORE_SEC)
    events = bisect.bisect_left(marks.events, end) - bisect.bisect_left(marks.events, started - EVENT_BEFORE_SEC)
    return {"rows": rows, "events": events, "closed": next_start is not None}


def read_json(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None                      # 無い・書き換えの途中（次の見回りで読む）
    return data if isinstance(data, dict) else None


def session_markers(folder: Path, session_id: str, marks: Optional[Marks] = None) -> dict:
    """記録のハンドごとの目印（キーは hand_id）。"""
    record = read_json(folder / f"{session_id}.json") or {}
    hands = [h for h in record.get("hands") or [] if isinstance(h, dict)]
    live = (read_json(folder / f"{session_id}.live_hand.json") or {}).get("hand")
    marks = marks if marks is not None else read_marks(folder, session_id)
    nexts = next_starts(hands, live)
    return {h.get("hand_id"): hand_marker(_epoch(h["started_at"]), nexts.get(h.get("hand_id")), marks)
            for h in hands if _epoch(h.get("started_at")) is not None}


# ――― 足された行だけ読む ―――

class _Tail:
    """追記だけのファイル（jsonl）から、足された行の時刻だけを読む（見回りのたびに全部読み直さない）。"""

    def __init__(self, path: Path, key: str) -> None:
        self.path = path
        self.key = key
        self.times: list[float] = []
        self._offset = 0
        self._rest = b""

    def update(self) -> bool:
        try:
            size = self.path.stat().st_size
        except OSError:
            return False
        if size < self._offset:           # 作り直された（ふつうは無い）
            self.times, self._offset, self._rest = [], 0, b""
        if size == self._offset:
            return False
        try:
            with self.path.open("rb") as f:
                f.seek(self._offset)
                data = f.read(size - self._offset)
        except OSError:
            return False
        self._offset += len(data)
        lines = (self._rest + data).split(b"\n")
        self._rest = lines.pop()          # 書きかけの最後の行は次に
        added = False
        for line in lines:
            value = _time_of(line.decode("utf-8", "replace"), self.key)
            if value is not None:
                bisect.insort(self.times, value)
                added = True
        return added


# ――― 見回り ―――

def default_runner(log_dir: Path, session_id: str, hand_id: int, target: str = "write") -> bool:
    """`tools/estimate_logs.py` を子プロセスで（低い優先度）。成功なら True。"""
    cmd = [sys.executable, str(ROOT / "tools" / "estimate_logs.py"), str(log_dir), "--session", session_id,
           "--hands", str(hand_id), "--workers", "1", "--shadow" if target == "shadow" else "--write"]
    kwargs: dict = {"cwd": str(ROOT), "capture_output": True, "timeout": RUN_TIMEOUT_SEC}
    if os.name == "nt":
        kwargs["creationflags"] = (getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x4000)
                                   | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
    else:
        kwargs["preexec_fn"] = lambda: os.nice(10)
    try:
        done = subprocess.run(cmd, **kwargs)
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("ハンド %s の推定を動かせませんでした: %s", hand_id, e)
        return False
    if done.returncode != 0:
        tail = (done.stderr or done.stdout or b"").decode("utf-8", "replace")[-800:]
        logger.warning("ハンド %s の推定に失敗しました（終了コード %s）: %s", hand_id, done.returncode, tail)
        return False
    return True


class LiveEstimator:
    """いまのセッションのハンドを、推せるようになったら 1 つずつ推す。"""

    def __init__(self, log_dir: Path, *, target: str = "write",
                 runner: Optional[Callable[[Path, str, int, str], bool]] = None,
                 clock: Callable[[], float] = time.time, idle_sec: float = IDLE_SEC, max_runs: int = MAX_RUNS,
                 active_sec: float = ACTIVE_SEC) -> None:
        self.log_dir = Path(log_dir)
        self.target = target
        self._runner = runner or default_runner
        self._clock = clock
        self._idle_sec = idle_sec
        self._max_runs = max_runs
        self._active_sec = active_sec
        self._runs: dict[tuple[str, object], int] = {}
        self._tried: dict[tuple[str, object], dict] = {}     # 最後に推したときの目印（失敗しても）
        self._tails: dict[str, tuple[_Tail, _Tail, _Tail]] = {}

    def current_session(self) -> Optional[str]:
        """events を最後に書いたセッション（`ACTIVE_SEC` より前なら無し）。"""
        try:
            files = list(self.log_dir.glob("*.events.jsonl"))
        except OSError:
            return None
        best = None
        for f in files:
            try:
                mtime = f.stat().st_mtime
            except OSError:
                continue
            if best is None or mtime > best[0]:
                best = (mtime, f.name[: -len(".events.jsonl")])
        if best is None or self._clock() - best[0] > self._active_sec:
            return None
        return best[1]

    def _marks(self, session_id: str) -> Marks:
        if session_id not in self._tails:
            d = self.log_dir
            self._tails = {session_id: (_Tail(d / f"{session_id}.transcripts.jsonl", "utterance_start_ts"),
                                        _Tail(d / f"{session_id}.offplay.jsonl", "utterance_start_ts"),
                                        _Tail(d / f"{session_id}.events.jsonl", "timestamp"))}
        rows, offplay, events = self._tails[session_id]
        for tail in (rows, offplay, events):
            tail.update()
        merged = sorted(rows.times + offplay.times) if offplay.times else rows.times
        return Marks(merged, events.times)

    def _estimates(self, session_id: str) -> dict:
        suffix = SHADOW_SUFFIX if self.target == "shadow" else ESTIMATE_SUFFIX
        data = read_json(self.log_dir / f"{session_id}{suffix}") or {}
        return data.get("hands") or {}

    def due(self, session_id: str) -> list[int]:
        """いま推すハンド（古い順）。"""
        return [hid for hid, _ in self._due(session_id)]

    def _due(self, session_id: str) -> list[tuple[int, dict]]:
        record = read_json(self.log_dir / f"{session_id}.json")
        if record is None:
            return []
        hands = [h for h in record.get("hands") or [] if isinstance(h, dict) and h.get("hand_id") is not None]
        live = (read_json(self.log_dir / f"{session_id}.live_hand.json") or {}).get("hand")
        nexts = next_starts(hands, live)
        marks = self._marks(session_id)
        done = self._estimates(session_id)
        now = self._clock()
        out = []
        for h in sorted(hands, key=lambda h: _epoch(h.get("started_at")) or 0.0):
            hid, started = h["hand_id"], _epoch(h.get("started_at"))
            if started is None or self._runs.get((session_id, hid), 0) >= self._max_runs:
                continue
            nxt = nexts.get(hid)
            ended = _epoch(h.get("ended_at")) or started
            if nxt is None and now - ended < self._idle_sec:
                continue                  # 窓がまだ閉じていない（次のハンドを待つ）
            marker = hand_marker(started, nxt, marks)
            if (done.get(str(hid)) or {}).get("inputs") == marker:
                continue                  # 推したときと入力が同じ
            if self._tried.get((session_id, hid)) == marker:
                continue                  # 同じ入力で失敗した（推定は決定的 = 推し直しても同じ）
            out.append((hid, marker))
        return out

    def step(self, stop: Optional[threading.Event] = None) -> int:
        """1 回の見回り。推したハンドの数。"""
        session_id = self.current_session()
        if session_id is None:
            return 0
        ran = 0
        for hid, marker in self._due(session_id):
            if stop is not None and stop.is_set():
                break
            key = (session_id, hid)
            self._runs[key] = self._runs.get(key, 0) + 1
            self._tried[key] = marker
            started = time.monotonic()
            ok = False
            try:
                ok = self._runner(self.log_dir, session_id, hid, self.target)
            except Exception:  # noqa: BLE001 — 推定の失敗で見回りを止めない
                logger.exception("ハンド %s の推定で例外", hid)
            ran += 1
            logger.info("ハンド %s を推定しました（%s・%.1f 秒・%d 回目）", hid, "成功" if ok else "失敗",
                        time.monotonic() - started, self._runs[key])
        return ran

    def run_forever(self, stop: threading.Event, interval: float = RECHECK_SEC) -> None:
        while not stop.wait(interval):
            self.step(stop)


def live_target(cfg: dict) -> Optional[str]:
    """設定 `estimator.live`: true（既定）= 推定のファイルに書く / "shadow" = 影のファイル / false = 推さない。"""
    value = (cfg.get("estimator") or {}).get("live", True)
    if value == "shadow":
        return "shadow"
    return "write" if value is True or value == "write" else None
