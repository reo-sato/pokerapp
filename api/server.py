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
import secrets
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
    list_measurement_rows,
    list_player_hands,
    list_player_sessions,
    list_session_hands,
)
from core.auth_identity_repository import AuthIdentityRepository
from core.auth_token import issue_player_token, verify_player_token
from core.control_queue import VALID_CONTROL_TYPES, ControlCommandLog
from core.ground_truth import (
    SOURCE_EDITED,
    SOURCE_PASSTHROUGH,
    GroundTruthError,
    hand_has_needs_review,
    validate_source,
)
from core.ground_truth_repository import GroundTruthRepository
from core.hand_correction_repository import (
    HandCorrectionError,
    HandCorrectionRepository,
)
from core.oidc import OidcError, OidcProvider, resolve_player_for_claim
from core.ledger_repository import (
    AlreadySettledError,
    DuplicateGrantError,
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
from core.player_credential_repository import (
    PinLockedError,
    PinTooShortError,
    PlayerCredentialRepository,
)
from core.player_repository import (
    DuplicateDisplayNameError,
    EmptyDisplayNameError,
    PlayerMergeError,
    PlayerNotFoundError,
    PlayerRepository,
    PlayerValidationError,
)
from core.session_repository import (
    InvalidSeatError,
    PlayerAlreadySeatedError,
    SeatTakenError,
    SessionAlreadyClosedError,
    SessionClosedError,
    SessionError,
    SessionNotFoundError,
    SessionRepository,
    UnknownPlayerError as SessionUnknownPlayerError,
)
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


class _LoginBody(BaseModel):
    """POST /api/auth/login の body（L1 PIN, ADR-0027）。"""

    player_id: str
    pin: str


class _SetPinBody(BaseModel):
    """POST /api/players/{id}/pin の body。current_pin は変更時の本人確認用。"""

    pin: str
    current_pin: str | None = None


class _StaffMergeBody(BaseModel):
    """POST /api/staff/players/merge の body（player merge, ADR-0030）。"""

    survivor_id: str
    absorbed_id: str


class _StaffPointGrantBody(BaseModel):
    """POST /api/staff/players/{id}/point-grants の body（ADR-0038 §A）。"""

    delta_points: int
    reason: str = "manual_grant"
    session_id: str | None = None
    idempotency_key: str | None = None


class _StaffSessionCreateBody(BaseModel):
    """POST /api/staff/sessions の body（ADR-0038 §B）。"""

    label: str | None = None
    blinds: dict | None = None


class _StaffPlayerCreateBody(BaseModel):
    """POST /api/staff/players の body（ADR-0038 §B）。"""

    display_name: str


class _StaffPlayerRenameBody(BaseModel):
    """PUT /api/staff/players/{id} の body（ADR-0038 §B）。"""

    display_name: str


class _SeatAssignItem(BaseModel):
    seat_no: int
    player_id: str


class _StaffSeatAssignBody(BaseModel):
    """PUT /api/staff/sessions/{sid}/hands/{hid}/seats の body（ADR-0038 §B）。

    指定 hand に seat→player を割り当てる（append。conflict は error）。
    """

    assignments: list[_SeatAssignItem]


class _StaffControlBody(BaseModel):
    """POST /api/staff/sessions/{sid}/control の body（hand logger 遠隔制御, ADR-0039）。"""

    type: str
    seat: int | None = None
    amount: int | None = None


class _OidcExchangeBody(BaseModel):
    """POST /api/auth/{provider}/exchange の body（L2, ADR-0031 D4）。"""

    code: str
    nonce: str | None = None


class _HandCorrectionBody(BaseModel):
    """POST /api/staff/.../hands/{hid}/corrections の body（ハンド訂正, ADR-0036）。"""

    field: str
    new_value: object = None
    action_index: int | None = None
    corrected_by: str | None = None
    note: str | None = None


class _GroundTruthBody(BaseModel):
    """PUT /api/staff/.../ground-truth/{hid} の body（Phase A 計測, ADR-0043）。

    `source="captured-passthrough"` の時は `hand` 不要（server が訂正適用後の captured を
    そのまま GT に書く）。`source="manual-edit"` の時は `hand` 必須（annotator 編集後の
    board/actions/players/winner_seat/notes を含む）。
    """

    source: str
    annotator: str = "staff"
    hand: dict | None = None


# ledger / settlement の error → (HTTP status, error code)。staff write で再利用する
# （error-shapes.md の ledger セクションと 1:1, ADR-0021）。具体例外を先に並べる。
_LEDGER_ERROR_MAP: list[tuple[type, int, str]] = [
    (LedgerNotFoundError, 404, "not_found"),
    (LedgerUnknownPlayerError, 404, "unknown_player"),
    (EntryFeeRequiresCashError, 400, "entry_fee_requires_cash"),
    (InsufficientPointsError, 400, "insufficient_points"),
    (DuplicateGrantError, 409, "duplicate_grant"),
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


# session/seat の error → (HTTP status, error code)（error-shapes.md の session セクションと 1:1,
# ADR-0038 §B）。SessionNotFoundError は基底 SessionError の subclass なので先頭で拾う。
_SESSION_ERROR_MAP: list[tuple[type, int, str]] = [
    (SessionNotFoundError, 404, "not_found"),
    (SessionAlreadyClosedError, 409, "already_closed"),
    (SessionClosedError, 409, "session_closed"),
    (SeatTakenError, 409, "seat_taken"),
    (PlayerAlreadySeatedError, 409, "player_already_seated"),
    (SessionUnknownPlayerError, 404, "unknown_player"),
    (InvalidSeatError, 400, "invalid_seat"),
]


def _map_session_error(exc: SessionError) -> JSONResponse:
    for exc_type, http_status, code in _SESSION_ERROR_MAP:
        if isinstance(exc, exc_type):
            return JSONResponse(status_code=http_status,
                                content={"code": code, "message": str(exc)})
    return JSONResponse(status_code=400, content={"code": "invalid_seat", "message": str(exc)})


# player registry の error → (HTTP status, error code)（ADR-0038 §B）。PlayerNotFoundError も
# PlayerValidationError の subclass なので、ここで not_found に明示マップする。
_PLAYER_ERROR_MAP: list[tuple[type, int, str]] = [
    (PlayerNotFoundError, 404, "not_found"),
    (EmptyDisplayNameError, 400, "empty_display_name"),
    (DuplicateDisplayNameError, 400, "duplicate_display_name"),
    (PlayerMergeError, 400, "invalid_merge"),
]


def _map_player_error(exc: PlayerValidationError) -> JSONResponse:
    for exc_type, http_status, code in _PLAYER_ERROR_MAP:
        if isinstance(exc, exc_type):
            return JSONResponse(status_code=http_status,
                                content={"code": code, "message": str(exc)})
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
    credential_repo: PlayerCredentialRepository | None = None,
    player_auth: str = "off",
    player_token_secret: str | None = None,
    player_token_ttl_sec: int = 43_200,
    pin_self_enroll: bool = False,
    oidc_providers: "dict[str, OidcProvider] | None" = None,
    identity_repo: AuthIdentityRepository | None = None,
    correction_repo: HandCorrectionRepository | None = None,
    ground_truth_repo: GroundTruthRepository | None = None,
) -> FastAPI:
    """viewer API の FastAPI app を構築する（repository は DI, ADR-0008 の流儀）。

    ledger_repo / order_repo / menu 省略時は既定ファイルから構築する（ledger=ADR-0016,
    orders=ADR-0018）。orders_writable=False（単独 --viewer-api の read-only モード）では
    注文 POST を 503 `orders_unavailable` で拒否する（単一プロセス所有, ADR-0018 §3）。

    staff_token を設定すると `/api/staff/...` のスタッフ会計エンドポイントが
    `Authorization: Bearer <token>` で有効になる（ADR-0021）。falsy なら staff write は
    403 `staff_writes_disabled`。staff write（need_write）は orders_writable を所有する
    プロセスのみ（単一書き手, ADR-0020）。

    player_auth（L1 PIN, ADR-0027）: 'off'（既定 = name-pick, 後方互換）/ 'optional'
    （PIN 登録済 player の write のみ本人トークンを要求）/ 'required'（全 player write に
    本人トークンを要求）。PIN 検証成功で stateless 署名トークン（player_token_secret、
    未設定なら起動ごとに ephemeral 生成）を発行し、self-write の principal を解決する。
    """
    if ledger_repo is None:
        ledger_repo = LedgerRepository(session_repo=session_repo, player_repo=player_repo)
    if order_repo is None:
        order_repo = OrderRequestRepository(session_repo=session_repo, player_repo=player_repo)
    if menu is None:
        menu = MenuMaster()
    if credential_repo is None:
        credential_repo = PlayerCredentialRepository()
    if identity_repo is None:
        identity_repo = AuthIdentityRepository()
    if correction_repo is None:
        correction_repo = HandCorrectionRepository()
    if ground_truth_repo is None:
        ground_truth_repo = GroundTruthRepository(log_dir)
    _oidc_providers = oidc_providers or {}
    # player トークン署名鍵。未設定なら ephemeral（再起動でトークン失効, ADR-0027 D3）。
    _player_secret = player_token_secret or secrets.token_hex(32)
    app = FastAPI(title="pokerapp viewer API", version=_app_version())

    # Expo web client（mobile/ 注文 POST・staff/ 会計 write）が別 origin から叩けるよう、
    # 書き込みメソッドも許可する（LAN 限定 + token 認可前提, ADR-0021/0038）。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "PUT"],
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
        return {"hands": list_player_hands(
            player_id, session_id, session_repo, log_dir, correction_repo)}

    @app.get("/api/sessions/{session_id}/hands/{hand_id}")
    def hand(session_id: str, hand_id: int) -> dict:
        return get_hand(session_id, hand_id, log_dir, correction_repo)

    @app.get("/api/players/{player_id}/sessions/{session_id}/ledger")
    def player_ledger(player_id: str, session_id: str) -> dict:
        player_repo.get(player_id)
        return get_player_session_ledger(player_id, session_id, ledger_repo)

    @app.get("/api/menu")
    def get_menu() -> dict:
        return {"items": menu.list_items()}

    # ――― player 認証（L1 PIN, ADR-0027。principal 解決レイヤ）―――

    def _resolve_player_principal(request: Request) -> "str | None":
        """Authorization: Bearer <player token> を player_id に解決（無効なら None）。

        staff token は player token として検証に通らない（別形式・別 secret）ため、両者は
        安全に共存する。
        """
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            return None
        return verify_player_token(auth[len("Bearer "):], _player_secret)

    def _require_player(request: Request, player_id: str) -> "JSONResponse | None":
        """player self-write の principal ガード（player_auth に従う, ADR-0027 D4）。

        - off                       : 常に許可（name-pick, 後方互換）。
        - optional + PIN 未登録      : 許可（その player は従来どおり name-pick）。
        - それ以外                   : 本人トークン必須。principal != path player_id は 403。
        """
        if player_auth == "off":
            return None
        if player_auth == "optional" and not credential_repo.has_pin(player_id):
            return None
        principal = _resolve_player_principal(request)
        if principal is None:
            return JSONResponse(status_code=401, content={
                "code": "unauthorized",
                "message": "ログインが必要です（POST /api/auth/login で取得したトークンを Bearer で送る）。",
            })
        # merge 済みでも同一人物なら可（survivor/absorbed は canonical で同一視, ADR-0030）。
        if player_repo.resolve_canonical(principal) != player_repo.resolve_canonical(player_id):
            return JSONResponse(status_code=403, content={
                "code": "forbidden",
                "message": "他の player としては操作できません。",
            })
        return None

    @app.post("/api/auth/login", response_model=None)
    def auth_login(body: _LoginBody) -> "JSONResponse | dict":
        """PIN を検証し、成功なら player principal トークンを発行する。"""
        if player_auth == "off":
            return JSONResponse(status_code=403, content={
                "code": "player_auth_disabled",
                "message": "player 認証は無効です（viewer_api.player_auth=off）。",
            })
        player_repo.get(body.player_id)  # unknown player → 404 not_found
        try:
            ok = credential_repo.verify_pin(body.player_id, body.pin)
        except PinLockedError as e:
            return JSONResponse(status_code=429,
                                content={"code": "pin_locked", "message": str(e)})
        if not ok:
            return JSONResponse(status_code=401,
                                content={"code": "invalid_pin", "message": "PIN が違います。"})
        # principal は canonical（merge 済みなら survivor）で発行する（ADR-0030 D2）。
        canonical = player_repo.resolve_canonical(body.player_id)
        token, exp = issue_player_token(canonical, _player_secret, player_token_ttl_sec)
        return {"token": token, "expires_at": exp, "player_id": canonical}

    @app.post("/api/auth/{provider}/exchange", response_model=None)
    def auth_oidc_exchange(
        provider: str, body: _OidcExchangeBody
    ) -> "JSONResponse | dict":
        """外部 IdP の認可コードを交換し、player principal トークンを発行する（L2, ADR-0031 D4）。

        provider 未登録（実 IdP 未構築 / LAN 既定）なら 404 unknown_provider で挙動不変。
        """
        oidc = _oidc_providers.get(provider)
        if oidc is None:
            return JSONResponse(status_code=404, content={
                "code": "unknown_provider",
                "message": f"provider={provider!r} は構成されていません。",
            })
        try:
            claim = oidc.verify_code(body.code, nonce=body.nonce)
        except OidcError as e:
            return JSONResponse(status_code=401,
                                content={"code": "invalid_idp_code", "message": str(e)})
        pid = resolve_player_for_claim(claim, identity_repo, player_repo)
        token, exp = issue_player_token(pid, _player_secret, player_token_ttl_sec)
        return {"token": token, "expires_at": exp, "player_id": pid}

    @app.post("/api/players/{player_id}/pin", response_model=None)
    def set_player_pin(
        player_id: str, request: Request, body: _SetPinBody
    ) -> "JSONResponse | dict":
        """PIN を設定/変更する。

        - 初回設定: staff token、または `pin_self_enroll=true`（player 自身）で許可。
        - 変更: 現 PIN 一致、または staff token（reset）で許可。
        """
        if player_auth == "off":
            return JSONResponse(status_code=403, content={
                "code": "player_auth_disabled",
                "message": "player 認証は無効です（viewer_api.player_auth=off）。",
            })
        player_repo.get(player_id)  # unknown player → 404 not_found
        is_staff = bool(staff_token) and \
            request.headers.get("authorization", "") == f"Bearer {staff_token}"
        if credential_repo.has_pin(player_id):
            if not is_staff:
                try:
                    valid = bool(body.current_pin) and \
                        credential_repo.verify_pin(player_id, body.current_pin)
                except PinLockedError as e:
                    return JSONResponse(status_code=429,
                                        content={"code": "pin_locked", "message": str(e)})
                if not valid:
                    return JSONResponse(status_code=401, content={
                        "code": "unauthorized",
                        "message": "現在の PIN（または staff token）が必要です。",
                    })
        elif not is_staff and not pin_self_enroll:
            return JSONResponse(status_code=401, content={
                "code": "unauthorized",
                "message": "初回 PIN 設定には staff token が必要です（pin_self_enroll=false）。",
            })
        try:
            credential_repo.set_pin(player_id, body.pin)
        except PinTooShortError as e:
            return JSONResponse(status_code=400,
                                content={"code": "pin_too_short", "message": str(e)})
        return {"player_id": player_id, "pin_set": True}

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
        player_id: str, session_id: str, request: Request, body: _OrderRequestBody
    ) -> "JSONResponse | dict":
        """注文リクエストを受け付ける（pending。ledger には書かない — ADR-0018 §2）。"""
        if not orders_writable:
            return JSONResponse(status_code=503, content={
                "code": "orders_unavailable",
                "message": "注文の受付はスタッフ会計画面（--ledger）の起動中のみ可能です。",
            })
        auth_err = _require_player(request, player_id)
        if auth_err is not None:
            return auth_err
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

    @app.post("/api/staff/players/merge", response_model=None)
    def staff_merge_players(
        request: Request, body: _StaffMergeBody
    ) -> "JSONResponse | dict":
        """player merge（absorbed を survivor に統合, ADR-0030）。registry を書くので write 所有のみ。"""
        err = _staff_guard(request, need_write=True)
        if err is not None:
            return err
        try:
            absorbed = player_repo.merge_players(body.survivor_id, body.absorbed_id)
        except PlayerNotFoundError as e:
            return JSONResponse(status_code=404,
                                content={"code": "not_found", "message": str(e)})
        except PlayerMergeError as e:
            return JSONResponse(status_code=400,
                                content={"code": "invalid_merge", "message": str(e)})
        return {
            "survivor_id": player_repo.resolve_canonical(body.survivor_id),
            "absorbed_id": body.absorbed_id,
            "merged_into": absorbed.merged_into,
            "merged_at": absorbed.merged_at,
        }

    @app.post("/api/staff/sessions/{session_id}/hands/{hand_id}/corrections",
              response_model=None)
    def staff_add_hand_correction(
        session_id: str, hand_id: int, request: Request, body: _HandCorrectionBody
    ) -> "JSONResponse | dict":
        """ハンド訂正を 1 件追記する（append-only オーバーレイ, ADR-0036）。staff write。"""
        err = _staff_guard(request, need_write=True)
        if err is not None:
            return err
        try:
            hand = get_hand(session_id, hand_id, log_dir)  # 存在 + 範囲チェック用（元 hand）
        except HandNotFoundError as e:
            return JSONResponse(status_code=404,
                                content={"code": "not_found", "message": str(e)})
        if body.action_index is not None:
            actions = hand.get("actions") or []
            if not (0 <= body.action_index < len(actions)):
                return JSONResponse(status_code=400, content={
                    "code": "invalid_correction",
                    "message": f"action_index={body.action_index} が範囲外です（0..{len(actions) - 1}）。",
                })
        try:
            c = correction_repo.add_correction(
                session_id, hand_id, body.field, body.new_value,
                action_index=body.action_index,
                corrected_by=body.corrected_by or "staff", note=body.note,
            )
        except HandCorrectionError as e:
            return JSONResponse(status_code=400,
                                content={"code": "invalid_correction", "message": str(e)})
        return c.to_dict()

    @app.get("/api/staff/sessions/{session_id}/hands", response_model=None)
    def staff_session_hands(
        session_id: str, request: Request
    ) -> "JSONResponse | dict":
        """session の全 hand（訂正適用済, hand_id 昇順）。staff read（ADR-0044）。

        ハンドリプレイ UI の staff 導線用。player read と違い seat 縛りなしで卓の
        全ハンドを返す。log 不在は空 list。
        """
        err = _staff_guard(request, need_write=False)
        if err is not None:
            return err
        return {"hands": list_session_hands(session_id, log_dir, correction_repo)}

    # ――― Phase A 計測: ground truth（ADR-0043）―――

    @app.get("/api/staff/sessions/{session_id}/measurement-rows", response_model=None)
    def staff_measurement_rows(
        session_id: str, request: Request
    ) -> "JSONResponse | dict":
        """計測タブの一覧行（hand_id / winner / chip won / needs_review / GT 状態）。staff read。"""
        err = _staff_guard(request, need_write=False)
        if err is not None:
            return err
        return {
            "rows": list_measurement_rows(
                session_id, log_dir, ground_truth_repo, correction_repo
            )
        }

    @app.put("/api/staff/sessions/{session_id}/ground-truth/{hand_id}",
             response_model=None)
    def staff_upsert_ground_truth(
        session_id: str, hand_id: int, request: Request, body: _GroundTruthBody
    ) -> "JSONResponse | dict":
        """ground truth を 1 件 LWW 上書きする（ADR-0043）。staff write。

        passthrough: server が `get_hand()`（訂正適用済）を GT として書く。
        manual-edit: body.hand を GT として書く。

        C-2 ガード（ADR-0043 §3）: passthrough 時に hand が `needs_review` を含むなら 400。
        """
        err = _staff_guard(request, need_write=True)
        if err is not None:
            return err
        try:
            validate_source(body.source)
        except GroundTruthError as e:
            return JSONResponse(status_code=400,
                                content={"code": "invalid_amount", "message": str(e)})
        try:
            captured = get_hand(session_id, hand_id, log_dir, correction_repo)
        except HandNotFoundError as e:
            return JSONResponse(status_code=404,
                                content={"code": "not_found", "message": str(e)})
        if body.source == SOURCE_PASSTHROUGH:
            if hand_has_needs_review(captured):
                return JSONResponse(status_code=400, content={
                    "code": "invalid_amount",
                    "message": "needs_review を含むハンドは「✓ 流す」できません（ADR-0043 §3）。",
                })
            hand_body = captured
        elif body.source == SOURCE_EDITED:
            if not isinstance(body.hand, dict) or not body.hand:
                return JSONResponse(status_code=400, content={
                    "code": "invalid_amount",
                    "message": "source=manual-edit には hand が必要です。",
                })
            hand_body = body.hand
        else:  # validate_source で弾いた後の defensive branch
            return JSONResponse(status_code=400, content={
                "code": "invalid_amount",
                "message": f"unknown source: {body.source}",
            })
        entry = ground_truth_repo.upsert(
            session_id, hand_id, hand_body,
            annotator=body.annotator or "staff", source=body.source,
        )
        return entry.to_dict()

    @app.get("/api/staff/sessions/{session_id}/ground-truth/{hand_id}",
             response_model=None)
    def staff_get_ground_truth(
        session_id: str, hand_id: int, request: Request
    ) -> "JSONResponse | dict":
        """1 件の ground truth を返す（detail 画面の編集 prefill 用）。staff read。"""
        err = _staff_guard(request, need_write=False)
        if err is not None:
            return err
        gt = ground_truth_repo.get(session_id, hand_id)
        if gt is None:
            return JSONResponse(status_code=404, content={
                "code": "not_found",
                "message": f"hand_id={hand_id} に ground truth がありません。",
            })
        return gt.to_dict()

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

    @app.post("/api/staff/sessions/{session_id}/close", response_model=None)
    def staff_close_session(
        session_id: str, request: Request
    ) -> "JSONResponse | dict":
        """session を close する（精算確定の前提, B1）。reopen は提供しない。"""
        err = _staff_guard(request, need_write=True)
        if err is not None:
            return err
        try:
            session = session_repo.close_session(session_id)
        except SessionNotFoundError as e:
            return JSONResponse(status_code=404,
                                content={"code": "not_found", "message": str(e)})
        except SessionAlreadyClosedError as e:
            return JSONResponse(status_code=409,
                                content={"code": "already_closed", "message": str(e)})
        return session.to_dict()

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

    # ――― 会計の不足分（reversal / point grant, ADR-0038 §A）―――

    @app.get("/api/staff/sessions/{session_id}/ledger-entries", response_model=None)
    def staff_list_ledger_entries(
        session_id: str, request: Request
    ) -> "JSONResponse | dict":
        """session の ledger entry 一覧（reversal UI が取消対象を選ぶための read）。"""
        err = _staff_guard(request, need_write=False)
        if err is not None:
            return err
        return {"entries": [e.to_dict() for e in ledger_repo.list_entries(session_id)]}

    @app.post("/api/staff/ledger-entries/{entry_id}/reverse", response_model=None)
    def staff_reverse_entry(entry_id: str, request: Request) -> "JSONResponse | dict":
        """ledger entry を reversal で取り消す（append-only, ADR-0016）。"""
        err = _staff_guard(request, need_write=True)
        if err is not None:
            return err
        try:
            entry = ledger_repo.reverse_entry(entry_id)
        except LedgerError as e:
            return _map_ledger_error(e)
        return entry.to_dict()

    @app.post("/api/staff/players/{player_id}/point-grants", response_model=None)
    def staff_grant_points(
        player_id: str, request: Request, body: _StaffPointGrantBody
    ) -> "JSONResponse | dict":
        """point を付与する（manual_grant / result_credit / campaign_grant, ADR-0016）。"""
        err = _staff_guard(request, need_write=True)
        if err is not None:
            return err
        try:
            entry = ledger_repo.grant_points(
                player_id, body.delta_points,
                reason=body.reason or "manual_grant",
                session_id=body.session_id, idempotency_key=body.idempotency_key,
            )
        except LedgerError as e:
            return _map_ledger_error(e)
        except ValueError as e:  # 不正な reason
            return JSONResponse(status_code=400,
                                content={"code": "invalid_amount", "message": str(e)})
        return entry.to_dict()

    # ――― session / 座席 / player ライフサイクル（ADR-0038 §B）―――

    @app.get("/api/staff/sessions", response_model=None)
    def staff_list_sessions(request: Request) -> "JSONResponse | dict":
        """全 session 一覧（staff の卓選択用）。"""
        err = _staff_guard(request, need_write=False)
        if err is not None:
            return err
        return {"sessions": [s.to_dict() for s in session_repo.list_sessions()]}

    @app.post("/api/staff/sessions", status_code=201, response_model=None)
    def staff_create_session(
        request: Request, body: _StaffSessionCreateBody
    ) -> "JSONResponse | dict":
        """session を作成する（UUID4 採番, ADR-0007）。"""
        err = _staff_guard(request, need_write=True)
        if err is not None:
            return err
        s = session_repo.create_session(label=body.label, blinds=body.blinds)
        return JSONResponse(status_code=201, content=s.to_dict())


    @app.get("/api/staff/sessions/{session_id}/seating", response_model=None)
    def staff_seating(session_id: str, request: Request) -> "JSONResponse | dict":
        """現在の seating（最新 hand から導出）+ 記録済 hand_id 一覧。"""
        err = _staff_guard(request, need_write=False)
        if err is not None:
            return err
        try:
            seating = session_repo.current_seating(session_id)
            hand_ids = session_repo.list_hand_ids(session_id)
        except SessionError as e:
            return _map_session_error(e)
        return {
            "seating": [sa.to_dict() for sa in seating],
            "hand_ids": hand_ids,
        }

    @app.put("/api/staff/sessions/{session_id}/hands/{hand_id}/seats", response_model=None)
    def staff_assign_seats(
        session_id: str, hand_id: int, request: Request, body: _StaffSeatAssignBody
    ) -> "JSONResponse | dict":
        """指定 hand に seat→player を割り当てる（append。conflict は session error）。"""
        err = _staff_guard(request, need_write=True)
        if err is not None:
            return err
        results: list[dict] = []
        try:
            for a in body.assignments:
                sa = session_repo.assign_seat(session_id, hand_id, a.seat_no, a.player_id)
                results.append(sa.to_dict())
        except SessionError as e:
            return _map_session_error(e)
        return {"assignments": results}

    @app.get("/api/staff/players", response_model=None)
    def staff_list_players(request: Request) -> "JSONResponse | dict":
        """registry の全 player（canonical, ADR-0030）。"""
        err = _staff_guard(request, need_write=False)
        if err is not None:
            return err
        return {"players": [p.to_dict() for p in player_repo.list_players()]}

    @app.post("/api/staff/players", status_code=201, response_model=None)
    def staff_create_player(
        request: Request, body: _StaffPlayerCreateBody
    ) -> "JSONResponse | dict":
        err = _staff_guard(request, need_write=True)
        if err is not None:
            return err
        try:
            p = player_repo.create_player(body.display_name)
        except PlayerValidationError as e:
            return _map_player_error(e)
        return JSONResponse(status_code=201, content=p.to_dict())

    @app.put("/api/staff/players/{player_id}", response_model=None)
    def staff_rename_player(
        player_id: str, request: Request, body: _StaffPlayerRenameBody
    ) -> "JSONResponse | dict":
        err = _staff_guard(request, need_write=True)
        if err is not None:
            return err
        try:
            p = player_repo.rename_player(player_id, body.display_name)
        except PlayerValidationError as e:
            return _map_player_error(e)
        return p.to_dict()

    # ――― hand logger 遠隔制御（control queue, ADR-0039 §C）―――

    @app.post("/api/staff/sessions/{session_id}/control", status_code=201,
              response_model=None)
    def staff_hand_control(
        session_id: str, request: Request, body: _StaffControlBody
    ) -> "JSONResponse | dict":
        """hand logger に制御コマンド（new_hand / winner / rebuy）を append する。

        適用は hand logger プロセス（`hand_control.enabled` で起動した consumer）が行う。
        ここでは control queue に 1 行積むだけ（fire-and-forget）。
        """
        err = _staff_guard(request, need_write=True)
        if err is not None:
            return err
        if body.type not in VALID_CONTROL_TYPES:
            return JSONResponse(status_code=400, content={
                "code": "invalid_control",
                "message": f"未対応の control type です: {body.type!r}",
            })
        args: dict = {}
        if body.type == "winner":
            if not isinstance(body.seat, int):
                return JSONResponse(status_code=400, content={
                    "code": "invalid_control", "message": "winner には seat が必要です。"})
            args["seat"] = body.seat
        elif body.type == "rebuy":
            if not isinstance(body.seat, int) or not isinstance(body.amount, int) or body.amount <= 0:
                return JSONResponse(status_code=400, content={
                    "code": "invalid_control",
                    "message": "rebuy には seat と正の amount が必要です。"})
            args["seat"] = body.seat
            args["amount"] = body.amount
        control_log = ControlCommandLog(Path(log_dir) / f"{session_id}.control.jsonl")
        command = control_log.append(body.type, args)
        return JSONResponse(status_code=201, content=command.to_dict())

    # ――― sync API（ADR-0022。staff-token gate, state-based merge）―――

    def _sync_paths() -> dict[str, "Path"]:
        """このノードの各ストアファイルパス（repo の path property 由来）+ hand log dir。"""
        return {
            "players_path": player_repo.path,
            "sessions_path": session_repo.path,
            "ledger_path": ledger_repo.path,
            "orders_path": order_repo.path,
            "log_dir": Path(log_dir),  # hand log file-level union（ADR-0032）
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


def auth_kwargs_from_config(api_cfg: dict) -> dict:
    """`viewer_api` config から L1 PIN 認証（ADR-0027）の create_app kwargs を組み立てる。

    main.py の `--ledger`（in-process）と `--viewer-api`（standalone）で共通に使い、
    挙動が分岐しないようにする。既定は player_auth='off'（name-pick, 後方互換）。
    """
    return {
        "player_auth": api_cfg.get("player_auth", "off"),
        "player_token_secret": api_cfg.get("player_token_secret") or None,
        "player_token_ttl_sec": int(api_cfg.get("player_token_ttl_sec", 43_200)),
        "pin_self_enroll": bool(api_cfg.get("pin_self_enroll", False)),
        "credential_repo": PlayerCredentialRepository(
            iterations=int(api_cfg.get("pin_iterations", 210_000)),
            min_length=int(api_cfg.get("pin_min_length", 4)),
            max_attempts=int(api_cfg.get("pin_max_attempts", 5)),
            lockout_sec=int(api_cfg.get("pin_lockout_sec", 300)),
        ),
    }


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
    # player_auth が有効でも write（注文 POST）は 503 が先に返るが、login / PIN 設定は可能。
    app = create_app(player_repo, session_repo, log_dir, ledger_repo=ledger_repo,
                     staff_token=api_cfg.get("staff_token") or None,
                     **auth_kwargs_from_config(api_cfg))

    logger.info("Starting viewer API on %s:%s (log_dir=%s)", bind_host, bind_port, log_dir)
    uvicorn.run(app, host=bind_host, port=bind_port)
