"""api/server.py

M1 viewer API server (ADR-0013, docs/contracts/viewer-api.md)。

FastAPI app factory + uvicorn 起動。読み取り専用 (GET のみ)。error は
docs/contracts/error-shapes.md の論理形 {"code", "message"} をそのまま HTTP body にする。
bind 既定は 127.0.0.1（無認証のため。LAN 公開は config で明示変更, ISSUE-0013）。

要 `pip install ".[api]"`（fastapi/uvicorn）。import 失敗は呼び出し側 (main.py) が
警告して落とさない（pokerkit fallback と同方針）。
"""
from __future__ import annotations

import logging
from importlib import metadata
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from api.read_models import (
    HandNotFoundError,
    get_hand,
    get_player_session_ledger,
    list_player_hands,
    list_player_sessions,
)
from core.ledger_repository import LedgerRepository
from core.menu import MenuMaster
from core.order_request_repository import (
    AlreadyResolvedError,
    InvalidOrderRequestError,
    OrderRequestNotFoundError,
    OrderRequestRepository,
    OrderSessionClosedError,
    OrderUnknownPlayerError,
)
from core.player_repository import PlayerNotFoundError, PlayerRepository
from core.session_repository import SessionNotFoundError, SessionRepository

logger = logging.getLogger(__name__)


def _app_version() -> str:
    try:
        return metadata.version("pokerapp")
    except metadata.PackageNotFoundError:
        return "unknown"


class _OrderRequestBody(BaseModel):
    """POST /order-requests の body（viewer-api.md）。"""

    item_name: str
    quantity: int
    note: str | None = None


def create_app(
    player_repo: PlayerRepository,
    session_repo: SessionRepository,
    log_dir: str | Path,
    ledger_repo: LedgerRepository | None = None,
    order_repo: OrderRequestRepository | None = None,
    menu: MenuMaster | None = None,
    orders_writable: bool = False,
) -> FastAPI:
    """viewer API の FastAPI app を構築する（repository は DI, ADR-0008 の流儀）。

    ledger_repo / order_repo / menu 省略時は既定ファイルから構築する（M4/M5, ADR-0014/0015）。
    orders_writable=False（単独 --viewer-api の read-only モード）では注文 POST を
    503 `orders_unavailable` で拒否する（単一プロセス所有, ADR-0015 §3）。
    """
    if ledger_repo is None:
        ledger_repo = LedgerRepository(session_repo=session_repo)
    if order_repo is None:
        order_repo = OrderRequestRepository(session_repo=session_repo)
    if menu is None:
        menu = MenuMaster()
    app = FastAPI(title="pokerapp viewer API", version=_app_version())

    # M1 は read-only GET のみのため全 origin を許可（Expo web client 用, viewer-api.md）。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET"],
        allow_headers=["*"],
    )

    @app.exception_handler(PlayerNotFoundError)
    @app.exception_handler(SessionNotFoundError)
    @app.exception_handler(HandNotFoundError)
    @app.exception_handler(OrderRequestNotFoundError)
    async def _not_found_handler(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=404, content={"code": "not_found", "message": str(exc)})

    # order_request 系の error → HTTP code 対応（viewer-api.md / error-shapes.md）
    _ORDER_ERROR_MAP: list[tuple[type, int, str]] = [
        (OrderUnknownPlayerError, 404, "unknown_player"),
        (OrderSessionClosedError, 409, "session_closed"),
        (AlreadyResolvedError, 409, "already_resolved"),
        (InvalidOrderRequestError, 400, "invalid_quantity"),
    ]
    for exc_type, http_status, code in _ORDER_ERROR_MAP:
        def _make_handler(http_status: int = http_status, code: str = code):
            async def _handler(request: Request, exc: Exception) -> JSONResponse:
                return JSONResponse(status_code=http_status,
                                    content={"code": code, "message": str(exc)})
            return _handler
        app.add_exception_handler(exc_type, _make_handler())

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok", "version": _app_version()}

    @app.get("/api/players")
    def players() -> dict:
        return {"players": [p.to_dict() for p in player_repo.list_players()]}

    @app.get("/api/players/{player_id}")
    def player(player_id: str) -> dict:
        return player_repo.get(player_id).to_dict()

    @app.get("/api/players/{player_id}/sessions")
    def player_sessions(player_id: str) -> dict:
        player_repo.get(player_id)
        return {"sessions": list_player_sessions(player_id, session_repo)}

    @app.get("/api/players/{player_id}/sessions/{session_id}/hands")
    def player_hands(player_id: str, session_id: str) -> dict:
        player_repo.get(player_id)
        return {"hands": list_player_hands(player_id, session_id, session_repo, log_dir)}

    @app.get("/api/sessions/{session_id}/hands/{hand_id}")
    def hand(session_id: str, hand_id: int) -> dict:
        return get_hand(session_id, hand_id, log_dir)

    @app.get("/api/players/{player_id}/sessions/{session_id}/ledger")
    def player_ledger(player_id: str, session_id: str) -> dict:
        player_repo.get(player_id)
        return get_player_session_ledger(player_id, session_id, ledger_repo)

    @app.get("/api/menu")
    def get_menu() -> dict:
        return {"items": menu.list_items()}

    @app.get("/api/players/{player_id}/sessions/{session_id}/order-requests")
    def list_order_requests(player_id: str, session_id: str) -> dict:
        player_repo.get(player_id)
        return {
            "requests": [
                r.to_dict()
                for r in order_repo.list_requests(session_id, player_id=player_id)
            ]
        }

    @app.post("/api/players/{player_id}/sessions/{session_id}/order-requests",
              status_code=201, response_model=None)
    def create_order_request(
        player_id: str, session_id: str, body: _OrderRequestBody
    ) -> "JSONResponse | dict":
        """注文リクエストを受け付ける（pending。ledger には書かない — ADR-0015 §2）。"""
        if not orders_writable:
            return JSONResponse(status_code=503, content={
                "code": "orders_unavailable",
                "message": "注文の受付はスタッフ会計画面（--ledger）の起動中のみ可能です。",
            })
        player_repo.get(player_id)
        if menu.unit_amount(body.item_name) is None:
            return JSONResponse(status_code=400, content={
                "code": "unknown_item",
                "message": f"item_name={body.item_name!r} はメニューにありません。",
            })
        request = order_repo.create_request(
            session_id, player_id, body.item_name, body.quantity, note=body.note,
        )
        return request.to_dict()

    return app


def run_server(cfg: dict) -> None:
    """config に従って viewer API を foreground で起動する（main.py --viewer-api）。"""
    import uvicorn

    api_cfg = cfg.get("viewer_api", {})
    bind_host = api_cfg.get("bind_host", "127.0.0.1")
    bind_port = api_cfg.get("bind_port", 8788)
    log_dir = cfg.get("session", {}).get("log_dir", "./logs")

    player_repo = PlayerRepository()
    session_repo = SessionRepository(player_repo=player_repo)
    ledger_repo = LedgerRepository(session_repo=session_repo)
    app = create_app(player_repo, session_repo, log_dir, ledger_repo=ledger_repo)

    logger.info("Starting viewer API on %s:%s (log_dir=%s)", bind_host, bind_port, log_dir)
    uvicorn.run(app, host=bind_host, port=bind_port)
