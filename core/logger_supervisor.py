"""core/logger_supervisor.py — 店舗サーバーの見張り役: iPad の操作でハンドロガーを起動し、見張る（ADR-0073 D1・D7・段階 C）。

- 起動: iPad の「卓を始める」で、卓の設定（`tools/session_setup_ui` の形の生の入力）を検証してからファイルに書き、
  `main.py --headless --setup <file>` を子のプロセスとして起動する。ロガーは起動のあとにもう一度検証する（時刻準拠の時計が
  モデルの読み込みの分だけずれない = D8）。同時に 1 つ（ロガーのロック = `core/logger_status.LoggerLock`）。
- 終了: 制御の `stop`（`POST /api/staff/logger/control`）。ここでは送らない。
- 見張り: 子は店舗サーバーと独立に動く（段階 0 の実測で、親のタスクを止めても子は残った）。店舗サーバーが起動し直したら、
  状態のファイルで見えるロガーをそのまま引き取る。ロガーが終了の段階に達しないまま止まったら、自動でやるのは 2 つだけ:
  お知らせを足す・`<sid>.live_hand.json` を消す。セッションは閉じない（閉じたセッションは開き直せない。閉じるかどうかは
  スタッフが選ぶ = D7）。
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from core.atomic_io import atomic_write_json
from core.logger_status import append_notice, read_logger, state_dir

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
SETUP_FILE = "logger_setup.json"          # iPad から受け取った卓の設定（ロガーに渡す）
CHILD_LOG = "logger_child.log"            # 子の標準出力・標準エラー（pythonw の子は端末を持たない）
CRASH_FILE = "logger_crash.json"          # 落ちたロガー（同じ落ち方を 2 回知らせない・iPad に出す）
START_INTERVAL_SEC = 10.0                 # 続けて押された「卓を始める」を断る
WATCH_SEC = 3.0
CREATE_NO_WINDOW = 0x08000000


class LoggerBusy(Exception):
    """ロガーがもう動いている（PC で起動したロガーも含む）。"""

    def __init__(self, holder_text: Optional[str]) -> None:
        super().__init__(holder_text or "ロガーが動いています")
        self.holder_text = holder_text


class TooSoon(Exception):
    """直前に起動したばかり。"""


class LoggerSupervisor:
    def __init__(self, log_dir: "str | Path", root: Path = ROOT, python: Optional[str] = None,
                 popen: Callable[..., Any] = subprocess.Popen, clock: Callable[[], float] = time.time,
                 state_directory: Optional[Path] = None) -> None:
        self.log_dir = Path(log_dir)
        self.root = root
        self.python = python or sys.executable
        self._popen = popen
        self._clock = clock
        self._state_dir = state_directory
        self._last_start: Optional[float] = None
        self._lock = threading.Lock()
        self._adopted = False
        self.child: Any = None

    @property
    def state_dir(self) -> Path:
        return self._state_dir or state_dir()

    # ――― 起動 ―――

    def start(self, raw: dict) -> dict:
        """卓の設定を検証して、ロガーを起動する。誤りは `SetupError`（項目ごと）、動いていれば `LoggerBusy`。"""
        from core.blind_structure import BlindStructureStore
        from tools.session_setup_ui import SetupError, build_session_config

        if not isinstance(raw, dict):
            raise SetupError([{"field": "setup", "message": "卓の設定は JSON のオブジェクトにしてください"}])
        with self._lock:
            now = self._clock()
            if self._last_start is not None and now - self._last_start < START_INTERVAL_SEC:
                raise TooSoon(f"起動したばかりです（{START_INTERVAL_SEC:.0f} 秒あけてください）")
            current = read_logger(self.state_dir)
            if current["status"] in ("running", "unresponsive"):
                raise LoggerBusy(current.get("holder_text"))
            setup = {k: v for k, v in raw.items() if k != "log_dir"}     # ログのフォルダは受け取らない（D8）
            build_session_config(dict(setup), BlindStructureStore())     # 誤りはその場で返す（結果は捨てる）
            self.log_dir.mkdir(parents=True, exist_ok=True)
            setup_path = self.log_dir / SETUP_FILE
            atomic_write_json(setup_path, setup)
            env = dict(os.environ, PYTHONUTF8="1")
            kwargs: dict[str, Any] = {"cwd": str(self.root), "env": env, "stdin": subprocess.DEVNULL}
            if sys.platform == "win32":
                kwargs["creationflags"] = CREATE_NO_WINDOW
            with (self.log_dir / CHILD_LOG).open("a", encoding="utf-8") as out:
                out.write(f"\n=== {datetime.now().isoformat(timespec='seconds')} iPad から起動 ===\n")
                out.flush()
                self.child = self._popen(
                    [self.python, str(self.root / "main.py"), "--headless", "--setup", str(setup_path.resolve())],
                    stdout=out, stderr=subprocess.STDOUT, **kwargs)
            self._last_start = now
            pid = getattr(self.child, "pid", None)
            logger.info("iPad からロガーを起動しました（pid %s）", pid)
            return {"pid": pid, "started_at": datetime.now().isoformat(timespec="seconds")}

    # ――― 見張り ―――

    def crash(self) -> Optional[dict]:
        """落ちたロガーの記録（無ければ None）。"""
        try:
            data = json.loads((self.state_dir / CRASH_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def watch_once(self) -> Optional[dict]:
        """ロガーが落ちていれば後始末して、その記録を返す（同じ pid は 1 回だけ）。"""
        if self.child is not None and hasattr(self.child, "poll"):
            self.child.poll()                     # 終わった子を回収する（ゾンビを残さない）
        current = read_logger(self.state_dir)
        state = current.get("state") or {}
        if not self._adopted:
            self._adopted = True
            if current["status"] in ("running", "unresponsive"):
                logger.info("動いているロガーを引き取りました（pid %s・セッション %s）",
                            state.get("pid"), state.get("session_id"))
        if current["status"] != "crashed":
            return None
        pid = state.get("pid")
        previous = self.crash()
        if previous is not None and previous.get("pid") == pid:
            return None
        sid = state.get("session_id")
        hand_id = None
        if sid:
            live = Path(state.get("log_dir") or self.log_dir) / f"{sid}.live_hand.json"
            try:
                hand_id = ((json.loads(live.read_text(encoding="utf-8")) or {}).get("hand") or {}).get("hand_id")
            except (OSError, ValueError, AttributeError):
                hand_id = None
            try:
                live.unlink()
            except OSError:
                pass
        record = {"pid": pid, "session_id": sid, "hand_id": hand_id, "phase": state.get("phase"),
                  "at": datetime.now().isoformat(timespec="seconds")}
        try:
            atomic_write_json(self.state_dir / CRASH_FILE, record)
        except OSError:
            logger.warning("落ちたロガーの記録を書けません", exc_info=True)
        text = "ロガーが止まりました（終了の手順を通っていません）"
        if hand_id is not None:
            text += f" — ハンド {hand_id} は記録の途中で止まりました"
        text += "。セッションを閉じるか、もう一度「卓を始める」かを選んでください"
        append_notice("notice", text, self.state_dir, session_id=sid)
        logger.warning("%s（pid %s・セッション %s）", text, pid, sid)
        return record

    def run_forever(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.watch_once()
            except Exception:  # noqa: BLE001 — 見張りの失敗で店舗サーバーを止めない
                logger.warning("ロガーの見張りに失敗しました", exc_info=True)
            stop.wait(WATCH_SEC)
