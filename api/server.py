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

from api.read_models import (
    HandNotFoundError,
    get_hand,
    list_player_hands,
    list_player_sessions,
)
from core.player_repository import PlayerNotFoundError, PlayerRepository
from core.session_repository import SessionNotFoundError, SessionRepository

logger = logging.getLogger(__name__)


def _app_version() -> str:
    try:
        return metadata.version("pokerapp")
    except metadata.PackageNotFoundError:
        return "unknown"


def create_app(
    player_repo: PlayerRepository,
    session_repo: SessionRepository,
    log_dir: str | Path,
) -> FastAPI:
    """viewer API の FastAPI app を構築する（repository は DI, ADR-0008 の流儀）。"""
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
    async def _not_found_handler(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=404, content={"code": "not_found", "message": str(exc)})

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
    app = create_app(player_repo, session_repo, log_dir)

    logger.info("Starting viewer API on %s:%s (log_dir=%s)", bind_host, bind_port, log_dir)
    uvicorn.run(app, host=bind_host, port=bind_port)
