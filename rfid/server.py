from __future__ import annotations

import logging
import threading
import time
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

    def __init__(self, host: str, port: int, on_event: RFIDEventCallback) -> None:
        self._host = host
        self._port = port
        self._on_event = on_event
        self._start_time: float = 0.0
        self._wsgi_server = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Flask サーバーをデーモンスレッドで起動する。"""
        from werkzeug.serving import make_server

        app = self._create_app()
        self._wsgi_server = make_server(self._host, self._port, app)
        self._start_time = time.monotonic()
        self._thread = threading.Thread(
            target=self._wsgi_server.serve_forever,
            name="RFIDFlaskServer",
            daemon=True,
        )
        self._thread.start()
        logger.info("RFIDFlaskServer started on %s:%d", self._host, self._port)

    def stop(self) -> None:
        """サーバーを停止する。"""
        if self._wsgi_server is not None:
            self._wsgi_server.shutdown()
            logger.info("RFIDFlaskServer stopped")

    def _create_app(self):
        """Flask アプリケーションを生成して返す。

        POST /rfid  : JSON ボディを検証し on_event を呼び出す。
                      必須キー: reader_id, tag_id, timestamp, event_type
                      成功: 200 {"ok": true}
                      バリデーションエラー: 400 {"error": "..."}
        GET  /status: {"status": "ok", "uptime": <float>}
        """
        from flask import Flask, jsonify, request

        app = Flask(__name__)
        # werkzeug アクセスログを抑制
        logging.getLogger("werkzeug").setLevel(logging.ERROR)

        @app.route("/rfid", methods=["POST"])
        def rfid_event():
            data = request.get_json(silent=True)
            if not data:
                return jsonify({"error": "JSON body required"}), 400

            required = {"reader_id", "tag_id", "timestamp", "event_type"}
            missing = required - set(data.keys())
            if missing:
                return jsonify({"error": f"Missing fields: {sorted(missing)}"}), 400

            if data["event_type"] not in ("present", "absent"):
                return jsonify({"error": "event_type must be 'present' or 'absent'"}), 400

            try:
                self._on_event(data)
            except Exception as exc:
                logger.exception("Error in on_event callback: %s", exc)
                return jsonify({"error": "Internal error"}), 500

            return jsonify({"ok": True}), 200

        @app.route("/status", methods=["GET"])
        def status():
            uptime = time.monotonic() - self._start_time
            return jsonify({"status": "ok", "uptime": uptime}), 200

        return app
