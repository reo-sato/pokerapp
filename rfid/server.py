from __future__ import annotations

import logging
import threading
from typing import Callable

logger = logging.getLogger(__name__)

# コールバック型: {"reader_id": str, "tag_id": str, "timestamp": str, "event_type": str}
RFIDEventCallback = Callable[[dict], None]


class RFIDFlaskServer:
    """ESP32 からの HTTP POST を受信する Flask サーバー（spec.md FR-06）。

    エンドポイント:
        POST /rfid   → on_event コールバックを呼び出す
        GET  /status → {"status": "ok", "uptime": <秒数>}

    使い方:
        def on_event(data: dict) -> None:
            print(data)

        server = RFIDFlaskServer("0.0.0.0", 8787, on_event)
        server.start()
        # ...
        server.stop()

    config.json の rfid.bind_host / rfid.bind_port から値を渡すこと。
    """

    _host: str
    _port: int
    _on_event: RFIDEventCallback
    _thread: threading.Thread
    _stop_event: threading.Event

    def __init__(self, host: str, port: int, on_event: RFIDEventCallback) -> None: ...

    def start(self) -> None:
        """Flask サーバーをデーモンスレッドで起動する。"""
        ...

    def stop(self) -> None:
        """サーバーを停止する。"""
        ...

    def _create_app(self):
        """Flask アプリケーションを生成して返す。

        POST /rfid  : JSON ボディを検証し on_event を呼び出す。
                      必須キー: reader_id, tag_id, timestamp, event_type
                      成功: 200 {"ok": true}
                      バリデーションエラー: 400 {"error": "..."}
        GET  /status: {"status": "ok", "uptime": <float>}
        """
        ...
