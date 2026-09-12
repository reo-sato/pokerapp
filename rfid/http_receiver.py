"""rfid/http_receiver.py

ESP32-WROOM-32 から WiFi 経由で送られてくる RFID イベントを
HTTP POST で受信して RFIDEvent キューに投入するスレッド。

ESP32 が送る JSON 形式:
    POST /rfid  Content-Type: application/json
    {
        "reader_id":  "seat_3",
        "tag_id":     "04A1B2C3D4E5F6",
        "timestamp":  "2026-04-06T17:00:00.123"
    }

reader_id は config の rfid.readers dict のキーと一致させる:
    "seat_1" … "seat_9" : 座席カードリーダー (role="seat")
    "board_1" … "board_5": ボードカードリーダー (role="board")

board_index の意味:
    board_1=フロップ1枚目, board_2=フロップ2枚目, board_3=フロップ3枚目
    board_4=ターン, board_5=リバー

iPhone ブラウザからの閲覧用に GET /status エンドポイントも提供する。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from core.event_queue import EventQueue
from core.events import RFIDEvent
from rfid.card_master import CardMaster, normalize_tag_id

logger = logging.getLogger(__name__)

# RFID POST は数百 byte 程度。巨大 Content-Length による読み込み (OOM/DoS) を防ぐ上限。
MAX_CONTENT_LENGTH = 16 * 1024


class RFIDHTTPReceiver(threading.Thread):
    """ESP32 からの HTTP POST を受信して RFIDEvent をキューに投入するスレッド。

    スレッド安全: rfid_queue への put() のみ使用。stop() 後に join() 可能。

    設定例 (config_default.json):
        "rfid": {
            "enabled": true,
            "transport": "http",
            "bind_host": "192.168.x.x",   // ESP32 から届く PC の LAN IP（既定はローカルのみの 127.0.0.1）
            "bind_port": 8787,
            "card_master_file": "./rfid_cards.json",
            "readers": {
                "seat_1":  {"role": "seat",  "seat": 1},
                "seat_2":  {"role": "seat",  "seat": 2},
                "board_1": {"role": "board", "index": 1},
                "board_2": {"role": "board", "index": 2}
            }
        }
    """

    def __init__(
        self,
        rfid_queue: EventQueue,
        card_master: CardMaster,
        reader_configs: dict[str, dict],
        bind_host: str = "127.0.0.1",
        bind_port: int = 8787,
        stop_event: Optional[threading.Event] = None,
    ) -> None:
        super().__init__(daemon=True, name="RFIDHTTPReceiver")
        self._rfid_queue = rfid_queue
        self._card_master = card_master
        self._reader_configs = reader_configs
        self._bind_host = bind_host
        self._bind_port = bind_port
        self._stop_event = stop_event or threading.Event()

        # ステータス（GUI 表示用）
        self._events_received: int = 0
        self._last_event_time: Optional[float] = None
        self._server: Optional[ThreadingHTTPServer] = None

    # ――― 公開 API ―――

    def stop(self) -> None:
        self._stop_event.set()
        if self._server:
            self._server.shutdown()

    @property
    def status(self) -> dict:
        """GUI 表示用ステータス dict。メインスレッドから安全に読める。"""
        return {
            "running":          self._server is not None,
            "bind_host":        self._bind_host,
            "bind_port":        self._bind_port,
            "events_received":  self._events_received,
            "last_event_time":  self._last_event_time,
        }

    # ――― スレッドライフサイクル ―――

    def run(self) -> None:
        try:
            server = ThreadingHTTPServer(
                (self._bind_host, self._bind_port), _RFIDRequestHandler
            )
            server._receiver = self  # type: ignore[attr-defined]
            self._server = server
            logger.info(
                "RFIDHTTPReceiver listening on %s:%d",
                self._bind_host, self._bind_port,
            )
            server.serve_forever(poll_interval=0.5)
        except OSError as exc:
            logger.error(
                "RFIDHTTPReceiver failed to bind %s:%d — %s",
                self._bind_host, self._bind_port, exc,
            )
        finally:
            self._server = None
            logger.info("RFIDHTTPReceiver stopped")

    # ――― 内部: イベント生成（ハンドラから呼ばれる） ―――

    def _handle_post(self, body: bytes) -> tuple[int, str]:
        """受信 JSON を解析して RFIDEvent を生成する。

        Returns:
            (http_status_code, message)
        """
        # JSON パース
        try:
            data: dict = json.loads(body)
        except json.JSONDecodeError as exc:
            logger.warning("RFIDHTTPReceiver: invalid JSON — %s", exc)
            return 400, "invalid JSON"

        reader_id: str = data.get("reader_id", "")
        tag_id_raw: str = data.get("tag_id", "")
        timestamp_str: str = data.get("timestamp", "")

        # タグ ID 正規化
        try:
            tag_id = normalize_tag_id(tag_id_raw)
        except Exception:
            tag_id = tag_id_raw.upper()

        # reader_id 検証
        reader_cfg = self._reader_configs.get(reader_id)
        if reader_cfg is None:
            logger.warning(
                "RFIDHTTPReceiver: unknown reader_id=%r (tag=%s)", reader_id, tag_id
            )
            return 200, "unknown reader_id"   # 200 で返して ESP32 の再送を防ぐ

        # カードルックアップ
        card = self._card_master.lookup(tag_id)
        if not card:
            logger.warning(
                "RFIDHTTPReceiver: unregistered tag %s (reader=%s) — needs_review",
                tag_id, reader_id,
            )

        # タイムスタンプ解析
        ts = _parse_timestamp(timestamp_str)

        # ロール・席番号・ボードインデックス決定
        role: str = reader_cfg.get("role", "seat")
        seat: Optional[int] = reader_cfg.get("seat") if role == "seat" else None
        board_index: Optional[int] = reader_cfg.get("index") if role == "board" else None

        event = RFIDEvent(
            tag_id=tag_id,
            card=card,
            reader_id=reader_id,
            role=role,
            seat=seat,
            timestamp=ts,
            raw_tag_id=tag_id_raw,
            board_index=board_index,
        )
        self._rfid_queue.put(event)

        self._events_received += 1
        self._last_event_time = time.time()

        logger.info(
            "RFIDHTTPReceiver: received reader=%s tag=%s card=%r role=%s seat=%s board_index=%s",
            reader_id, tag_id, card or "(unknown)", role, seat, board_index,
        )
        return 200, "ok"


# ――― HTTP ハンドラ ―――

class _RFIDRequestHandler(BaseHTTPRequestHandler):
    """ThreadingHTTPServer に登録するリクエストハンドラ。"""

    def do_POST(self) -> None:  # noqa: N802
        if self.path not in ("/rfid", "/rfid/"):
            self._send_json(404, {"error": "not found"})
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            self._send_json(400, {"error": "invalid Content-Length"})
            return
        if length < 0 or length > MAX_CONTENT_LENGTH:
            self._send_json(413, {"error": "payload too large"})
            return
        body = self.rfile.read(length)

        receiver: RFIDHTTPReceiver = self.server._receiver  # type: ignore[attr-defined]
        status, msg = receiver._handle_post(body)
        self._send_json(status, {"status": msg})

    def do_GET(self) -> None:  # noqa: N802
        """GET /status — iPhone ブラウザから状態確認用。"""
        if self.path not in ("/status", "/status/"):
            self._send_json(404, {"error": "not found"})
            return
        receiver: RFIDHTTPReceiver = self.server._receiver  # type: ignore[attr-defined]
        payload = receiver.status.copy()
        if payload["last_event_time"]:
            payload["last_event_time"] = datetime.fromtimestamp(
                payload["last_event_time"]
            ).isoformat(timespec="milliseconds")
        self._send_json(200, payload)

    def _send_json(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: N802
        """デフォルトの print ログを logging に差し替える。"""
        logger.debug("HTTP %s - %s", self.address_string(), fmt % args)


# ――― ユーティリティ ―――

def _parse_timestamp(ts_str: str) -> float:
    """ISO 8601 文字列を UNIX タイムスタンプに変換する。失敗時は現在時刻。"""
    if not ts_str:
        return time.time()
    try:
        dt = datetime.fromisoformat(ts_str)
        # タイムゾーン情報がなければローカル時刻として扱う
        if dt.tzinfo is None:
            return dt.timestamp()
        return dt.astimezone(timezone.utc).timestamp()
    except ValueError:
        logger.debug("Could not parse timestamp %r, using current time", ts_str)
        return time.time()
