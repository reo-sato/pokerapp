"""cloud/app.py — クラウドのアプリ（Cloud Run に置く, ADR-0067 D4）。

**許可した受け口だけを登録する**（店内の `api/server.py:create_app` に「クラウドでは無効」を足す方式ではない）。
スタッフ・会計を書く受け口・同期の snapshot/merge・名前選びは、このアプリに存在しない
（`tests/test_cloud_app.py` が受け口の一覧を固定する）。

フェーズ 2a の受け口:
- `GET  /api/health`                                 生きているか（データベースにつながるか）
- `POST /api/cloud/v1/sync/push`                     店舗 PC（端末トークン）→ 変わったレコード
- `GET  /api/cloud/v1/sync/status`                   店舗 PC（端末トークン）→ 件数と最大の seq
- `GET  /api/cloud/v1/admin/stores`                  運営（管理トークン）→ 店舗の一覧
- `POST /api/cloud/v1/admin/stores`                  運営 → 店舗を作り、端末トークンを発行
- `POST /api/cloud/v1/admin/stores/{id}/devices`     運営 → 端末トークンを発行（前のものを取り消すこともできる）
- `POST /api/cloud/v1/admin/stores/{id}/devices/revoke`  運営 → その店舗の端末トークンをすべて取り消す
管理の受け口は、管理トークン（環境変数 `CLOUD_ADMIN_TOKEN`、32 文字以上）が無ければ存在しない（404）。

フェーズ 2c の受け口（店外での閲覧, ADR-0067 D5）:
- `POST /api/cloud/v1/viewer-links`                  店舗 PC（端末トークン）→ その人の閲覧リンクを発行（前のものは取り消し）
- `POST /api/cloud/v1/viewer-links/revoke`           店舗 PC → その人の閲覧リンクを取り消す
- `POST /api/cloud/v1/auth/link`                     お客さん → リンクの乱数を 30 日のログインに換える
- お客さんの読み取り（ログイン必須・**自分だけ**。店内の viewer API と同じ形）:
  `GET /api/players/{id}` / `.../sessions` / `.../sessions/{sid}/hands` / `.../sessions/{sid}/ledger` /
  `GET /api/sessions/{sid}/hands/{hid}`（その人が座っていたハンドだけ）
- `/`（お客さん向け画面のクラウド用ビルド `cloud/static/player/`。あれば）
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from cloud import tokens
from cloud.ingest import PushError, ingest_push
from cloud.storage import Storage, StoreNotFound
from cloud.views import ViewCache
from api.read_models import (
    HandNotFoundError,
    get_hand,
    hide_hole_cards,
    hole_card_mode,
    viewer_seats,
    get_player_session_ledger,
    list_player_hands,
    list_player_sessions,
)
from core.auth_token import issue_player_token, verify_player_token
from core.ledger_repository import LedgerNotFoundError
from core.player_repository import PlayerNotFoundError
from core.session_repository import SessionNotFoundError

logger = logging.getLogger("cloud")

API_PREFIX = "/api/cloud/v1"
DEFAULT_MAX_BODY = 4 * 1024 * 1024
DEFAULT_LOGIN_TTL_SEC = 30 * 24 * 3600
PLAYER_WEB_DIR = Path(__file__).resolve().parent / "static" / "player"


@dataclass
class CloudSettings:
    admin_token: Optional[str] = None
    max_body_bytes: int = DEFAULT_MAX_BODY
    admin_rate: tuple[int, float] = (10, 60.0)          # 回数, 秒（IP ごと）
    auth_fail_rate: tuple[int, float] = (30, 60.0)      # 端末トークンの失敗
    link_rate: tuple[int, float] = (20, 60.0)           # 閲覧リンクの交換
    clock: Callable[[], float] = field(default=time.monotonic)
    token_secret: Optional[str] = None                  # お客さんのログインの署名（固定しないと再起動で全員ログアウト）
    login_ttl_sec: int = DEFAULT_LOGIN_TTL_SEC
    public_url: Optional[str] = None                    # リンクの URL の頭（無ければ要求の URL から）
    player_web_dir: Optional[Path] = None               # お客さん向け画面（無ければ配信しない）
    view_root: Optional[Path] = None                    # 読み取りの写しの置き場（無ければ一時ディレクトリ）
    wall_clock: Callable[[], float] = field(default=time.time)


class RateLimiter:
    """IP ごとの回数制限（インスタンスの中だけ。Cloud Run の最大 2 台なら目安として十分）。"""

    def __init__(self, limit: int, window: float, clock: Callable[[], float]) -> None:
        self.limit, self.window, self.clock = limit, window, clock
        self._hits: dict[str, deque] = {}
        self._lock = threading.Lock()

    def hit(self, key: str) -> bool:
        """数えて、まだ上限内なら True。"""
        now = self.clock()
        with self._lock:
            q = self._hits.setdefault(key, deque())
            while q and now - q[0] >= self.window:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            if len(self._hits) > 10000:   # 古い IP を捨てる（記憶が増え続けないように）
                for k in [k for k, v in self._hits.items() if not v]:
                    del self._hits[k]
            return True


def client_ip(request: Request) -> str:
    # Cloud Run は X-Forwarded-For の最後に接続元の IP を足す（前の値はお客さんが付けられる）
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else "?"


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"code": code, "message": message}, status_code=status)


class _Fail(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        self.status, self.code, self.message = status, code, message


async def _read_json(request: Request, limit: int) -> object:
    length = request.headers.get("content-length")
    if length is not None:
        try:
            if int(length) > limit:
                raise _Fail(413, "payload_too_large", f"本文は {limit} バイトまでです")
        except ValueError:
            raise _Fail(400, "invalid_payload", "Content-Length が不正です") from None
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise _Fail(413, "payload_too_large", f"本文は {limit} バイトまでです")
        chunks.append(chunk)
    try:
        return json.loads(b"".join(chunks) or b"null")
    except (ValueError, UnicodeDecodeError):
        raise _Fail(400, "invalid_payload", "本文が JSON ではありません") from None


def create_cloud_app(storage: Storage, settings: Optional[CloudSettings] = None) -> FastAPI:
    settings = settings or CloudSettings()
    # 説明のページ（/docs・/openapi.json）は出さない（攻撃の手がかりを減らす）
    app = FastAPI(title="pokerapp cloud", docs_url=None, redoc_url=None, openapi_url=None)
    admin_limit = RateLimiter(*settings.admin_rate, settings.clock)
    auth_fail_limit = RateLimiter(*settings.auth_fail_rate, settings.clock)
    admin_enabled = bool(settings.admin_token and len(settings.admin_token) >= tokens.MIN_ADMIN_TOKEN_LEN)
    if settings.admin_token and not admin_enabled:
        logger.warning("CLOUD_ADMIN_TOKEN が短すぎるので管理の受け口は無効です（%d 文字以上）",
                       tokens.MIN_ADMIN_TOKEN_LEN)

    @app.exception_handler(_Fail)
    async def _fail_handler(request: Request, exc: _Fail) -> JSONResponse:
        return _error(exc.status, exc.code, exc.message)

    @app.exception_handler(StarletteHTTPException)
    async def _http_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        if exc.status_code == 404:
            return _error(404, "not_found", "見つかりません")
        if exc.status_code == 405:
            return _error(405, "method_not_allowed", "この方法では使えません")
        return _error(exc.status_code, "error", str(exc.detail))

    @app.middleware("http")
    async def _headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    def _device(request: Request) -> dict:
        token = tokens.bearer(request.headers.get("authorization"))
        device = storage.find_device(tokens.hash_token(token)) if token else None
        if device is None or device.get("revoked_at"):
            if not auth_fail_limit.hit(client_ip(request)):
                raise _Fail(429, "too_many_requests", "しばらく待ってからやり直してください")
            raise _Fail(401, "unauthorized", "端末トークンが違うか、取り消されています")
        store = storage.get_store(device["store_id"])
        if store is None or store["status"] != "active":
            raise _Fail(403, "store_inactive", "この店舗は使えない状態です")
        return {**device, "store": store}

    def _admin(request: Request) -> None:
        if not admin_enabled:
            raise _Fail(404, "not_found", "見つかりません")
        if not admin_limit.hit(client_ip(request)):
            raise _Fail(429, "too_many_requests", "しばらく待ってからやり直してください")
        if not tokens.admin_token_ok(tokens.bearer(request.headers.get("authorization")), settings.admin_token):
            raise _Fail(401, "unauthorized", "管理トークンが違います")

    @app.get("/api/health")
    def health() -> JSONResponse:
        ok = storage.ping()
        return JSONResponse({"status": "ok" if ok else "degraded", "database": "ok" if ok else "error"},
                            status_code=200 if ok else 503)

    @app.post(f"{API_PREFIX}/sync/push")
    async def sync_push(request: Request) -> JSONResponse:
        # データベースを使う処理はスレッドで（待ち受けのループを止めない）
        device = await run_in_threadpool(_device, request)
        payload = await _read_json(request, settings.max_body_bytes)
        try:
            result = await run_in_threadpool(ingest_push, storage, device["store_id"], payload)
        except PushError as exc:
            raise _Fail(exc.status, exc.code, exc.message) from None
        await run_in_threadpool(storage.touch_device, device["device_id"])
        logger.info("push store=%s seq=%s results=%s rejected=%d", device["store_id"], result["seq"],
                    result["results"], len(result["rejected"]))
        return JSONResponse(result)

    @app.get(f"{API_PREFIX}/sync/status")
    def sync_status(request: Request) -> JSONResponse:
        device = _device(request)
        store = device["store"]
        return JSONResponse({"store_id": store["store_id"], "name": store["name"],
                             "counts": storage.counts(store["store_id"]),
                             "max_seq": storage.max_seq(store["store_id"])})

    @app.get(f"{API_PREFIX}/admin/stores")
    def admin_list_stores(request: Request) -> JSONResponse:
        _admin(request)
        return JSONResponse({"stores": storage.list_stores()})

    def _issue_device(store_id: str, label: str) -> tuple[dict, str]:
        token = tokens.new_device_token()
        device = storage.add_device(store_id, tokens.hash_token(token), label)
        return device, token

    @app.post(f"{API_PREFIX}/admin/stores")
    async def admin_create_store(request: Request) -> JSONResponse:
        await run_in_threadpool(_admin, request)
        body = await _read_json(request, 16 * 1024)
        name = body.get("name") if isinstance(body, dict) else None
        if not isinstance(name, str) or not name.strip() or len(name) > 100:
            raise _Fail(400, "invalid_payload", "name（店名, 100 文字まで）が必要です")
        label = body.get("device_label") or "店舗 PC"
        store = await run_in_threadpool(storage.create_store, name.strip())
        device, token = await run_in_threadpool(_issue_device, store["store_id"], str(label)[:100])
        logger.info("store created store=%s device=%s", store["store_id"], device["device_id"])
        return JSONResponse({"store": store, "device": device, "device_token": token}, status_code=201)

    @app.post(f"{API_PREFIX}/admin/stores/{{store_id}}/devices")
    async def admin_issue_device(store_id: str, request: Request) -> JSONResponse:
        await run_in_threadpool(_admin, request)
        body = await _read_json(request, 16 * 1024)
        body = body if isinstance(body, dict) else {}
        try:
            device, token = await run_in_threadpool(_issue_device, store_id, str(body.get("label") or "店舗 PC")[:100])
        except StoreNotFound:
            raise _Fail(404, "not_found", "店舗が見つかりません") from None
        revoked = 0
        if body.get("revoke_others"):
            revoked = await run_in_threadpool(storage.revoke_devices, store_id, device["device_id"])
        logger.info("device issued store=%s device=%s revoked=%d", store_id, device["device_id"], revoked)
        return JSONResponse({"device": device, "device_token": token, "revoked": revoked}, status_code=201)

    @app.post(f"{API_PREFIX}/admin/stores/{{store_id}}/devices/revoke")
    def admin_revoke_devices(store_id: str, request: Request) -> JSONResponse:
        _admin(request)
        if storage.get_store(store_id) is None:
            raise _Fail(404, "not_found", "店舗が見つかりません")
        revoked = storage.revoke_devices(store_id)
        logger.info("devices revoked store=%s n=%d", store_id, revoked)
        return JSONResponse({"revoked": revoked})

    # ――― 店外での閲覧（ADR-0067 D5, 2c）―――

    secret = settings.token_secret
    if not secret:
        import secrets as _secrets

        secret = _secrets.token_hex(32)
        logger.warning("CLOUD_TOKEN_SECRET が無いので一時的な鍵を使います（再起動するとお客さんのログインが切れます）")
    views = ViewCache(storage, settings.view_root)
    link_limit = RateLimiter(*settings.link_rate, settings.clock)

    def _player_id_arg(body: object) -> str:
        pid = body.get("player_id") if isinstance(body, dict) else None
        if not isinstance(pid, str) or not pid or len(pid) > 64:
            raise _Fail(400, "invalid_payload", "player_id が必要です")
        return pid

    def _public_base(request: Request) -> str:
        return (settings.public_url or str(request.base_url)).rstrip("/")

    @app.post(f"{API_PREFIX}/viewer-links")
    async def issue_viewer_link(request: Request) -> JSONResponse:
        device = await run_in_threadpool(_device, request)
        pid = _player_id_arg(await _read_json(request, 16 * 1024))
        store_id = device["store_id"]

        def work() -> tuple[dict, str]:
            if storage.get_record(store_id, "players", pid) is None:
                raise _Fail(404, "unknown_player", "この人はまだクラウドに届いていません（少し待ってからもう一度）")
            token = tokens.new_link_token()
            return storage.create_link(store_id, pid, tokens.hash_token(token)), token

        link, token = await run_in_threadpool(work)
        logger.info("viewer link issued store=%s link=%s", store_id, link["link_id"])
        return JSONResponse({"link_id": link["link_id"], "player_id": pid, "created_at": link["created_at"],
                             "url": f"{_public_base(request)}/#link={token}"}, status_code=201)

    @app.post(f"{API_PREFIX}/viewer-links/revoke")
    async def revoke_viewer_link(request: Request) -> JSONResponse:
        device = await run_in_threadpool(_device, request)
        pid = _player_id_arg(await _read_json(request, 16 * 1024))
        revoked = await run_in_threadpool(storage.revoke_links, device["store_id"], pid)
        logger.info("viewer links revoked store=%s n=%d", device["store_id"], revoked)
        return JSONResponse({"revoked": revoked})

    @app.post(f"{API_PREFIX}/auth/link")
    async def link_login(request: Request) -> JSONResponse:
        if not link_limit.hit(client_ip(request)):
            raise _Fail(429, "too_many_requests", "しばらく待ってからやり直してください")
        body = await _read_json(request, 4 * 1024)
        raw = body.get("link") if isinstance(body, dict) else None
        if not isinstance(raw, str) or not raw or len(raw) > 200:
            raise _Fail(400, "invalid_payload", "link が必要です")

        def work() -> dict:
            link = storage.find_link(tokens.hash_token(raw))
            if link is None or link.get("revoked_at"):
                raise _Fail(401, "invalid_link", "このリンクは使えません（取り消されたか、新しいリンクが出ています）。"
                                                 "お店で新しいリンクをもらってください")
            store = storage.get_store(link["store_id"])
            if store is None or store["status"] != "active":
                raise _Fail(403, "store_inactive", "このお店のページはいま使えません")
            storage.touch_link(link["link_id"])
            subject = f"{link['store_id']}:{link['link_id']}:{link['player_id']}"
            token, exp = issue_player_token(subject, secret, settings.login_ttl_sec, now=settings.wall_clock())
            player = storage.get_record(link["store_id"], "players", link["player_id"]) or {}
            return {"token": token, "player_id": link["player_id"], "expires_at": exp,
                    "display_name": player.get("display_name"), "store_name": store["name"]}

        return JSONResponse(await run_in_threadpool(work))

    def _principal(request: Request) -> dict:
        token = tokens.bearer(request.headers.get("authorization"))
        subject = verify_player_token(token or "", secret, now=settings.wall_clock())
        parts = subject.split(":") if subject else []
        if len(parts) != 3:
            raise _Fail(401, "unauthorized", "もう一度、お店でもらったリンクから開いてください")
        store_id, link_id, player_id = parts
        link = storage.get_link(link_id)
        if (link is None or link.get("revoked_at") or link["store_id"] != store_id
                or link["player_id"] != player_id):
            raise _Fail(401, "link_revoked", "このリンクは取り消されました。お店で新しいリンクをもらってください")
        store = storage.get_store(store_id)
        if store is None or store["status"] != "active":
            raise _Fail(403, "store_inactive", "このお店のページはいま使えません")
        return {"store_id": store_id, "player_id": player_id, "view": views.get(store_id)}

    def _own(request: Request, player_id: str) -> dict:
        """自分（merge で 1 人になった player を含む）でなければ 404（他人の有無も教えない）。"""
        me = _principal(request)
        players = me["view"].players
        try:
            same = players.resolve_canonical(player_id) == players.resolve_canonical(me["player_id"])
        except PlayerNotFoundError:
            same = False
        if not same:
            raise _Fail(404, "not_found", "見つかりません")
        return me

    _MISSING = (PlayerNotFoundError, SessionNotFoundError, LedgerNotFoundError, HandNotFoundError)

    @app.get("/api/players/{player_id}")
    def player(player_id: str, request: Request) -> JSONResponse:
        me = _own(request, player_id)
        try:
            return JSONResponse(me["view"].players.get(player_id).to_dict())
        except _MISSING:
            raise _Fail(404, "not_found", "見つかりません") from None

    @app.get("/api/players/{player_id}/sessions")
    def player_sessions(player_id: str, request: Request) -> JSONResponse:
        me = _own(request, player_id)
        return JSONResponse({"sessions": list_player_sessions(player_id, me["view"].sessions)})

    def _visible(view, session_id: str, hand: dict, player_id: str) -> dict:
        """公開範囲（セッションの設定 > 店舗の設定）に合わせて手札を隠す（ADR-0067 D6, 店内と同じ関数）。"""
        mode = hole_card_mode(view.sessions, session_id, view.settings.get("hole_cards", "all"))
        return hide_hole_cards(hand, viewer_seats(view.sessions, session_id, hand["hand_id"], player_id), mode)

    @app.get("/api/players/{player_id}/sessions/{session_id}/hands")
    def player_hands(player_id: str, session_id: str, request: Request) -> JSONResponse:
        view = _own(request, player_id)["view"]
        view.ensure_hands(session_id)
        try:
            hands = list_player_hands(player_id, session_id, view.sessions, view.log_dir, view.corrections)
        except _MISSING:
            raise _Fail(404, "not_found", "見つかりません") from None
        return JSONResponse({"hands": [_visible(view, session_id, h, player_id) for h in hands]})

    @app.get("/api/sessions/{session_id}/hands/{hand_id}")
    def hand(session_id: str, hand_id: int, request: Request) -> JSONResponse:
        me = _principal(request)
        view = me["view"]
        try:
            seated = set(view.sessions.resolve_seat_map_for_hand(session_id, hand_id).values())
            mine = view.players.equivalence_class(me["player_id"])
        except _MISSING:
            raise _Fail(404, "not_found", "見つかりません") from None
        except Exception:  # noqa: BLE001 — 座席の記録が無いハンドは見せない
            raise _Fail(404, "not_found", "見つかりません") from None
        if not (seated & mine):
            raise _Fail(404, "not_found", "見つかりません")   # 座っていなかったハンドは見せない
        view.ensure_hands(session_id)
        try:
            found = get_hand(session_id, hand_id, view.log_dir, view.corrections)
            return JSONResponse(_visible(view, session_id, found, me["player_id"]))
        except _MISSING:
            raise _Fail(404, "not_found", "見つかりません") from None

    @app.get("/api/players/{player_id}/sessions/{session_id}/ledger")
    def player_ledger(player_id: str, session_id: str, request: Request) -> JSONResponse:
        view = _own(request, player_id)["view"]
        try:
            return JSONResponse(get_player_session_ledger(player_id, session_id, view.ledger))
        except _MISSING:
            raise _Fail(404, "not_found", "見つかりません") from None

    web_dir = settings.player_web_dir
    if web_dir is not None and (Path(web_dir) / "index.html").is_file():
        from api.server import _PlayerWebFiles

        app.mount("/", _PlayerWebFiles(directory=str(web_dir), html=True), name="player-web")

    return app
