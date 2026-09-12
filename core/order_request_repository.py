"""core/order_request_repository.py

Phase M5 (ADR-0018): 注文リクエストの永続化 + 状態遷移。

- **thread-safe**: `--ledger` プロセスでは viewer API スレッド（create）と GUI スレッド
  （confirm/reject/list）が同居するため、全公開メソッドを lock で保護する。
- **単一プロセス所有**: write は in-process API を抱えた `--ledger` プロセスのみ
  （ADR-0018 §3）。read-only consumer（単独 `--viewer-api`）のために読み込みは
  reload-on-read（mtime 検知）で他プロセスの write に追従する。
- 状態遷移は pending → confirmed | rejected のみ。confirm は ledger_entry (kind=order) を
  追記してからリンクする。

永続形: `order_requests.json` = {"requests": [order_request, ...]}（追記順、アトミックリネーム）。
"""
from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime
from pathlib import Path

from core.atomic_io import atomic_write_json, read_json_file
from core.ledger_repository import LedgerRepository
from core.order_request import OrderRequest
from core.player_repository import PlayerNotFoundError, PlayerRepository
from core.session_repository import SessionRepository

logger = logging.getLogger(__name__)

_DEFAULT_ORDER_DB = Path(__file__).parent.parent / "order_requests.json"

_MAX_QUANTITY = 99
_MAX_ITEM_NAME = 100
_MAX_NOTE = 200


class OrderRequestError(Exception):
    """order request 操作の基底例外。"""


class OrderRequestNotFoundError(OrderRequestError):
    """指定 request_id が存在しない（error code: not_found）。"""


class OrderSessionClosedError(OrderRequestError):
    """closed の session に注文しようとした（error code: session_closed）。"""


class OrderUnknownPlayerError(OrderRequestError):
    """player_id が registry に実在しない（error code: unknown_player）。"""


class InvalidOrderRequestError(OrderRequestError):
    """quantity 範囲外 / item_name 空・過長 等（error code: invalid_quantity）。"""


class AlreadyResolvedError(OrderRequestError):
    """confirmed / rejected 済みの request を再度解決しようとした（error code: already_resolved）。"""


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class OrderRequestRepository:
    """注文リクエストの永続ストア（thread-safe / reload-on-read）。"""

    def __init__(
        self,
        path: str | Path | None = None,
        session_repo: SessionRepository | None = None,
        player_repo: PlayerRepository | None = None,
    ) -> None:
        self._path = Path(path) if path is not None else _DEFAULT_ORDER_DB
        # player 実在判定は session レイヤと同じ registry を共有する（名前空間ずれ防止）。
        # player_repo 明示 > session_repo の player_repo > 既定 registry の優先順。
        if player_repo is not None:
            self._player_repo = player_repo
        elif session_repo is not None:
            self._player_repo = session_repo.player_repo
        else:
            self._player_repo = PlayerRepository()
        self._session_repo = (
            session_repo
            if session_repo is not None
            else SessionRepository(player_repo=self._player_repo)
        )
        self._lock = threading.RLock()
        self._requests: list[OrderRequest] = []
        self._loaded_mtime: float | None = None
        self._reload()

    @property
    def path(self) -> Path:
        """この repository の永続ファイルパス（sync が snapshot/merge 対象を特定する用）。"""
        return self._path

    def reload(self) -> None:
        """ディスクから注文リクエストを再読込する（sync 後の最新化用, ADR-0022）。

        file-level merge（`core/sync.py`）が `order_requests.json` を書き換えた後、live プロセスの
        in-memory 状態を最新化するために呼ぶ。既存の reload-on-read 機構（`_reload`）に委譲する。
        """
        with self._lock:
            self._reload()

    # ――― 永続化 ―――

    def _reload(self) -> None:
        if not self._path.exists():
            self._loaded_mtime = None
            return
        try:
            mtime = self._path.stat().st_mtime
        except OSError:
            return
        data = read_json_file(self._path)  # 破損は退避して None（B7）
        if data is None:
            return  # 破損/読めない → 現状維持（破損は退避済み）
        requests: list[OrderRequest] = []
        for raw in data.get("requests", []):
            try:
                requests.append(OrderRequest.from_dict(raw))
            except (KeyError, TypeError):
                logger.warning("Skipping malformed order request record: %r", raw)
        self._requests = requests
        self._loaded_mtime = mtime

    def _maybe_reload(self) -> None:
        """別プロセスの write に追従する（mtime が進んでいたら読み直す）。"""
        try:
            mtime = self._path.stat().st_mtime if self._path.exists() else None
        except OSError:
            return
        if mtime != self._loaded_mtime:
            self._reload()

    def _flush(self) -> None:
        try:
            atomic_write_json(self._path, {"requests": [r.to_dict() for r in self._requests]})
            self._loaded_mtime = self._path.stat().st_mtime
        except OSError:
            logger.exception("Failed to write order request DB: %s", self._path)

    # ――― 操作 ―――

    def create_request(
        self,
        session_id: str,
        player_id: str,
        item_name: str,
        quantity: int,
        note: str | None = None,
    ) -> OrderRequest:
        """pending の注文リクエストを作る（ledger には書かない）。"""
        with self._lock:
            self._maybe_reload()
            session = self._session_repo.get_session(session_id)  # not_found を透過
            if session.status == "closed":
                raise OrderSessionClosedError(f"session_id={session_id} は closed です。")
            try:
                self._player_repo.get(player_id)
            except PlayerNotFoundError as e:
                raise OrderUnknownPlayerError(
                    f"player_id={player_id} は registry に存在しません。"
                ) from e
            item = (item_name or "").strip()
            if not item or len(item) > _MAX_ITEM_NAME:
                raise InvalidOrderRequestError(
                    f"item_name は 1〜{_MAX_ITEM_NAME} 文字が必要です。"
                )
            if not isinstance(quantity, int) or isinstance(quantity, bool) or \
                    not (1 <= quantity <= _MAX_QUANTITY):
                raise InvalidOrderRequestError(
                    f"quantity={quantity!r} は 1..{_MAX_QUANTITY} の整数が必要です。"
                )
            if note is not None and len(note) > _MAX_NOTE:
                raise InvalidOrderRequestError(f"note は {_MAX_NOTE} 文字以内が必要です。")

            request = OrderRequest(
                request_id=uuid.uuid4().hex,
                session_id=session_id,
                player_id=player_id,
                item_name=item,
                quantity=quantity,
                status="pending",
                requested_at=_now_iso(),
                note=note,
            )
            self._requests.append(request)
            self._flush()
            logger.info(
                "Order request created: %s %s x%d (session=%s player=%s)",
                request.request_id, item, quantity, session_id, player_id,
            )
            return request

    def get(self, request_id: str) -> OrderRequest:
        with self._lock:
            self._maybe_reload()
            for r in self._requests:
                if r.request_id == request_id:
                    return r
            raise OrderRequestNotFoundError(f"request_id={request_id} は存在しません。")

    def list_requests(
        self,
        session_id: str,
        player_id: str | None = None,
        status: str | None = None,
    ) -> list[OrderRequest]:
        """session の request を requested_at（追記）順で返す。

        player_id filter は merge を考慮し、survivor の equivalence class で突合する（ADR-0030 D2）。
        """
        with self._lock:
            self._maybe_reload()
            self._session_repo.get_session(session_id)  # not_found を透過
            cls = self._player_repo.equivalence_class(player_id) if player_id is not None else None
            return [
                r for r in self._requests
                if r.session_id == session_id
                and (cls is None or r.player_id in cls)
                and (status is None or r.status == status)
            ]

    def confirm_request(
        self, request_id: str, unit_amount: int, ledger_repo: LedgerRepository
    ) -> OrderRequest:
        """request を確定し、ledger_entry (kind=order) を追記してリンクする（スタッフ操作）。

        closed session への確定は OrderSessionClosedError（verify-v1 ledger は closed を
        拒否しないため、注文確定の不変条件はこの層で守る）。ledger 側の validation error
        （invalid_amount 等）は透過する。ledger 追記が成功してから status を変える
        （失敗時は pending のまま）。
        """
        with self._lock:
            self._maybe_reload()
            request = self.get(request_id)
            if request.status != "pending":
                raise AlreadyResolvedError(
                    f"request_id={request_id} は既に {request.status} です。"
                )
            session = self._session_repo.get_session(request.session_id)
            if session.status == "closed":
                raise OrderSessionClosedError(
                    f"session_id={request.session_id} は closed です。"
                )
            entry = ledger_repo.add_entry(
                request.session_id, request.player_id, "order",
                cash_amount=unit_amount * request.quantity,
                note=request.note,
                order={
                    "item_name": request.item_name,
                    "unit_amount": unit_amount,
                    "quantity": request.quantity,
                },
            )
            request.status = "confirmed"
            request.resolved_at = _now_iso()
            request.ledger_entry_id = entry.entry_id
            self._flush()
            logger.info("Order request confirmed: %s -> ledger %s",
                        request_id, entry.entry_id)
            return request

    def reject_request(self, request_id: str) -> OrderRequest:
        """request を却下する（スタッフ操作。ledger には何も書かない）。"""
        with self._lock:
            self._maybe_reload()
            request = self.get(request_id)
            if request.status != "pending":
                raise AlreadyResolvedError(
                    f"request_id={request_id} は既に {request.status} です。"
                )
            request.status = "rejected"
            request.resolved_at = _now_iso()
            self._flush()
            logger.info("Order request rejected: %s", request_id)
            return request
