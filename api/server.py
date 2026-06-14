"""api/server.py

viewer API server (ADR-0017, docs/contracts/viewer-api.md)。

FastAPI app factory + uvicorn 起動。読み取り専用 (GET のみ)。error は
docs/contracts/error-shapes.md の論理形 {"code", "message"} をそのまま HTTP body にする。
bind 既定は 127.0.0.1（無認証のため。LAN 公開は config で明示変更, ISSUE-0019）。

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
from core.ledger_repository import (
    AlreadySettledError,
    EntryFeeRequiresCashError,
    InsufficientPointsError,
    InvalidAmountError,
    LedgerError,
    LedgerNotFoundError,
    LedgerRepository,
    SessionNotClosedError,
    UnknownPlayerError as LedgerUnknownPlayerError,
)
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
from core.sync import build_snapshot, merge_snapshot_into

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


class _StaffLedgerEntryBody(BaseModel):
    """POST /api/staff/.../ledger-entries の body（staff write API, ADR-0021）。"""

    player_id: str
    kind: str
    cash_amount: int = 0
    point_amount: int = 0
    note: str | None = None
    hand_id: int | None = None
    order: dict | None = None


class _StaffPaymentStatusBody(BaseModel):
    """PUT .../payment-status の body。"""

    status: str


class _StaffPaymentBody(BaseModel):
    """PUT .../payment の body（partial-paid 対応, ADR-0023）。"""

    paid_amount: int


class _StaffConfirmOrderBody(BaseModel):
    """POST /api/staff/order-requests/{id}/confirm の body。"""

    unit_amount: int


# ledger / settlement の error → (HTTP status, error code)。staff write で再利用する
# （error-shapes.md の ledger セクションと 1:1, ADR-0021）。具体例外を先に並べる。
_LEDGER_ERROR_MAP: list[tuple[type, int, str]] = [
    (LedgerNotFoundError, 404, "not_found"),
    (LedgerUnknownPlayerError, 404, "unknown_player"),
    (EntryFeeRequiresCashError, 400, "entry_fee_requires_cash"),
    (InsufficientPointsError, 400, "insufficient_points"),
    (InvalidAmountError, 400, "invalid_amount"),
    (SessionNotClosedError, 409, "session_not_closed"),
    (AlreadySettledError, 409, "already_settled"),
]


def _map_ledger_error(exc: LedgerError) -> JSONResponse:
    for exc_type, http_status, code in _LEDGER_ERROR_MAP:
        if isinstance(exc, exc_type):
            return JSONResponse(status_code=http_status,
                                content={"code": code, "message": str(exc)})
    # 基底 LedgerError（未分類）は invalid_amount 扱いにフォールバック。
    return JSONResponse(status_code=400, content={"code": "invalid_amount", "message": str(exc)})


def create_app(
    player_repo: PlayerRepository,
    session_repo: SessionRepository,
    log_dir: str | Path,
    ledger_repo: LedgerRepository | None = None,
    order_repo: OrderRequestRepository | None = None,
    menu: MenuMaster | None = None,
    orders_writable: bool = False,
    staff_token: str | None = None,
    buyin_presets: "list[int] | None" = None,
) -> FastAPI:
    """viewer API の FastAPI app を構築する（repository は DI, ADR-0008 の流儀）。

    ledger_repo / order_repo / menu 省略時は既定ファイルから構築する（ledger=ADR-0016,
    orders=ADR-0018）。orders_writable=False（単独 --viewer-api の read-only モード）では
    注文 POST を 503 `orders_unavailable` で拒否する（単一プロセス所有, ADR-0018 §3）。

    staff_token を設定すると `/api/staff/...` のスタッフ会計エンドポイントが
    `Authorization: Bearer <token>` で有効になる（ADR-0021）。falsy なら staff write は
    403 `staff_writes_disabled`。staff write（need_write）は orders_writable を所有する
    プロセスのみ（単一書き手, ADR-0020）。
    """
    if ledger_repo is None:
        ledger_repo = LedgerRepository(session_repo=session_repo, player_repo=player_repo)
    if order_repo is None:
        order_repo = OrderRequestRepository(session_repo=session_repo, player_repo=player_repo)
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
    @app.exception_handler(LedgerNotFoundError)
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
        """注文リクエストを受け付ける（pending。ledger には書かない — ADR-0018 §2）。"""
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

    # ――― staff write API（ADR-0021。Bearer token 認証 + 単一書き手）―――

    def _staff_guard(request: Request, *, need_write: bool) -> "JSONResponse | None":
        """スタッフ会計エンドポイントの認可ガード（ADR-0021）。

        - staff_token 未設定 → 403 staff_writes_disabled（運用で有効化していない）。
        - Authorization: Bearer <token> が無い/不一致 → 401 unauthorized。
        - need_write かつ orders_writable=False → 503 orders_unavailable
          （このプロセスは write を所有しない。単一書き手, ADR-0020）。
        - OK なら None。
        """
        if not staff_token:
            return JSONResponse(status_code=403, content={
                "code": "staff_writes_disabled",
                "message": "スタッフ会計 API は無効です（viewer_api.staff_token 未設定）。",
            })
        auth = request.headers.get("authorization", "")
        expected = f"Bearer {staff_token}"
        if auth != expected:
            return JSONResponse(status_code=401, content={
                "code": "unauthorized",
                "message": "スタッフトークンが無効です（Authorization: Bearer <token>）。",
            })
        if need_write and not orders_writable:
            return JSONResponse(status_code=503, content={
                "code": "orders_unavailable",
                "message": "会計 write はスタッフ会計画面（--ledger）の起動中のみ可能です。",
            })
        return None

    _buyin_presets = [int(a) for a in (buyin_presets or []) if int(a) > 0]

    @app.get("/api/staff/buyin-presets", response_model=None)
    def staff_buyin_presets(request: Request) -> "JSONResponse | dict":
        """buy-in 金額プリセット（config 由来, ADR-0026）。別端末スタッフ UI のメニュー用。"""
        err = _staff_guard(request, need_write=False)
        if err is not None:
            return err
        return {"presets": list(_buyin_presets)}

    @app.get("/api/staff/sessions/{session_id}/settlement", response_model=None)
    def staff_settlement(session_id: str, request: Request) -> "JSONResponse | dict":
        err = _staff_guard(request, need_write=False)
        if err is not None:
            return err
        try:
            rows = ledger_repo.compute_settlement(session_id)
        except LedgerError as e:
            return _map_ledger_error(e)
        return {"settlements": [s.to_dict() for s in rows]}

    @app.get("/api/staff/sessions/{session_id}/order-requests", response_model=None)
    def staff_order_requests(
        session_id: str, request: Request, status: str | None = None
    ) -> "JSONResponse | dict":
        err = _staff_guard(request, need_write=False)
        if err is not None:
            return err
        return {
            "requests": [
                r.to_dict()
                for r in order_repo.list_requests(session_id, status=status)
            ]
        }

    @app.post("/api/staff/sessions/{session_id}/ledger-entries",
              status_code=201, response_model=None)
    def staff_add_ledger_entry(
        session_id: str, request: Request, body: _StaffLedgerEntryBody
    ) -> "JSONResponse | dict":
        err = _staff_guard(request, need_write=True)
        if err is not None:
            return err
        try:
            entry = ledger_repo.add_entry(
                session_id, body.player_id, body.kind,
                cash_amount=body.cash_amount, point_amount=body.point_amount,
                note=body.note, hand_id=body.hand_id, order=body.order,
            )
        except LedgerError as e:
            return _map_ledger_error(e)
        except ValueError as e:  # invalid kind
            return JSONResponse(status_code=400,
                                content={"code": "invalid_amount", "message": str(e)})
        return JSONResponse(status_code=201, content=entry.to_dict())

    @app.post("/api/staff/sessions/{session_id}/settlement/commit", response_model=None)
    def staff_commit_settlement(
        session_id: str, request: Request
    ) -> "JSONResponse | dict":
        err = _staff_guard(request, need_write=True)
        if err is not None:
            return err
        try:
            rows = ledger_repo.commit_settlement(session_id)
        except LedgerError as e:
            return _map_ledger_error(e)
        return {"settlements": [s.to_dict() for s in rows]}

    @app.put("/api/staff/sessions/{session_id}/players/{player_id}/payment-status",
             response_model=None)
    def staff_set_payment_status(
        session_id: str, player_id: str, request: Request,
        body: _StaffPaymentStatusBody,
    ) -> "JSONResponse | dict":
        err = _staff_guard(request, need_write=True)
        if err is not None:
            return err
        try:
            settlement = ledger_repo.set_payment_status(session_id, player_id, body.status)
        except LedgerError as e:
            return _map_ledger_error(e)
        except ValueError as e:  # invalid status
            return JSONResponse(status_code=400,
                                content={"code": "invalid_amount", "message": str(e)})
        return settlement.to_dict()

    @app.put("/api/staff/sessions/{session_id}/players/{player_id}/payment",
             response_model=None)
    def staff_record_payment(
        session_id: str, player_id: str, request: Request,
        body: _StaffPaymentBody,
    ) -> "JSONResponse | dict":
        """受領額 paid_amount を記録し payment_status を導出する（partial-paid, ADR-0023）。"""
        err = _staff_guard(request, need_write=True)
        if err is not None:
            return err
        try:
            settlement = ledger_repo.record_payment(session_id, player_id, body.paid_amount)
        except LedgerError as e:
            return _map_ledger_error(e)
        return settlement.to_dict()

    @app.post("/api/staff/order-requests/{request_id}/confirm", response_model=None)
    def staff_confirm_order(
        request_id: str, request: Request, body: _StaffConfirmOrderBody
    ) -> "JSONResponse | dict":
        err = _staff_guard(request, need_write=True)
        if err is not None:
            return err
        try:
            req = order_repo.confirm_request(request_id, body.unit_amount, ledger_repo)
        except LedgerError as e:  # ledger 側 validation（invalid_amount 等）を透過
            return _map_ledger_error(e)
        return req.to_dict()

    @app.post("/api/staff/order-requests/{request_id}/reject", response_model=None)
    def staff_reject_order(
        request_id: str, request: Request
    ) -> "JSONResponse | dict":
        err = _staff_guard(request, need_write=True)
        if err is not None:
            return err
        return order_repo.reject_request(request_id).to_dict()

    # ――― sync API（ADR-0022。staff-token gate, state-based merge）―――

    def _sync_paths() -> dict[str, "Path"]:
        """このノードの各ストアファイルパス（repo の path property 由来）。"""
        return {
            "players_path": player_repo.path,
            "sessions_path": session_repo.path,
            "ledger_path": ledger_repo.path,
            "orders_path": order_repo.path,
        }

    @app.get("/api/staff/sync/snapshot", response_model=None)
    def staff_sync_snapshot(request: Request) -> "JSONResponse | dict":
        """このノードの全レコード snapshot を返す（peer が取り込む。read-only でも可）。"""
        err = _staff_guard(request, need_write=False)
        if err is not None:
            return err
        return build_snapshot(**_sync_paths())

    @app.post("/api/staff/sync/merge", response_model=None)
    async def staff_sync_merge(request: Request) -> "JSONResponse | dict":
        """peer snapshot を取り込み（state-based merge）、repo を reload して summary を返す。

        write 所有プロセス（orders_writable）のみ受理（read-only は 503）。
        """
        err = _staff_guard(request, need_write=True)
        if err is not None:
            return err
        peer = await request.json()
        summary = merge_snapshot_into(**_sync_paths(), peer=peer)
        # file-level merge 後、live プロセスの in-memory を最新化する（ADR-0022）。
        player_repo.reload()
        session_repo.reload()
        ledger_repo.reload()
        order_repo.reload()
        return summary

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
    # standalone --viewer-api は read-only（orders_writable=False）。staff_token があれば
    # staff *read*（settlement / order queue）は可能、staff *write* は 503（ADR-0021）。
    app = create_app(player_repo, session_repo, log_dir, ledger_repo=ledger_repo,
                     staff_token=api_cfg.get("staff_token") or None)

    logger.info("Starting viewer API on %s:%s (log_dir=%s)", bind_host, bind_port, log_dir)
    uvicorn.run(app, host=bind_host, port=bind_port)
