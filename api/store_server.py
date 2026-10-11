"""api/store_server.py — 画面を持たない店舗サーバー（`python main.py --serve`, ADR-0065）。

店舗 PC のサインイン時（通電 → 自動サインイン）にタスクスケジューラが窓なしで起動する（`installer/store_server_task.ps1`）。
1 つのプロセスで:

- **会計の書き手の API**（会計画面 `--ledger` に組み込まれていた API と同じ = `create_store_app`）。
  お客さん向け画面 `/`・スタッフ用画面 `/staff/`・注文の受付・スタッフの会計操作（ADR-0059/0064/0021）。
- **卓モニタ**（`tools/table_monitor.py`, 既定 8790）と、任意で**真のアクション入力**（`tools/ground_truth_ui.py`, 8791）。

録音（ハンドロガー `--cli`）は含めない。席と名前をスタッフが入れる対話式で、RDP のセッションの中で動かす。

会計を書くのは 1 プロセスだけ（`core/store_lock.py`）。会計画面（`--ledger`）が動いていれば起動しない。

落ちたとき:
- 卓モニタなどの小さなサーバーは、このプロセスの中で起動し直す（5 秒後。失敗が続けば最大 60 秒おき）。
- API が止まればプロセスごと終わる（終了コード 1）。タスクスケジューラが 1 分後に起動し直す。
"""
from __future__ import annotations

import logging
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable, Optional

from api.server import (
    PLAYER_WEB_DIR,
    STAFF_WEB_DIR,
    auth_kwargs_from_config,
    create_app,
    log_web_addresses,
)
from core.ledger_repository import LedgerRepository
from core.menu import MenuMaster
from core.order_request_repository import OrderRequestRepository
from core.player_repository import PlayerRepository
from core.session_repository import SessionRepository
from core.store_lock import StoreWriterLock, describe_holder
from core.version import describe as describe_version

logger = logging.getLogger(__name__)

DEFAULT_BIND_HOST = "0.0.0.0"   # iPad・スマホから使うのが目的（受け付けは Windows のファイアウォールで店内に限る）
DEFAULT_MONITOR_PORT = 8790
DEFAULT_GROUND_TRUTH_PORT = 8791
SIDE_RESTART_SEC = 5.0
SIDE_RESTART_MAX_SEC = 60.0
SIDE_STABLE_SEC = 60.0
LOG_PATH = Path(__file__).resolve().parent.parent / "logs" / "store_server.log"


def create_store_app(cfg: dict, *, player_repo: PlayerRepository, session_repo: SessionRepository,
                     ledger_repo: LedgerRepository, order_repo: OrderRequestRepository,
                     menu: MenuMaster):
    """会計の書き手の API（注文 write・スタッフの会計 write が有効）を作る。

    会計画面（`--ledger` + `viewer_api.enabled`）と店舗サーバー（`--serve`）の両方がこれを使い、
    同じ API・同じ画面を出す。
    """
    api_cfg = cfg.get("viewer_api", {})
    return create_app(
        player_repo, session_repo,
        cfg.get("session", {}).get("log_dir", "./logs"),
        ledger_repo=ledger_repo, order_repo=order_repo, menu=menu,
        orders_writable=True,
        staff_token=api_cfg.get("staff_token") or None,
        buyin_presets=cfg.get("ledger", {}).get("buyin_presets", []) or [],   # ADR-0026
        player_web_dir=PLAYER_WEB_DIR,   # お客さん向け画面（ADR-0059）
        staff_web_dir=STAFF_WEB_DIR,     # スタッフ用 iPad 画面（ADR-0064）
        **auth_kwargs_from_config(api_cfg),   # L1 PIN（ADR-0027）
    )


def server_settings(cfg: dict, host: Optional[str] = None, port: Optional[int] = None) -> dict:
    """待ち受けの設定。`--host` / `--port` > `store_server` > `viewer_api.bind_port` > 既定の順。"""
    ss = cfg.get("store_server", {})
    api_cfg = cfg.get("viewer_api", {})
    return {
        "host": host or ss.get("bind_host") or DEFAULT_BIND_HOST,
        "port": int(port or ss.get("bind_port") or api_cfg.get("bind_port") or 8788),
        "table_monitor": bool(ss.get("table_monitor", True)),
        "table_monitor_port": int(ss.get("table_monitor_port", DEFAULT_MONITOR_PORT)),
        "ground_truth_ui": bool(ss.get("ground_truth_ui", False)),
        "ground_truth_port": int(ss.get("ground_truth_port", DEFAULT_GROUND_TRUTH_PORT)),
        "log_dir": cfg.get("session", {}).get("log_dir", "./logs"),
    }


def keep_running(name: str, target: Callable[[], None], stop: threading.Event,
                 restart_sec: float = SIDE_RESTART_SEC, max_restart_sec: float = SIDE_RESTART_MAX_SEC,
                 stable_sec: float = SIDE_STABLE_SEC,
                 clock: Callable[[], float] = time.monotonic) -> threading.Thread:
    """`target`（戻らない想定のサーバー）を別スレッドで動かし、戻ったり落ちたりしたら起動し直す。

    続けて失敗するほど待ち時間を倍にする（`restart_sec` → 最大 `max_restart_sec`）。ポートが他に使われて
    いるときなどに、ログを数秒ごとの失敗で埋めないため。`stable_sec` 以上動いてから止まったら最初の待ち時間に戻す。
    """
    def loop() -> None:
        wait = restart_sec
        while not stop.is_set():
            began = clock()
            try:
                target()
                logger.warning("%s が止まりました。%.0f 秒後に起動し直します。", name, wait)
            except Exception:  # noqa: BLE001 — どんな失敗でも店舗サーバーごとは落とさない
                logger.exception("%s が落ちました。%.0f 秒後に起動し直します。", name, wait)
            if clock() - began >= stable_sec:
                wait = restart_sec
            if stop.wait(wait):
                break
            wait = min(wait * 2, max_restart_sec)
    thread = threading.Thread(target=loop, daemon=True, name=f"keep-{name}")
    thread.start()
    return thread


def _route_logs(path: Path) -> None:
    """ログをファイルへ（自動起動では端末が無い）。5 MB × 5 世代で回す。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = RotatingFileHandler(path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(threadName)s] %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def _side_servers(settings: dict, stop: threading.Event) -> list[threading.Thread]:
    threads = []
    host = settings["host"]
    log_dir = Path(settings["log_dir"])
    if settings["table_monitor"]:
        from tools import table_monitor

        port = settings["table_monitor_port"]
        threads.append(keep_running(
            "卓モニタ", lambda: table_monitor.serve(log_dir, None, host, port), stop))
        logger.info("卓モニタ: http://<この PC の IP>:%s/", port)
    if settings["ground_truth_ui"]:
        from tools import ground_truth_ui

        port = settings["ground_truth_port"]

        def run_truth() -> None:
            log_dir.mkdir(parents=True, exist_ok=True)
            server = ground_truth_ui.make_server(
                log_dir, host, port, Path("hand_corrections.json"), ground_truth_ui.BLIND_EVERY)
            try:
                server.serve_forever()
            finally:
                server.server_close()

        threads.append(keep_running("真のアクション入力", run_truth, stop))
        logger.info("真のアクション入力: http://<この PC の IP>:%s/", port)
    return threads


def run_store_server(cfg: dict, host: Optional[str] = None, port: Optional[int] = None,
                     lock: Optional[StoreWriterLock] = None, log_to_file: bool = True) -> int:
    """店舗サーバーを foreground で動かす。戻り値は終了コード（0 = 正常に止めた / 1 = 止まった・起動できない）。"""
    import uvicorn

    if log_to_file:
        _route_logs(LOG_PATH)
    lock = lock or StoreWriterLock()
    if not lock.acquire("serve"):
        msg = (f"会計を書くプロセスが既に動いています: {describe_holder(lock.holder())}。"
               "店舗サーバーは起動しません（会計画面を閉じてから、もう一度起動してください）。")
        logger.error(msg)
        print(msg)
        return 1

    settings = server_settings(cfg, host, port)
    stop = threading.Event()
    try:
        player_repo = PlayerRepository()
        session_repo = SessionRepository(player_repo=player_repo)
        app = create_store_app(
            cfg, player_repo=player_repo, session_repo=session_repo,
            ledger_repo=LedgerRepository(session_repo=session_repo, player_repo=player_repo),
            order_repo=OrderRequestRepository(session_repo=session_repo, player_repo=player_repo),
            menu=MenuMaster(),
        )
        _side_servers(settings, stop)
        logger.info("店舗サーバーを起動します: %s:%s（%s）", settings["host"], settings["port"], describe_version())
        log_web_addresses(settings["port"])
        print(f"店舗サーバー: http://<この PC の IP>:{settings['port']}/（スタッフ用は /staff/）。"
              f"ログ: {LOG_PATH}")
        server = uvicorn.Server(uvicorn.Config(
            app, host=settings["host"], port=settings["port"], log_level="warning"))
        try:
            server.run()
        except SystemExit:   # uvicorn は待ち受けに失敗すると sys.exit する
            pass
        if server.started:
            logger.info("店舗サーバーを止めました。")
            return 0
        logger.error("API を起動できませんでした（ポート %s が別のプログラムに使われている可能性。"
                     "お客さん用 ハンド履歴（start_viewer.cmd）を閉じてください）。", settings["port"])
        return 1
    except Exception:  # noqa: BLE001 — 自動起動では端末が無いので、理由をログに残して終わる
        logger.exception("店舗サーバーが落ちました（タスクスケジューラが起動し直します）。")
        return 1
    finally:
        stop.set()
        lock.release()
