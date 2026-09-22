"""tests/test_menu_edit.py

ADR-0046 — menu master の staff 編集（価格改定・品切れ）。

- core: set_items の validation / atomic 永続化 / reload-on-read / sold_out
- API: PUT /api/staff/menu（staff write）/ GET /api/menu への反映 /
  注文 POST の item_sold_out reject

fastapi / httpx 未導入環境では API 部分のみ skip。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.menu import MenuMaster, MenuValidationError


def _write_menu(path: Path, items: list[dict]) -> None:
    path.write_text(json.dumps({"items": items}, ensure_ascii=False), encoding="utf-8")


class TestMenuMasterEdit:
    def test_set_items_persists_atomically_and_normalizes(self, tmp_path: Path):
        menu = MenuMaster(path=tmp_path / "menu.json")
        saved = menu.set_items([
            {"item_name": " ビール ", "unit_amount": 700},
            {"item_name": "コーラ", "unit_amount": 400, "sold_out": True},
            {"item_name": "水", "unit_amount": 300, "sold_out": False},
        ])
        assert [i["item_name"] for i in saved] == ["ビール", "コーラ", "水"]
        # sold_out=false は永続形に書かない（既存ファイル互換）。
        raw = json.loads((tmp_path / "menu.json").read_text(encoding="utf-8"))
        assert raw["items"][0] == {"item_name": "ビール", "unit_amount": 700}
        assert raw["items"][1]["sold_out"] is True
        assert "sold_out" not in raw["items"][2]

        assert menu.unit_amount("ビール") == 700
        assert menu.is_sold_out("コーラ") is True
        assert menu.is_sold_out("ビール") is False
        assert menu.is_sold_out("メニュー外") is False

    @pytest.mark.parametrize("bad", [
        [{"item_name": "", "unit_amount": 100}],
        [{"item_name": "x" * 101, "unit_amount": 100}],
        [{"item_name": "a", "unit_amount": -1}],
        [{"item_name": "a", "unit_amount": "100"}],
        [{"item_name": "a", "unit_amount": True}],
        [{"item_name": "a", "unit_amount": 100, "sold_out": "yes"}],
        [{"item_name": "a", "unit_amount": 100}, {"item_name": "a", "unit_amount": 200}],
        "not-a-list",
    ])
    def test_set_items_validation(self, tmp_path: Path, bad):
        menu = MenuMaster(path=tmp_path / "menu.json")
        with pytest.raises(MenuValidationError):
            menu.set_items(bad)  # type: ignore[arg-type]
        # 失敗時は永続化されない。
        assert not (tmp_path / "menu.json").exists()

    def test_reload_on_read_follows_other_process_write(self, tmp_path: Path):
        """別プロセス（別インスタンス）の write に mtime 検知で追従する。"""
        import os

        path = tmp_path / "menu.json"
        _write_menu(path, [{"item_name": "ビール", "unit_amount": 700}])
        reader = MenuMaster(path=path)
        writer = MenuMaster(path=path)
        assert reader.unit_amount("ビール") == 700

        writer.set_items([{"item_name": "ビール", "unit_amount": 800, "sold_out": True}])
        # mtime 解像度対策（同一秒内更新でも検知できるよう強制的に進める）。
        st = path.stat()
        os.utime(path, (st.st_atime, st.st_mtime + 1))
        assert reader.unit_amount("ビール") == 800
        assert reader.is_sold_out("ビール") is True

    def test_sold_out_loads_from_file(self, tmp_path: Path):
        path = tmp_path / "menu.json"
        _write_menu(path, [
            {"item_name": "ビール", "unit_amount": 700, "sold_out": True},
        ])
        menu = MenuMaster(path=path)
        assert menu.is_sold_out("ビール") is True
        assert menu.list_items()[0]["sold_out"] is True


# ――― API（fastapi/httpx が無ければ skip）―――

fastapi = pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from api.client import ViewerApiClient, ViewerApiError  # noqa: E402
from api.server import create_app  # noqa: E402
from core.ledger_repository import LedgerRepository  # noqa: E402
from core.order_request_repository import OrderRequestRepository  # noqa: E402
from core.player_repository import PlayerRepository  # noqa: E402
from core.session_repository import SessionRepository  # noqa: E402

_TOKEN = "staff-token"


def _build(tmp_path: Path) -> dict:
    players = PlayerRepository(path=tmp_path / "players.json")
    alice = players.create_player("Alice")
    sessions = SessionRepository(path=tmp_path / "sessions.json", player_repo=players)
    s = sessions.create_session(label="menu")
    ledger = LedgerRepository(path=tmp_path / "ledger.json", session_repo=sessions)
    orders = OrderRequestRepository(path=tmp_path / "order_requests.json",
                                    session_repo=sessions)
    menu_path = tmp_path / "menu.json"
    _write_menu(menu_path, [{"item_name": "ビール", "unit_amount": 700}])
    menu = MenuMaster(path=menu_path)
    app = create_app(players, sessions, tmp_path / "logs", ledger_repo=ledger,
                     order_repo=orders, menu=menu, orders_writable=True,
                     staff_token=_TOKEN)
    http = TestClient(app)
    return {
        "http": http, "alice": alice, "session": s,
        "staff": ViewerApiClient(client=http, staff_token=_TOKEN),
        "player": ViewerApiClient(client=http),
    }


def test_staff_update_menu_roundtrip(tmp_path: Path):
    env = _build(tmp_path)
    items = env["staff"].update_menu([
        {"item_name": "ビール", "unit_amount": 800, "sold_out": True},
        {"item_name": "ハイボール", "unit_amount": 600},
    ])
    assert [i["item_name"] for i in items] == ["ビール", "ハイボール"]

    # GET /api/menu（無認証 read）に即反映。
    body = env["http"].get("/api/menu").json()
    assert body["items"][0]["sold_out"] is True
    assert body["items"][1] == {"item_name": "ハイボール", "unit_amount": 600}


def test_staff_update_menu_validation_and_auth(tmp_path: Path):
    env = _build(tmp_path)
    with pytest.raises(ViewerApiError) as exc:
        env["staff"].update_menu([{"item_name": "", "unit_amount": 100}])
    assert exc.value.code == "invalid_menu"
    assert exc.value.status_code == 400

    with pytest.raises(ViewerApiError) as exc2:
        ViewerApiClient(client=env["http"], staff_token="wrong").update_menu([])
    assert exc2.value.code == "unauthorized"


def test_order_post_rejects_sold_out_item(tmp_path: Path):
    env = _build(tmp_path)
    env["staff"].update_menu([{"item_name": "ビール", "unit_amount": 700, "sold_out": True}])

    sid, pid = env["session"].session_id, env["alice"].player_id
    with pytest.raises(ViewerApiError) as exc:
        env["player"].create_order_request(pid, sid, "ビール", 1)
    assert exc.value.code == "item_sold_out"
    assert exc.value.status_code == 400

    # 品切れ解除で注文できる。
    env["staff"].update_menu([{"item_name": "ビール", "unit_amount": 700}])
    created = env["player"].create_order_request(pid, sid, "ビール", 1)
    assert created["status"] == "pending"
