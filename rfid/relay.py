"""rfid/relay.py — RFID の中継（relay）。

店舗 2026-09-30: RDP で操作する店舗 PC で、RDP のセッションの中のアプリは PC/SC のサービスに届かなかった
（`SCardEstablishContext` = 0x8010001D「スマート カード リソース マネージャーが実行されていません」。スマートカードの
転送をサーバ・接続元の両方で止め、サインアウトして入り直しても同じ）。システムの権限（セッション 0）からは
11 台すべて読めた。

- 中継（`RelayPoller` + `serve`）はシステムの権限のスケジュールタスク（PC の起動時, `installer/rfid_relay_task.ps1`）で
  動き、設定のリーダーを順に読んで、最新の UID を PC の中（127.0.0.1）だけに渡す。
- ロガーは `RelayBridge` で受け取る。札の扱い（`RFIDThread` の在否・ボードの位置・配り直し）は、リーダーを直接読む
  ときと同じ。中継が動いていなければ、これまでどおりリーダーを直接読む（`AutoRFIDSource`）。
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional

from rfid.bridge import PCSCBridge, bridge_read_uids, call_bridge_factory, pcsc_reader_present

logger = logging.getLogger(__name__)

DEFAULT_RELAY_PORT = 8792
RELAY_HOST = "127.0.0.1"
RECONNECT_SEC = 5.0          # つながらないリーダーを試し直す間隔
STALE_SEC = 3.0              # これより古い読み取りは使わない（中継の読み取りが止まった）
_SNAPSHOT_TTL_SEC = 0.04     # ロガーの 1 周（全リーダー）で 1 回だけ取りに行く
_HTTP_TIMEOUT_SEC = 0.5


def reader_key(name: str, index: int) -> str:
    """中継の中のリーダーの名前（PC/SC のリーダー名 + 物理リーダーの番号 = Get UID の P2）。"""
    return f"{name}|{index}"


def _targets(reader_configs: list[dict]) -> list[tuple[str, str, int]]:
    """設定のリーダー（重複は 1 つ）: (key, リーダー名, 物理リーダーの番号)。"""
    from rfid.reader_thread import _reader_index_of

    seen: dict[str, tuple[str, str, int]] = {}
    for i, cfg in enumerate(reader_configs):
        name = str(cfg.get("name", ""))
        index = _reader_index_of(cfg, i)
        seen.setdefault(reader_key(name, index), (reader_key(name, index), name, index))
    return list(seen.values())


class RelayPoller(threading.Thread):
    """設定のリーダーを順に読み、それぞれの最新の UID を持つ（中継の読み取り側）。"""

    def __init__(
        self,
        reader_configs: list[dict],
        *,
        poll_interval_ms: int = 50,
        bridge_factory: Optional[Callable] = None,
        reader_present: Optional[Callable[[str], bool]] = None,
        stop_event: Optional[threading.Event] = None,
        clock: Callable[[], float] = time.time,
        reconnect_sec: float = RECONNECT_SEC,
    ) -> None:
        super().__init__(daemon=True, name="RFIDRelayPoller")
        self._targets = _targets(reader_configs)
        self._poll_interval = max(0.0, poll_interval_ms / 1000.0)
        self._bridge_factory = bridge_factory or PCSCBridge
        self._reader_present = reader_present or pcsc_reader_present
        self._stop_event = stop_event or threading.Event()
        self._clock = clock
        self._reconnect_sec = max(0.0, float(reconnect_sec))
        self._bridges: dict[str, object] = {}
        self._latest: dict[str, tuple[list[str], float]] = {}
        self._lock = threading.Lock()
        self._tried_at: Optional[float] = None
        self._warned = False
        self._pending_targets: Optional[list[tuple[str, str, int]]] = None
        self._last_error: Optional[str] = None

    def stop(self) -> None:
        self._stop_event.set()

    def set_readers(self, reader_configs: list[dict]) -> None:
        """設定のリーダーを差し替える（config.json が変わったとき）。次の 1 周の頭で反映する（読むのは中継の
        スレッドだけ）。"""
        with self._lock:
            self._pending_targets = _targets(reader_configs)

    def _apply_pending_targets(self) -> None:
        with self._lock:
            targets, self._pending_targets = self._pending_targets, None
        if targets is None or targets == self._targets:
            return
        keep = {key for key, _, _ in targets}
        for key in [k for k in self._bridges if k not in keep]:
            close = getattr(self._bridges.pop(key), "close", None)
            if callable(close):
                close()
            with self._lock:
                self._latest.pop(key, None)
        self._targets = targets
        self._tried_at = None                      # 増えたリーダーはすぐ試す
        logger.info("RFID relay: 設定のリーダーが変わりました（%d 台）", len(targets))

    def _connect_missing(self) -> None:
        missing = [t for t in self._targets if t[0] not in self._bridges]
        if not missing:
            return
        now = self._clock()
        if self._tried_at is not None and now - self._tried_at < self._reconnect_sec:
            return
        first = self._tried_at is None
        self._tried_at = now
        names = {name for _, name, _ in missing}
        if not first and not any(self._reader_present(name) for name in names):
            return                                 # まだ見えない（ログを埋めない）
        for key, name, index in missing:
            bridge = call_bridge_factory(self._bridge_factory, name, index)
            if bridge.connect():
                self._bridges[key] = bridge
                logger.info("RFID relay: reader ready %s", key)
        if not self._bridges and not self._warned:
            logger.warning("RFID relay: リーダーにつながりません（%s）— %.0f 秒ごとに試し直します",
                           ", ".join(sorted(names)), self._reconnect_sec)
            self._warned = True

    def poll_once(self) -> None:
        """つながっていないリーダーを（間隔をあけて）試し、つながっているリーダーを 1 周読む。"""
        self._apply_pending_targets()
        self._connect_missing()
        for key, bridge in list(self._bridges.items()):
            uids = bridge_read_uids(bridge)
            with self._lock:
                self._latest[key] = (list(uids), self._clock())

    def run(self) -> None:
        logger.info("RFID relay: %d reader(s)", len(self._targets))
        try:
            while not self._stop_event.is_set():
                try:
                    self.poll_once()
                    self._last_error = None
                except Exception as e:  # noqa: BLE001 — 中継は止めない（読み取りが古くなればロガーは使わない）
                    if str(e) != self._last_error:
                        logger.exception("RFID relay: 読み取りに失敗しました（1 秒後に試し直します）")
                        self._last_error = str(e)
                    self._stop_event.wait(1.0)
                self._stop_event.wait(self._poll_interval)
        finally:
            for bridge in self._bridges.values():
                close = getattr(bridge, "close", None)
                if callable(close):
                    close()

    def snapshot(self) -> dict:
        """いまの読み取り（中継の HTTP が返す形）。"""
        with self._lock:
            readers = {key: {"uids": uids, "at": at} for key, (uids, at) in self._latest.items()}
        connected = sum(1 for key, _, _ in self._targets if key in self._bridges)
        return {
            "state": "running" if connected else "no_readers",
            "connected": connected,
            "configured": len(self._targets),
            "at": self._clock(),
            "readers": readers,
        }


def make_relay_server(poller: RelayPoller, port: int = DEFAULT_RELAY_PORT) -> ThreadingHTTPServer:
    """中継の HTTP（127.0.0.1 だけ）。`GET /` = いまの読み取り（`RelayPoller.snapshot`）。"""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler の規約
            body = json.dumps(poller.snapshot()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args: Any) -> None:  # 1 秒に何十回も来るので記録しない
            return

    server = ThreadingHTTPServer((RELAY_HOST, port), Handler)
    server.daemon_threads = True
    return server


class RelayClient:
    """中継の読み取りを取りに行く（ロガー側）。全リーダーぶんを 1 回で取り、少しの間使い回す。"""

    def __init__(self, port: int = DEFAULT_RELAY_PORT, *, timeout: float = _HTTP_TIMEOUT_SEC,
                 clock: Callable[[], float] = time.time) -> None:
        self.url = f"http://{RELAY_HOST}:{port}/"
        self._timeout = timeout
        self._clock = clock
        self._lock = threading.Lock()
        self._cached: Optional[dict] = None
        self._cached_at = 0.0
        # PC の中だけ。Windows のプロキシ設定（レジストリ）があっても 127.0.0.1 をプロキシに送らない。
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def fetch(self) -> Optional[dict]:
        """中継のいまの読み取り。中継が動いていなければ None。"""
        try:
            with self._opener.open(self.url, timeout=self._timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (OSError, ValueError, urllib.error.URLError):
            return None
        return data if isinstance(data, dict) else None

    def snapshot(self, max_age: float = _SNAPSHOT_TTL_SEC) -> Optional[dict]:
        with self._lock:
            now = time.monotonic()
            if self._cached is None or now - self._cached_at > max_age:
                self._cached = self.fetch()
                self._cached_at = now
            return self._cached

    def available(self) -> bool:
        return self.snapshot(max_age=1.0) is not None

    def now(self) -> float:
        return self._clock()


def relay_port(rfid_cfg: dict) -> int:
    return int((rfid_cfg or {}).get("relay_port", DEFAULT_RELAY_PORT))


def running_relay(rfid_cfg: dict) -> Optional[RelayClient]:
    """中継が動いていれば、その client（無ければ None）。確認・登録の道具が中継を通して読むのに使う。"""
    client = RelayClient(port=relay_port(rfid_cfg))
    return client if client.fetch() is not None else None


class RelayBridge:
    """`PCSCBridge` の代わりに中継から UID を受け取る（`RFIDThread` の bridge）。"""

    def __init__(self, reader_name: str, reader_index: int, client: RelayClient) -> None:
        self.reader_name = reader_name
        self.reader_index = reader_index
        self._key = reader_key(reader_name, reader_index)
        self._client = client

    def connect(self) -> bool:
        """中継が動いていれば True（リーダーそのものへの接続は中継が持つ）。"""
        return self._client.available()

    def read_uids(self) -> list[str]:
        snap = self._client.snapshot()
        entry = (snap or {}).get("readers", {}).get(self._key)
        if not isinstance(entry, dict):
            return []
        at = entry.get("at")
        if not isinstance(at, (int, float)) or self._client.now() - at > STALE_SEC:
            return []                         # 中継の読み取りが止まっている
        return [u for u in entry.get("uids") or [] if isinstance(u, str) and u]

    def read_uid(self) -> Optional[str]:
        uids = self.read_uids()
        return uids[0] if uids else None

    def close(self) -> None:
        return None


class AutoRFIDSource:
    """RFID の読み取り元を選ぶ: 中継が動いていれば中継、無ければリーダーを直接（`RFIDThread` に渡す）。

    リーダーにつなぐたびに選び直すので、ロガーを先に起動して中継があとから動いても使い始める。
    """

    def __init__(self, client: Optional[RelayClient] = None, *,
                 direct_factory: Callable = PCSCBridge,
                 direct_present: Callable[[str], bool] = pcsc_reader_present) -> None:
        self.client = client or RelayClient()
        self._direct_factory = direct_factory
        self._direct_present = direct_present
        self.mode = "direct"                  # 最後につないだ読み取り元（"relay" / "direct"）

    def bridge(self, reader_name: str, reader_index: int = 0) -> object:
        if self.client.available():
            self.mode = "relay"
            return RelayBridge(reader_name, reader_index, self.client)
        self.mode = "direct"
        return call_bridge_factory(self._direct_factory, reader_name, reader_index)

    def present(self, reader_name: str) -> bool:
        return self.client.available() or self._direct_present(reader_name)
