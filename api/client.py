"""api/client.py

Phase S5 (ADR-0020): viewer API の Python クライアント（local↔API client 分離点）。

mobile の `HttpRepository`（`mobile/src/api/httpRepository.ts`）に相当する Python 実装。
viewer API（`api/server.py`）の read endpoints（+ 注文 GET/POST）を HTTP で呼び、非 2xx を
error-shape（`{"code","message"}`, `error-shapes.md`）の `code` を載せた `ViewerApiError` に
変換する。これにより「Python の別プロセス / 別 front-end が同じ HTTP 契約で読める」ことを実証し、
`tests/test_viewer_api_client.py` の round-trip 契約 test で API↔client の drift を検知する。

httpx を使う（`[api]` extra）。テストは ``httpx.Client(transport=ASGITransport(app=...))`` を
注入して in-process（ソケットなし）で round-trip する。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    import httpx


class ViewerApiError(Exception):
    """viewer API が返した error（error-shapes.md の code を保持）。

    分岐は ``code``（not_found / orders_unavailable / unknown_item / invalid_quantity /
    session_closed / already_resolved 等）、表示は ``message``。
    """

    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.status_code = status_code


class ViewerApiClient:
    """viewer API の read-only クライアント（+ 注文 GET/POST）。

    Args:
        base_url: API のベース URL（例 ``http://192.168.1.10:8788``）。
        client: 既存の ``httpx.Client``（テストで ASGITransport を注入する用）。
                None なら base_url で構築する。
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8788",
        client: "Optional[httpx.Client]" = None,
        staff_token: str | None = None,
        player_token: str | None = None,
    ) -> None:
        if client is not None:
            self._client = client
            self._owns_client = False
        else:
            import httpx

            self._client = httpx.Client(base_url=base_url.rstrip("/"))
            self._owns_client = True
        # staff write API（ADR-0021）用の Bearer token。staff_* メソッドのみで付与する。
        self._staff_token = staff_token
        # player principal token（L1 PIN, ADR-0027）。login() で取得・更新し、self-write に付与。
        self._player_token = player_token

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "ViewerApiClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ――― 内部 ―――

    def _request(
        self, method: str, path: str, json: Any = None,
        headers: "Optional[dict[str, str]]" = None,
    ) -> Any:
        resp = self._client.request(method, path, json=json, headers=headers)
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code >= 400:
            code = body.get("code", "unknown_error") if isinstance(body, dict) else "unknown_error"
            message = body.get("message", f"HTTP {resp.status_code}") if isinstance(body, dict) else ""
            raise ViewerApiError(code, message, resp.status_code)
        return body

    def _get(self, path: str) -> Any:
        return self._request("GET", path)

    # ――― read endpoints（mobile HttpRepository と対称）―――

    def health(self) -> dict:
        return self._get("/api/health")

    def list_players(self) -> list[dict]:
        return self._get("/api/players")["players"]

    def get_player(self, player_id: str) -> dict:
        return self._get(f"/api/players/{player_id}")

    def list_player_sessions(self, player_id: str) -> list[dict]:
        return self._get(f"/api/players/{player_id}/sessions")["sessions"]

    def list_player_hands(self, player_id: str, session_id: str) -> list[dict]:
        return self._get(f"/api/players/{player_id}/sessions/{session_id}/hands")["hands"]

    def get_hand(self, session_id: str, hand_id: int) -> dict:
        return self._get(f"/api/sessions/{session_id}/hands/{hand_id}")

    def get_player_session_ledger(self, player_id: str, session_id: str) -> dict:
        return self._get(f"/api/players/{player_id}/sessions/{session_id}/ledger")

    def get_menu(self) -> list[dict]:
        return self._get("/api/menu")["items"]

    def list_order_requests(self, player_id: str, session_id: str) -> list[dict]:
        return self._get(
            f"/api/players/{player_id}/sessions/{session_id}/order-requests"
        )["requests"]

    # ――― player 認証（L1 PIN, ADR-0027）―――

    def _player_headers(self) -> dict[str, str]:
        if not self._player_token:
            return {}
        return {"Authorization": f"Bearer {self._player_token}"}

    def login(self, player_id: str, pin: str) -> dict:
        """PIN でログインし、principal トークンを取得・保持する（POST /api/auth/login）。"""
        body = self._request("POST", "/api/auth/login",
                             json={"player_id": player_id, "pin": pin})
        self._player_token = body.get("token")
        return body

    def oidc_exchange(self, provider: str, code: str, nonce: str | None = None) -> dict:
        """外部 IdP の認可コードを交換し、principal トークンを取得・保持する（L2, ADR-0031）。"""
        payload: dict = {"code": code}
        if nonce is not None:
            payload["nonce"] = nonce
        body = self._request("POST", f"/api/auth/{provider}/exchange", json=payload)
        self._player_token = body.get("token")
        return body

    def set_pin(
        self, player_id: str, pin: str, current_pin: str | None = None
    ) -> dict:
        """PIN を設定/変更する。staff token があれば staff reset として送る（ADR-0027 D6）。"""
        payload: dict = {"pin": pin}
        if current_pin is not None:
            payload["current_pin"] = current_pin
        return self._request(
            "POST", f"/api/players/{player_id}/pin",
            json=payload, headers=self._staff_headers(),
        )

    # ――― 注文 write（所有プロセスのみ受理。read-only は 503 orders_unavailable）―――

    def create_order_request(
        self, player_id: str, session_id: str, item_name: str, quantity: int,
        note: str | None = None,
    ) -> dict:
        payload: dict = {"item_name": item_name, "quantity": quantity}
        if note is not None:
            payload["note"] = note
        return self._request(
            "POST", f"/api/players/{player_id}/sessions/{session_id}/order-requests",
            json=payload, headers=self._player_headers(),
        )

    # ――― staff write API（ADR-0021。Bearer token。会計 write は所有プロセスのみ）―――

    def _staff_headers(self) -> dict[str, str]:
        if not self._staff_token:
            return {}
        return {"Authorization": f"Bearer {self._staff_token}"}

    def get_buyin_presets(self) -> list[int]:
        """buy-in 金額プリセットを取得する（ADR-0026, staff token）。"""
        return self._request(
            "GET", "/api/staff/buyin-presets", headers=self._staff_headers(),
        )["presets"]

    def merge_players(self, survivor_id: str, absorbed_id: str) -> dict:
        """absorbed を survivor に統合する（player merge, ADR-0030, staff token）。"""
        return self._request(
            "POST", "/api/staff/players/merge",
            json={"survivor_id": survivor_id, "absorbed_id": absorbed_id},
            headers=self._staff_headers(),
        )

    def compute_settlement(self, session_id: str) -> list[dict]:
        return self._request(
            "GET", f"/api/staff/sessions/{session_id}/settlement",
            headers=self._staff_headers(),
        )["settlements"]

    def list_session_order_requests(
        self, session_id: str, status: str | None = None
    ) -> list[dict]:
        path = f"/api/staff/sessions/{session_id}/order-requests"
        if status is not None:
            path += f"?status={status}"
        return self._request("GET", path, headers=self._staff_headers())["requests"]

    def add_ledger_entry(
        self, session_id: str, player_id: str, kind: str,
        cash_amount: int = 0, point_amount: int = 0, note: str | None = None,
        hand_id: int | None = None, order: dict | None = None,
    ) -> dict:
        payload: dict = {
            "player_id": player_id, "kind": kind,
            "cash_amount": cash_amount, "point_amount": point_amount,
        }
        if note is not None:
            payload["note"] = note
        if hand_id is not None:
            payload["hand_id"] = hand_id
        if order is not None:
            payload["order"] = order
        return self._request(
            "POST", f"/api/staff/sessions/{session_id}/ledger-entries",
            json=payload, headers=self._staff_headers(),
        )

    def commit_settlement(self, session_id: str) -> list[dict]:
        return self._request(
            "POST", f"/api/staff/sessions/{session_id}/settlement/commit",
            headers=self._staff_headers(),
        )["settlements"]

    def set_payment_status(self, session_id: str, player_id: str, status: str) -> dict:
        return self._request(
            "PUT",
            f"/api/staff/sessions/{session_id}/players/{player_id}/payment-status",
            json={"status": status}, headers=self._staff_headers(),
        )

    def record_payment(
        self, session_id: str, player_id: str, paid_amount: int
    ) -> dict:
        """受領額を記録し partial/paid/unpaid を導出する（ADR-0023）。"""
        return self._request(
            "PUT",
            f"/api/staff/sessions/{session_id}/players/{player_id}/payment",
            json={"paid_amount": paid_amount}, headers=self._staff_headers(),
        )

    def confirm_order(self, request_id: str, unit_amount: int) -> dict:
        return self._request(
            "POST", f"/api/staff/order-requests/{request_id}/confirm",
            json={"unit_amount": unit_amount}, headers=self._staff_headers(),
        )

    def reject_order(self, request_id: str) -> dict:
        return self._request(
            "POST", f"/api/staff/order-requests/{request_id}/reject",
            headers=self._staff_headers(),
        )

    # ――― 会計の不足分（reversal / point grant, ADR-0036 §A）―――

    def reverse_entry(self, entry_id: str) -> dict:
        """ledger entry を reversal で取り消す（append-only）。"""
        return self._request(
            "POST", f"/api/staff/ledger-entries/{entry_id}/reverse",
            headers=self._staff_headers(),
        )

    def grant_points(
        self, player_id: str, delta_points: int, reason: str = "manual_grant",
        session_id: str | None = None, idempotency_key: str | None = None,
    ) -> dict:
        """point を付与する（manual_grant 等）。"""
        payload: dict = {"delta_points": delta_points, "reason": reason}
        if session_id is not None:
            payload["session_id"] = session_id
        if idempotency_key is not None:
            payload["idempotency_key"] = idempotency_key
        return self._request(
            "POST", f"/api/staff/players/{player_id}/point-grants",
            json=payload, headers=self._staff_headers(),
        )

    # ――― session / 座席 / player ライフサイクル（ADR-0036 §B）―――

    def list_sessions(self) -> list[dict]:
        """全 session 一覧（staff 卓選択用）。"""
        return self._request(
            "GET", "/api/staff/sessions", headers=self._staff_headers(),
        )["sessions"]

    def create_session(
        self, label: str | None = None, blinds: dict | None = None
    ) -> dict:
        payload: dict = {}
        if label is not None:
            payload["label"] = label
        if blinds is not None:
            payload["blinds"] = blinds
        return self._request(
            "POST", "/api/staff/sessions", json=payload, headers=self._staff_headers(),
        )

    def close_session(self, session_id: str) -> dict:
        return self._request(
            "POST", f"/api/staff/sessions/{session_id}/close",
            headers=self._staff_headers(),
        )

    def get_seating(self, session_id: str) -> dict:
        """現在の seating + 記録済 hand_id 一覧。"""
        return self._request(
            "GET", f"/api/staff/sessions/{session_id}/seating",
            headers=self._staff_headers(),
        )

    def assign_seats(
        self, session_id: str, hand_id: int, assignments: list[dict]
    ) -> list[dict]:
        """指定 hand に seat→player を割り当てる（assignments=[{seat_no, player_id}, ...]）。"""
        return self._request(
            "PUT", f"/api/staff/sessions/{session_id}/hands/{hand_id}/seats",
            json={"assignments": assignments}, headers=self._staff_headers(),
        )["assignments"]

    def staff_list_players(self) -> list[dict]:
        """registry の全 player（staff token）。"""
        return self._request(
            "GET", "/api/staff/players", headers=self._staff_headers(),
        )["players"]

    def create_player(self, display_name: str) -> dict:
        return self._request(
            "POST", "/api/staff/players", json={"display_name": display_name},
            headers=self._staff_headers(),
        )

    def rename_player(self, player_id: str, display_name: str) -> dict:
        return self._request(
            "PUT", f"/api/staff/players/{player_id}",
            json={"display_name": display_name}, headers=self._staff_headers(),
        )

    # ――― sync API（ADR-0022。state-based merge。staff-token gate）―――

    def pull_sync_snapshot(self) -> dict:
        """peer ノードの全レコード snapshot を取得する（GET /api/staff/sync/snapshot）。"""
        return self._request(
            "GET", "/api/staff/sync/snapshot", headers=self._staff_headers(),
        )

    def push_sync_merge(self, snapshot: dict) -> dict:
        """snapshot を peer ノードに送って merge させ、summary を受け取る（POST .../merge）。"""
        return self._request(
            "POST", "/api/staff/sync/merge", json=snapshot,
            headers=self._staff_headers(),
        )

    def sync_bidirectional(self, peer: "ViewerApiClient") -> dict:
        """self と peer を双方向に収束させる（ADR-0022）。

        peer の snapshot を self に merge し、self の snapshot を peer に merge する。merge は
        可換・冪等なので、両ノードが同じ union 状態に収束する。
        """
        into_self = self.push_sync_merge(peer.pull_sync_snapshot())
        into_peer = peer.push_sync_merge(self.pull_sync_snapshot())
        return {"into_self": into_self, "into_peer": into_peer}
