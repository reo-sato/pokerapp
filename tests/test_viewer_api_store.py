"""tests/test_viewer_api_store.py

ADR-0059: 店舗でお客さんが自分のハンドを見る。

- hand logger（別プロセス）が API の起動後に書いた `players.json` / `sessions.json` を、API を
  再起動せずに読む（`/api/` の要求ごとに、変わっていれば読み直す）。
- お客さん向け画面（mobile/ の web 版）を同じポートの `/` で配信する。API は `/api/` のまま。
- `--viewer-api` の待ち受けは `--host` / `--port` で config より優先して指定できる。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

import api.server as server  # noqa: E402
from api.server import PLAYER_WEB_DIR, create_app  # noqa: E402
from core.hand_log import HandSummary  # noqa: E402
from core.ledger_repository import LedgerRepository  # noqa: E402
from core.menu import MenuMaster  # noqa: E402
from core.order_request_repository import OrderRequestRepository  # noqa: E402
from core.player_repository import PlayerRepository  # noqa: E402
from core.session_repository import SessionRepository  # noqa: E402


def _client(tmp_path: Path, web_dir: Path | None = None) -> TestClient:
    players = PlayerRepository(tmp_path / "players.json")
    sessions = SessionRepository(tmp_path / "sessions.json", player_repo=players)
    return TestClient(create_app(
        players, sessions, tmp_path / "logs",
        ledger_repo=LedgerRepository(tmp_path / "ledger.json", session_repo=sessions),
        order_repo=OrderRequestRepository(tmp_path / "order_requests.json", session_repo=sessions),
        menu=MenuMaster(tmp_path / "menu.json"),
        player_web_dir=web_dir,
    ))


def _write_hand_log(log_dir: Path, session_id: str, seat_names: dict[int, str]) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    hand = HandSummary(
        hand_id=1, session_id=session_id,
        started_at="2026-09-25T20:00:00", ended_at="2026-09-25T20:01:00",
        blinds={"sb": 5, "bb": 10}, board=["As", "Kd", "7c"], board_source="rfid",
        players=[{"seat": seat, "name": name, "hole_cards": None, "hole_cards_source": "",
                  "stack_start": 1000, "stack_end": 1000, "result": 0}
                 for seat, name in seat_names.items()],
        pot_total=15, winner_seat=1, actions=[], review_required=False,
    ).to_dict()
    (log_dir / f"{session_id}.json").write_text(
        json.dumps({"session_id": session_id, "hands": [hand]}, ensure_ascii=False),
        encoding="utf-8",
    )


class TestHandLoggerWritesAfterTheApiStarted:
    def test_customer_finds_the_session_and_hand(self, tmp_path: Path):
        client = _client(tmp_path)
        assert client.get("/api/players").json()["players"] == []

        # hand logger（別プロセス = 別の repository インスタンス）が席とハンドを書く
        players = PlayerRepository(tmp_path / "players.json")
        sessions = SessionRepository(tmp_path / "sessions.json", player_repo=players)
        session = sessions.create_session(label="金曜", blinds={"sb": 5, "bb": 10})
        taro = players.find_or_create("太郎")
        sessions.assign_seat(session.session_id, 1, 1, taro.player_id)
        _write_hand_log(tmp_path / "logs", session.session_id, {1: "太郎", 2: "Player2"})

        names = [p["display_name"] for p in client.get("/api/players").json()["players"]]
        assert names == ["太郎"]
        mine = client.get(f"/api/players/{taro.player_id}/sessions").json()["sessions"]
        assert [s["session_id"] for s in mine] == [session.session_id]
        hands = client.get(
            f"/api/players/{taro.player_id}/sessions/{session.session_id}/hands"
        ).json()["hands"]
        assert [h["hand_id"] for h in hands] == [1]

    def test_later_hands_show_up_without_a_restart(self, tmp_path: Path):
        client = _client(tmp_path)
        players = PlayerRepository(tmp_path / "players.json")
        sessions = SessionRepository(tmp_path / "sessions.json", player_repo=players)
        session = sessions.create_session(label="x")
        taro = players.find_or_create("太郎")
        sessions.assign_seat(session.session_id, 1, 1, taro.player_id)
        url = f"/api/players/{taro.player_id}/sessions"
        assert client.get(url).json()["sessions"][0]["hands_played"] == 1
        sessions.assign_seat(session.session_id, 2, 1, taro.player_id)
        assert client.get(url).json()["sessions"][0]["hands_played"] == 2


class TestPlayerScreenIsServed:
    def _web(self, tmp_path: Path) -> Path:
        web = tmp_path / "web"
        (web / "_expo" / "static" / "js" / "web").mkdir(parents=True)
        (web / "index.html").write_text('<div id="root"></div>', encoding="utf-8")
        (web / "_expo" / "static" / "js" / "web" / "index-abc.js").write_text("1", encoding="utf-8")
        return web

    def test_root_serves_the_screen_and_api_stays_json(self, tmp_path: Path):
        client = _client(tmp_path, self._web(tmp_path))
        page = client.get("/")
        assert page.status_code == 200 and 'id="root"' in page.text
        assert client.get("/_expo/static/js/web/index-abc.js").status_code == 200
        assert client.get("/api/health").json()["status"] == "ok"
        assert client.get("/api/players").json() == {"players": []}

    def test_without_a_build_the_root_is_not_served(self, tmp_path: Path):
        client = _client(tmp_path, tmp_path / "missing")
        assert client.get("/").status_code == 404
        assert client.get("/api/health").status_code == 200


class TestRunServerAddress:
    def test_host_and_port_override_the_config(self, monkeypatch: pytest.MonkeyPatch):
        import sys
        import types

        seen: dict = {}
        fake_uvicorn = types.SimpleNamespace(run=lambda app, host, port: seen.update(host=host, port=port))
        monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)   # テスト依存に uvicorn は無い
        cfg = {"viewer_api": {"bind_host": "127.0.0.1", "bind_port": 8788}}
        server.run_server(cfg, host="0.0.0.0", port=9999)
        assert seen == {"host": "0.0.0.0", "port": 9999}
        server.run_server(cfg)
        assert seen == {"host": "127.0.0.1", "port": 8788}


class TestCommittedPlayerBuild:
    """リポジトリに含めたビルド（`api/static/player/`, ADR-0059）をそのまま配信する。"""

    def test_the_server_serves_the_committed_build(self, tmp_path: Path):
        import re

        client = _client(tmp_path, PLAYER_WEB_DIR)
        page = client.get("/")
        assert page.status_code == 200 and 'id="root"' in page.text
        for src in re.findall(r'<script[^>]*\bsrc="([^"]+)"', page.text):
            assert client.get(src).status_code == 200, src

    def test_the_entry_is_revalidated_but_hashed_scripts_are_not(self, tmp_path: Path):
        # 更新で JS の名前が変わる。古い index.html が残ると消えた JS を読みに行って真っ白になる。
        client = _client(tmp_path, self._web(tmp_path))
        assert client.get("/").headers["cache-control"] == "no-cache"
        assert "cache-control" not in client.get("/_expo/static/js/web/index-abc.js").headers

    _web = TestPlayerScreenIsServed._web

    def test_unknown_api_paths_stay_json(self, tmp_path: Path):
        client = _client(tmp_path, PLAYER_WEB_DIR)
        res = client.get("/api/no-such-endpoint")
        assert res.status_code == 404
        assert res.headers["content-type"].startswith("application/json")
