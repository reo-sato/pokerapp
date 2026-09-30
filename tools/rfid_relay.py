#!/usr/bin/env python3
"""tools/rfid_relay.py — RFID の中継（RDP のセッションの外でリーダーを読み、ロガーに渡す）。

RDP で操作する店舗 PC では、RDP のセッションの中のアプリからリーダー（PC/SC）が見えない（店舗 2026-09-30）。
中継はシステムの権限（スケジュールタスク `PokerRFIDRelay`, PC の起動時）で動き、ロガーは動いている中継から札を
受け取る（`rfid/relay.py`）。

使い方:
    python tools/rfid_relay.py serve            # 中継を動かす（タスクが使う。ログは logs/rfid_relay.log）
    python tools/rfid_relay.py status           # 中継が動いているか・リーダーごとの読み取り
    python tools/rfid_relay.py watch --seconds 60   # 札を置く・外すたびに 1 行出す（中継を通した読み取りの確認）

中継の登録と起動（管理者）: installer/rfid_relay_task.ps1
"""
from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rfid.relay import (  # noqa: E402
    DEFAULT_RELAY_PORT,
    STALE_SEC,
    RelayClient,
    RelayPoller,
    make_relay_server,
    reader_key,
    relay_port,
)

CONFIG_CHECK_SEC = 2.0       # config.json が変わったか見る間隔（変わればリーダーを読み直す = 中継の起動し直し不要）


def _rfid_config() -> dict:
    from core.config import load_config

    return load_config().get("rfid", {}) or {}


def _readers(rfid_cfg: dict) -> list[dict]:
    readers = rfid_cfg.get("pcsc_readers", rfid_cfg.get("readers", []))
    return readers if isinstance(readers, list) else []


def _port(args: argparse.Namespace, rfid_cfg: dict) -> int:
    return int(args.port or relay_port(rfid_cfg))


def _config_stamp() -> tuple[int, int] | None:
    from core.config import _CONFIG_PATH

    try:
        st = _CONFIG_PATH.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def watch_config(poller: RelayPoller, stop: threading.Event, *, interval: float = CONFIG_CHECK_SEC,
                 stamp=_config_stamp, load=_rfid_config) -> None:
    """config.json が変わったら、中継が読むリーダーを入れ替える（読めない瞬間はいまのまま）。"""
    last = stamp()
    while not stop.wait(interval):
        now = stamp()
        if now == last:
            continue
        try:
            readers = _readers(load())
        except Exception as e:  # noqa: BLE001 — 書きかけの config は次の確認で読む
            logging.getLogger("rfid_relay").warning("config.json を読めません（次の確認で読み直します）: %s", e)
            continue
        last = now
        poller.set_readers(readers)


def _label(cfg: dict) -> str:
    if cfg.get("role") == "board":
        return f"board [r{cfg.get('reader', 0)}]"
    return f"seat {cfg.get('seat')} [r{cfg.get('reader', 0)}]"


def _keys(readers: list[dict]) -> list[tuple[str, str]]:
    from rfid.reader_thread import _reader_index_of

    return [(_label(cfg), reader_key(str(cfg.get("name", "")), _reader_index_of(cfg, i)))
            for i, cfg in enumerate(readers)]


def cmd_serve(args: argparse.Namespace) -> int:
    log_dir = ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    logging.basicConfig(
        filename=str(log_dir / "rfid_relay.log"), level=logging.INFO, encoding="utf-8",
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    rfid_cfg = _rfid_config()
    readers = _readers(rfid_cfg)
    port = _port(args, rfid_cfg)
    poller = RelayPoller(readers, poll_interval_ms=int(args.poll_ms))
    try:
        server = make_relay_server(poller, port)
    except OSError as e:
        logging.getLogger("rfid_relay").error("port %d を開けません（中継が既に動いている?）: %s", port, e)
        return 1
    poller.start()
    stop = threading.Event()
    threading.Thread(target=watch_config, args=(poller, stop), daemon=True, name="RelayConfigWatch").start()
    logging.getLogger("rfid_relay").info("RFID relay listening on 127.0.0.1:%d (%d readers)", port, len(readers))
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        stop.set()
        poller.stop()
        server.server_close()
    return 0


def describe(snapshot: dict | None, readers: list[dict], now: float) -> tuple[bool, list[str]]:
    """中継の読み取りを人が読む形に。(使えるか, 行)。"""
    if snapshot is None:
        return False, ["中継: 動いていません（installer\\rfid_relay_task.ps1 で登録・起動）"]
    connected, configured = snapshot.get("connected", 0), snapshot.get("configured", 0)
    ok = snapshot.get("state") == "running" and connected == configured and configured > 0
    lines = [f"中継: 動いています（リーダー {connected}/{configured} 台）"
             + ("" if ok else " — つながっていないリーダーがあります")]
    table = snapshot.get("readers") or {}
    for label, key in _keys(readers):
        entry = table.get(key)
        if not isinstance(entry, dict):
            lines.append(f"  ✗ {label:<14} つながっていません")
            ok = False
            continue
        age = now - float(entry.get("at") or 0)
        uids = entry.get("uids") or []
        state = "読み取りが止まっています" if age > STALE_SEC else (f"札 {len(uids)} 枚" if uids else "札なし")
        lines.append(f"  {'✓' if age <= STALE_SEC else '✗'} {label:<14} {state}")
        ok = ok and age <= STALE_SEC
    return ok, lines


def cmd_status(args: argparse.Namespace) -> int:
    rfid_cfg = _rfid_config()
    client = RelayClient(port=_port(args, rfid_cfg))
    deadline = time.time() + max(0.0, float(args.wait))
    while True:                                   # 起動直後はつながるまで少し待つ（--wait 秒まで）
        ok, lines = describe(client.fetch(), _readers(rfid_cfg), time.time())
        if ok or time.time() >= deadline:
            break
        time.sleep(0.5)
    print("\n".join(lines))
    print(f"\n結果: {'PASS ✅' if ok else 'FAIL ❌'}")
    return 0 if ok else 1


def cmd_watch(args: argparse.Namespace) -> int:
    from rfid.card_master import CardMaster

    rfid_cfg = _rfid_config()
    readers = _readers(rfid_cfg)
    client = RelayClient(port=_port(args, rfid_cfg))
    cards = CardMaster(rfid_cfg.get("card_master_file", str(ROOT / "rfid_cards.json")))
    keys = _keys(readers)
    last: dict[str, set[str]] = {}
    deadline = time.time() + float(args.seconds)
    if client.fetch() is None:
        print("中継: 動いていません（installer\\rfid_relay_task.ps1 で登録・起動）")
        return 1
    print(f"札を置く・外すと 1 行出ます（{args.seconds:.0f} 秒）")
    while time.time() < deadline:
        snap = client.fetch() or {}
        table = snap.get("readers") or {}
        for label, key in keys:
            now_uids = set((table.get(key) or {}).get("uids") or [])
            before = last.get(key, set())
            for uid in sorted(now_uids - before):
                print(f"  + {label:<14} {cards.lookup(uid) or '(未登録)'}  {uid}")
            for uid in sorted(before - now_uids):
                print(f"  - {label:<14} {cards.lookup(uid) or '(未登録)'}  {uid}")
            last[key] = now_uids
        time.sleep(0.2)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="RFID の中継（RDP のセッションの外でリーダーを読む）")
    parser.add_argument("--port", type=int, default=None, help=f"中継のポート（既定 {DEFAULT_RELAY_PORT}）")
    sub = parser.add_subparsers(dest="command", required=True)
    p_serve = sub.add_parser("serve", help="中継を動かす（スケジュールタスクが使う）")
    p_serve.add_argument("--poll-ms", type=int, default=50, help="リーダーを 1 周読む間隔（ミリ秒）")
    p_serve.set_defaults(func=cmd_serve)
    p_status = sub.add_parser("status", help="中継が動いているか・リーダーごとの読み取り")
    p_status.add_argument("--wait", type=float, default=0.0, help="すべて読めるまで待つ秒数（中継を起動した直後）")
    p_status.set_defaults(func=cmd_status)
    p_watch = sub.add_parser("watch", help="札を置く・外すたびに 1 行出す")
    p_watch.add_argument("--seconds", type=float, default=60.0)
    p_watch.set_defaults(func=cmd_watch)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    threading.current_thread().name = "rfid_relay"
    raise SystemExit(main())
