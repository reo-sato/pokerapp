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
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from cloud import tokens
from cloud.ingest import PushError, ingest_push
from cloud.storage import Storage, StoreNotFound

logger = logging.getLogger("cloud")

API_PREFIX = "/api/cloud/v1"
DEFAULT_MAX_BODY = 4 * 1024 * 1024


@dataclass
class CloudSettings:
    admin_token: Optional[str] = None
    max_body_bytes: int = DEFAULT_MAX_BODY
    admin_rate: tuple[int, float] = (10, 60.0)          # 回数, 秒（IP ごと）
    auth_fail_rate: tuple[int, float] = (30, 60.0)      # 端末トークンの失敗
    clock: Callable[[], float] = field(default=time.monotonic)


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

    return app
