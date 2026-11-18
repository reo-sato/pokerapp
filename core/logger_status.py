"""core/logger_status.py — ハンドロガーを 1 つに限るロックと、状態・お知らせのファイル（ADR-0073 D2・D3）。

iPad（スタッフ用画面）で、ロガーが動いているか・どのセッションか・マイクと RFID の状態・端末に出たお知らせを見るため、
ロガーは次のファイルを書く。店舗サーバーの API（`GET /api/staff/logger`）が読む。

- `logger.lock`: ロガーを 1 つに限る OS のロック（`core/store_lock.py` と同じ作り）。端末・GUI・台本のどれで起動しても
  取る。取れなければ起動しない。先頭に持ち主（入口・pid・時刻）を書く。見張り側はロックを**取ってみない**（試し取りの
  一瞬に起動したロガーが止まるため）: 持ち主の pid と心拍で判断する。
- `logger_state.json`: 数秒ごとに書き直す。段階（設定待ち / 動作中 / 終了中 / 終了）・session_id・マイクと RFID の状態・
  聞き取り待ちの件数・心拍（`updated_at`）。
- `logger_notices.jsonl`: 端末に出したお知らせ（`●` の行・未適用・アクション・聞き取り）を 1 行ずつ。起動のたびに前の分を
  `logger_notices.prev.jsonl` に移す。各行に連番（`seq`）があり、画面は「この番号より後」を読む。

置き場はリポジトリの `logs/`（卓の設定でログのフォルダが決まる前から書くため）。環境変数 `POKERAPP_LOGGER_STATE_DIR` で
変えられる（試験が並列で走るとき）。
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from core.atomic_io import atomic_write_json
from core.store_lock import StoreWriterLock

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR_ENV = "POKERAPP_LOGGER_STATE_DIR"
LOCK_FILE = "logger.lock"
STATE_FILE = "logger_state.json"
NOTICES_FILE = "logger_notices.jsonl"
PREV_NOTICES_FILE = "logger_notices.prev.jsonl"
HEARTBEAT_SEC = 3.0
STALE_SEC = 15.0            # 心拍がこれより古ければ「応答なし」
LOCK_RETRIES = 4            # 起動のときのやり直し（0.5 秒おき。見張りや別の起動と重なった一瞬を避ける）

PHASES = ("starting", "setup", "running", "stopping", "stopped")
MODE_JA = {"cli": "端末（RDP）", "gui": "画面（RDP）", "script": "台本", "headless": "iPad から"}


def state_dir() -> Path:
    raw = os.environ.get(STATE_DIR_ENV)
    return Path(raw) if raw else ROOT / "logs"


class LoggerLock(StoreWriterLock):
    """ロガーを 1 つに限るロック（取れたら、このプロセスが唯一のロガー）。"""

    def __init__(self, directory: Optional[Path] = None) -> None:
        super().__init__((directory or state_dir()) / LOCK_FILE)

    def acquire_with_retries(self, mode: str, retries: Optional[int] = None, wait: float = 0.5) -> bool:
        retries = LOCK_RETRIES if retries is None else retries
        for attempt in range(retries):
            if self.acquire(mode):
                return True
            if attempt + 1 < retries:
                time.sleep(wait)
        return False


def describe_logger_holder(info: Optional[dict]) -> str:
    if not info:
        return "別のロガー"
    since = info.get("since")
    return (f"{MODE_JA.get(str(info.get('mode')), 'ロガー')}で起動したロガー"
            f"（pid {info.get('pid')}{f', {since} から' if since else ''}）")


def pid_alive(pid: Any) -> bool:
    """そのプロセスが動いているか（Windows = OpenProcess + 終了コード、それ以外 = kill 0）。"""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)      # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259                                # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class LoggerStatus:
    """ロガーの状態とお知らせのファイルを書く。`start()` で心拍のスレッドを動かし、`close()` で「終了」にする。

    `sources` は状態に足す値を返す関数（マイク・RFID・聞き取り待ち）。ロガーの組み立ての途中で `attach()` する。
    """

    def __init__(self, mode: str, directory: Optional[Path] = None, version: Optional[str] = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.dir = directory or state_dir()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.mode = mode
        self._clock = clock
        self._lock = threading.Lock()
        self._sources: dict[str, Callable[[], Any]] = {}
        self._hooks: list[Callable[[], None]] = []        # 心拍ごとに呼ぶ（セッションの見張りなど）
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._seq = 0
        notices = self.dir / NOTICES_FILE
        if notices.exists():
            try:
                os.replace(notices, self.dir / PREV_NOTICES_FILE)
            except OSError:
                logger.warning("前のお知らせを移せません: %s", notices)
        self.state: dict = {"phase": "starting", "mode": mode, "pid": os.getpid(), "session_id": None,
                            "started_at": _now_iso(), "updated_at": None, "version": version}

    # ――― 状態 ―――

    def attach(self, name: str, source: Callable[[], Any]) -> None:
        with self._lock:
            self._sources[name] = source

    def add_hook(self, hook: Callable[[], None]) -> None:
        """心拍ごと（3 秒）に呼ぶ関数を足す（例外はログに残して続ける）。"""
        with self._lock:
            self._hooks.append(hook)

    def set(self, **fields: Any) -> None:
        with self._lock:
            self.state.update(fields)
        self.write()

    def write(self) -> None:
        with self._lock:
            snapshot = dict(self.state)
            sources = dict(self._sources)
        for name, source in sources.items():
            try:
                snapshot[name] = source()
            except Exception as e:  # noqa: BLE001 — 状態を集められなくてもロガーは続ける
                snapshot[name] = {"error": str(e)[:200]}
        snapshot["updated_at"] = _now_iso()
        snapshot["updated_ts"] = self._clock()
        snapshot["notice_seq"] = self._seq
        try:
            atomic_write_json(self.dir / STATE_FILE, json.loads(json.dumps(snapshot, ensure_ascii=False, default=str)))
        except (OSError, TypeError, ValueError):
            logger.warning("ロガーの状態を書けません: %s", self.dir / STATE_FILE, exc_info=True)

    def start(self) -> None:
        self.write()
        if self._thread is None:
            self._thread = threading.Thread(target=self._beat, name="LoggerStatus", daemon=True)
            self._thread.start()

    def _beat(self) -> None:
        while not self._stop.wait(HEARTBEAT_SEC):
            with self._lock:
                hooks = list(self._hooks)
            for hook in hooks:
                try:
                    hook()
                except Exception:  # noqa: BLE001 — 見張りの失敗でロガーを止めない
                    logger.warning("ロガーの見張りに失敗しました", exc_info=True)
            self.write()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        with self._lock:
            self._sources.clear()
            self._hooks.clear()
            self.state.update(phase="stopped", ended_at=_now_iso())
        self.write()

    # ――― お知らせ ―――

    def notice(self, kind: str, text: str, **fields: Any) -> None:
        """端末に出したお知らせを 1 行足す（kind = notice / unapplied / action / heard / info）。"""
        with self._lock:
            self._seq += 1
            row = {"seq": self._seq, "t": self._clock(), "kind": kind, "text": str(text)[:500],
                   "session_id": self.state.get("session_id"), **fields}
            try:
                with (self.dir / NOTICES_FILE).open("a", encoding="utf-8") as f:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
            except OSError:
                logger.warning("お知らせを書けません", exc_info=True)


# ───────────────────────── 読む側（店舗サーバーの API） ─────────────────────────


def _read_json(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def read_logger(directory: Optional[Path] = None, now: Optional[float] = None) -> dict:
    """ロガーの様子: `status` = running（動作中）/ unresponsive（心拍が古い）/ crashed（終わらずに止まった）/ stopped。

    ロックは取ってみない。持ち主の pid が動いているかと、状態のファイルの心拍で判断する。
    """
    d = directory or state_dir()
    now = time.time() if now is None else now
    holder = LoggerLock(d).holder()
    state = _read_json(d / STATE_FILE)
    pid = (state or {}).get("pid") or (holder or {}).get("pid")
    alive = pid_alive(pid)
    phase = (state or {}).get("phase")
    age = now - float(state["updated_ts"]) if state and isinstance(state.get("updated_ts"), (int, float)) else None
    if state is None and not alive:
        status = "stopped"
    elif alive and phase != "stopped":
        status = "running" if age is not None and age <= STALE_SEC else "unresponsive"
    elif phase == "stopped":
        status = "stopped"
    else:
        status = "crashed"                    # 終了の段階に達しないままプロセスが無い
    return {"status": status, "state": state, "holder": holder, "heartbeat_age_sec": None if age is None else round(age, 1),
            "holder_text": describe_logger_holder(holder) if status in ("running", "unresponsive") else None}


def read_notices(directory: Optional[Path] = None, since: int = 0, limit: int = 100) -> dict:
    """`since` より後のお知らせ（多ければ新しい方から `limit` 件）。"""
    d = directory or state_dir()
    rows: list[dict] = []
    try:
        lines = (d / NOTICES_FILE).read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    parsed = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and isinstance(row.get("seq"), int):
            parsed.append(row)
    newest = parsed[-1]["seq"] if parsed else 0
    reset = since > newest                       # ロガーが起動し直した（番号が 1 から）: 初めから返す
    start = 0 if reset else since
    rows = [r for r in parsed if r["seq"] > start]
    return {"notices": rows[-limit:], "last_seq": rows[-1]["seq"] if rows else start, "reset": reset}


# ───────────────────────── Windows のセッションの見張り（ADR-0073 段階 0） ─────────────────────────

SESSION_CHANGE_TEXT = {"rdp": "RDP でつながりました", "disconnected": "RDP が閉じられました（切断）",
                       "console": "コンソールに戻りました"}


def session_watcher(get_info: Callable[[], dict], notify: Callable[[str], None],
                    reopen: Optional[Callable[[str], None]] = None) -> Callable[[], None]:
    """セッションの種類（コンソール / RDP / 切断）が変わったら、知らせてマイクの開き直しを頼む関数を作る。

    セッションが変わると、開いたままのマイクが無音になることがある（新しく開けば聞こえる）。`reopen` があれば
    開き直しを頼み（録音の側が「マイクを開き直します（…）」と知らせる）、無ければ知らせるだけ。Windows 以外では何もしない。
    """
    from core.win_session import session_kind

    last = [session_kind(get_info())]

    def check() -> None:
        kind = session_kind(get_info())
        if kind == "unknown" or kind == last[0]:
            return
        last[0] = kind
        text = SESSION_CHANGE_TEXT.get(kind, f"セッションが変わりました（{kind}）")
        if reopen is not None:
            reopen(text)
        else:
            notify(text)

    return check
