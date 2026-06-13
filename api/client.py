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
    ) -> None:
        if client is not None:
            self._client = client
            self._owns_client = False
        else:
            import httpx

            self._client = httpx.Client(base_url=base_url.rstrip("/"))
            self._owns_client = True

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "ViewerApiClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ――― 内部 ―――

    def _request(self, method: str, path: str, json: Any = None) -> Any:
        resp = self._client.request(method, path, json=json)
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
            json=payload,
        )
