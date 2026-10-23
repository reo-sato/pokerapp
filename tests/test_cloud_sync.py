"""tests/test_cloud_sync.py

ADR-0067 2b: 店舗 PC → クラウドの送り手（`core/cloud_sync.py`）と、つなぐ道具（`tools/cloud_link.py`）。

本物の店舗 PC の保存（repository で作ったファイル）→ 送り手 → 本物のクラウドのアプリ（メモリの保存）で確かめる。
- 1 回目はすべて、2 回目は何も送らない。変わったものだけ送る。
- sessions.json に載っていないセッションのハンドは送らない。聞き取った文は送らない。
- 塊に分けて送る。大きすぎると半分に分ける。1 件で大きすぎるものは送らない。
- 失敗したら間を空ける（60 → 120 → … → 900 秒）。成功で戻る。
- 店舗サーバーの中で動き、`/api/health` の `cloud` に状態が出る。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
pytest.importorskip("jsonschema")

from fastapi.testclient import TestClient  # noqa: E402

import core.cloud_sync as cloud_sync  # noqa: E402
from cloud.app import CloudSettings, create_cloud_app  # noqa: E402
from cloud.storage import MemoryStorage  # noqa: E402
from cloud.tokens import hash_token, new_device_token  # noqa: E402
from core.cloud_sync import (  # noqa: E402
    CloudSyncer,
    SourcePaths,
    SyncState,
    TransportError,
    load_credentials,
    syncer_from_config,
)
from core.ledger_repository import LedgerRepository  # noqa: E402
from core.player_repository import PlayerRepository  # noqa: E402
from core.session_repository import SessionRepository  # noqa: E402

from tests.cloud_helpers import store_snapshot  # noqa: E402

ADMIN = "a" * 40


class Bridge:
    """送り手の transport を、クラウドのアプリ（TestClient）につなぐ。"""

    def __init__(self, client: TestClient) -> None:
        self.client = client
        self.calls: list[tuple[str, str, int]] = []
        self.fail_with: Exception | None = None

    def request(self, method, url, token, body=None):
        if self.fail_with:
            raise self.fail_with
        path = "/" + url.split("/", 3)[3]
        res = self.client.request(method, path, headers={"Authorization": f"Bearer {token}"}, json=body)
        self.calls.append((method, path, res.status_code))
        try:
            return res.status_code, res.json()
        except ValueError:
            return res.status_code, None


@pytest.fixture()
def cloud():
    storage = MemoryStorage()
    client = TestClient(create_cloud_app(storage, CloudSettings(admin_token=ADMIN)))
    store = storage.create_store("テスト店")
    token = new_device_token()
    storage.add_device(store["store_id"], hash_token(token))
    return {"storage": storage, "client": client, "store_id": store["store_id"], "token": token,
            "bridge": Bridge(client)}


@pytest.fixture()
def shop(tmp_path):
    snap = store_snapshot(tmp_path)
    return {"root": tmp_path, "paths": SourcePaths.under(tmp_path), "ids": snap["ids"]}


def _syncer(cloud, shop, **kw):
    kw.setdefault("now_us", lambda: 1000)
    return CloudSyncer("https://cloud.example", cloud["token"], paths=shop["paths"],
                       state_path=shop["root"] / "cloud_sync_state.json", transport=cloud["bridge"], **kw)


def test_first_run_sends_everything_then_nothing(cloud, shop):
    syncer = _syncer(cloud, shop)
    first = syncer.run_once()
    assert first["ok"] and first["sent"] == 2 + 1 + 3 + 1 + 2 + 2 + 2 + 1   # 最後の 1 = 店舗の設定
    counts = cloud["storage"].counts(cloud["store_id"])
    assert counts == {"players": 2, "sessions": 1, "ledger_entries": 3, "point_entries": 1, "settlements": 2,
                      "order_requests": 2, "hands": 2, "store_settings": 1}
    second = syncer.run_once()
    assert second == {"ok": True, "sent": 0, "batches": 0, "error": None}
    assert syncer.status()["last_ok_at"] and syncer.status()["pending"] == 0


def test_only_changes_are_sent(cloud, shop):
    syncer = _syncer(cloud, shop)
    syncer.run_once()
    players = PlayerRepository(shop["root"] / "players.json")
    sessions = SessionRepository(shop["root"] / "sessions.json", player_repo=players)
    ledger = LedgerRepository(shop["root"] / "ledger.json", session_repo=sessions, player_repo=players)
    ledger.grant_points(shop["ids"]["bob"], 50)
    players.rename_player(shop["ids"]["alice"], "アリス2")
    result = syncer.run_once()
    assert result["sent"] == 2
    got = cloud["storage"].get_record(cloud["store_id"], "players", shop["ids"]["alice"])
    assert got["display_name"] == "アリス2"


def test_hands_only_for_known_sessions_and_without_heard_text(cloud, shop):
    log_dir = shop["root"] / "logs"
    (log_dir / "other.json").write_text(json.dumps({"session_id": "other", "hands": [
        {"hand_id": 1, "session_id": "other", "started_at": "a", "ended_at": "b", "players": [], "actions": []}]}),
        encoding="utf-8")
    _syncer(cloud, shop).run_once()
    hands = cloud["storage"].list_records(cloud["store_id"], "hands")
    assert {h["session_id"] for h in hands} == {shop["ids"]["session_id"]}
    for hand in hands:
        assert all("raw_text" not in a and "asr_confidence" not in a for a in hand["actions"])


def test_batches_and_sequence_numbers(cloud, shop, monkeypatch):
    monkeypatch.setattr(cloud_sync, "BATCH_MAX_RECORDS", 3)
    syncer = _syncer(cloud, shop)   # 時計が止まっていても seq は増え続ける
    result = syncer.run_once()
    pushes = [c for c in cloud["bridge"].calls if c[1].endswith("/push")]
    assert result["ok"] and len(pushes) == result["batches"] == 5   # 14 件 ÷ 3
    assert syncer.state.last_seq == 1004
    assert pushes and cloud["bridge"].calls[0][1].endswith("/status")   # 最初にクラウドの番号を確かめる
    assert cloud["storage"].max_seq(cloud["store_id"]) == 1004


def test_too_large_batches_are_split_and_huge_records_skipped(shop):
    storage = MemoryStorage()
    client = TestClient(create_cloud_app(storage, CloudSettings(max_body_bytes=6000)))
    store = storage.create_store("店")
    token = new_device_token()
    storage.add_device(store["store_id"], hash_token(token))
    bridge = Bridge(client)
    players = PlayerRepository(shop["root"] / "players.json")
    players.create_player("x" * 8000)       # 1 件で上限を超える
    syncer = CloudSyncer("https://c", token, paths=shop["paths"], state_path=shop["root"] / "s.json",
                         transport=bridge, now_us=lambda: 1)
    result = syncer.run_once()
    assert result["ok"]
    assert any(code == 413 for _, _, code in bridge.calls)
    assert storage.counts(store["store_id"])["players"] == 2          # 大きすぎる 1 件以外は届いた
    assert any("大きすぎ" in r for r in syncer.state.rejected.values())
    assert syncer.run_once()["sent"] == 0                              # 変わるまで送り直さない


def test_rejected_records_wait_until_they_change(cloud, shop):
    path = shop["root"] / "players.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["players"].append({"player_id": "f" * 32, "display_name": "壊れた"})   # created_at が無い
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    syncer = _syncer(cloud, shop)
    assert syncer.run_once()["ok"]
    assert f"players:{'f' * 32}" in syncer.state.rejected
    assert syncer.status()["rejected"] == 1
    assert syncer.run_once()["sent"] == 0
    doc["players"][-1]["created_at"] = "2026-10-11T10:00:00"
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    assert syncer.run_once()["sent"] == 1
    assert syncer.state.rejected == {}


def test_failures_back_off_and_recover(cloud, shop):
    syncer = _syncer(cloud, shop)
    cloud["bridge"].fail_with = TransportError("ConnectError")
    delays = []
    for _ in range(6):
        result = syncer.run_once()
        assert not result["ok"] and "つながりません" in result["error"]
        delays.append(syncer.next_delay())
    assert delays == [120, 240, 480, 900, 900, 900]
    assert syncer.status()["pending"] > 0
    cloud["bridge"].fail_with = None
    assert syncer.run_once()["ok"] and syncer.next_delay() == 60
    assert syncer.status()["last_error"] is None


def test_a_revoked_token_says_how_to_fix_it(cloud, shop):
    cloud["storage"].revoke_devices(cloud["store_id"])
    result = _syncer(cloud, shop).run_once()
    assert not result["ok"] and "cloud_link" in result["error"]


def test_state_survives_restart_and_a_lost_state_just_resends(cloud, shop):
    _syncer(cloud, shop).run_once()
    assert _syncer(cloud, shop).run_once()["sent"] == 0              # 覚えている
    (shop["root"] / "cloud_sync_state.json").unlink()
    again = _syncer(cloud, shop, now_us=lambda: 5)                    # 時計が戻っても
    result = again.run_once()
    assert result["ok"] and result["sent"] > 0
    assert again.state.last_seq > 1000                                # クラウドの番号に合わせて進む
    assert cloud["storage"].counts(cloud["store_id"])["hands"] == 2


def test_a_hand_changed_while_the_state_was_lost_still_arrives(cloud, shop):
    _syncer(cloud, shop).run_once()                                   # 番号 1000 台で送った
    (shop["root"] / "cloud_sync_state.json").unlink()
    log = shop["root"] / "logs" / f"{shop['ids']['session_id']}.json"
    doc = json.loads(log.read_text(encoding="utf-8"))
    doc["hands"][0]["winner_seat"] = 3
    log.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    _syncer(cloud, shop, now_us=lambda: 5).run_once()                 # 時計は戻っている
    key = f"{shop['ids']['session_id']}:{doc['hands'][0]['hand_id']}"
    assert cloud["storage"].get_record(cloud["store_id"], "hands", key)["winner_seat"] == 3


def test_settings_changes_are_sent(cloud, shop):
    settings = {"hole_cards": "all"}
    syncer = _syncer(cloud, shop, settings_fn=lambda: dict(settings))
    syncer.run_once()
    settings["hole_cards"] = "own_and_shown"
    assert syncer.run_once()["sent"] == 1
    assert cloud["storage"].get_record(cloud["store_id"], "store_settings", "settings") == settings
    assert cloud_sync.store_settings({"viewer": {"hole_cards": "nonsense"}}) == {"hole_cards": "all"}


class TestFromConfig:
    def test_needs_both_the_flag_and_the_credentials(self, tmp_path):
        creds = tmp_path / "cloud_credentials.json"
        assert syncer_from_config({}, credentials_path=creds) is None
        assert syncer_from_config({"cloud": {"enabled": True}}, credentials_path=creds) is None
        creds.write_text(json.dumps({"url": "https://c.example/", "device_token": "pkd_x"}), encoding="utf-8")
        syncer = syncer_from_config({"cloud": {"enabled": True, "interval_sec": 30}}, credentials_path=creds,
                                    state_path=tmp_path / "s.json")
        assert syncer.url == "https://c.example" and syncer.interval_sec == 30
        assert syncer.status()["enabled"] is True


def test_the_store_server_runs_it_and_reports_it(cloud, shop, monkeypatch):
    from api import store_server

    creds = shop["root"] / "cloud_credentials.json"
    creds.write_text(json.dumps({"url": "https://c.example", "device_token": cloud["token"]}), encoding="utf-8")
    stop = threading.Event()
    syncer = store_server.start_cloud_sync(
        {"cloud": {"enabled": True}}, stop, credentials_path=creds, state_path=shop["root"] / "s.json",
        transport=cloud["bridge"], paths=shop["paths"])
    try:
        for _ in range(100):
            if syncer.status()["last_ok_at"]:
                break
            stop.wait(0.05)
        assert syncer.status()["last_ok_at"]
        assert cloud["storage"].counts(cloud["store_id"])["hands"] == 2
    finally:
        stop.set()
    assert store_server.start_cloud_sync({}, threading.Event()) is None

    players = PlayerRepository(shop["root"] / "players.json")
    sessions = SessionRepository(shop["root"] / "sessions.json", player_repo=players)
    from core.menu import MenuMaster
    from core.order_request_repository import OrderRequestRepository

    app = store_server.create_store_app(
        {"session": {"log_dir": str(shop["root"] / "logs")}}, player_repo=players, session_repo=sessions,
        ledger_repo=LedgerRepository(shop["root"] / "ledger.json", session_repo=sessions),
        order_repo=OrderRequestRepository(shop["root"] / "order_requests.json", session_repo=sessions),
        menu=MenuMaster(shop["root"] / "menu.json"), cloud_status=syncer.status)
    health = TestClient(app).get("/api/health").json()
    assert health["cloud"]["enabled"] is True and health["cloud"]["last_ok_at"]
    assert cloud["token"] not in json.dumps(health)


class TestCloudLink:
    @pytest.fixture()
    def link(self, tmp_path):
        import tools.cloud_link as cloud_link

        storage = MemoryStorage()
        bridge = Bridge(TestClient(create_cloud_app(storage, CloudSettings(admin_token=ADMIN))))
        paths = {"credentials_path": tmp_path / "cloud_credentials.json", "state_path": tmp_path / "s.json",
                 "config_path": tmp_path / "config.json"}
        (tmp_path / "config.json").write_text(json.dumps({"session": {"log_dir": str(tmp_path / "logs")}}),
                                               encoding="utf-8")

        def run(*argv, secret=ADMIN):
            return cloud_link.main(list(argv), transport=bridge, ask_secret=lambda _: secret, **paths)

        return {"run": run, "storage": storage, "paths": paths, "module": cloud_link}

    def test_link_a_new_store(self, link, capsys):
        assert link["run"]("link", "--url", "https://cloud.example/x", "--name", "ポーカー店") == 0
        creds = load_credentials(link["paths"]["credentials_path"])
        assert creds["url"] == "https://cloud.example" and creds["store_name"] == "ポーカー店"
        assert ADMIN not in link["paths"]["credentials_path"].read_text(encoding="utf-8")   # 管理トークンは保存しない
        cfg = json.loads(link["paths"]["config_path"].read_text(encoding="utf-8"))
        assert cfg["cloud"]["enabled"] is True and cfg["session"]  # ほかの設定はそのまま
        out = capsys.readouterr().out
        assert creds["device_token"] not in out                    # 画面にも全部は出さない
        assert link["run"]("link", "--url", "https://cloud.example", "--name", "二重") == 1   # 2 つ目は作らない

    def test_wrong_admin_token_and_bad_url(self, link, capsys):
        assert link["run"]("link", "--url", "https://cloud.example", "--name", "店", secret="b" * 40) == 1
        assert "401" in capsys.readouterr().err
        assert link["run"]("link", "--url", "http://cloud.example", "--name", "店") == 1
        assert not link["paths"]["credentials_path"].exists()

    def test_reissue_status_push_unlink(self, link, capsys):
        link["run"]("link", "--url", "https://cloud.example", "--name", "店")
        old = load_credentials(link["paths"]["credentials_path"])["device_token"]
        assert link["run"]("reissue") == 0
        new = load_credentials(link["paths"]["credentials_path"])["device_token"]
        assert new != old
        assert link["storage"].find_device(hash_token(old))["revoked_at"] is not None
        assert link["run"]("push") == 0
        capsys.readouterr()
        assert link["run"]("status") == 0
        out = capsys.readouterr().out
        assert "店舗: 店" in out and "store_settings 1" in out and new not in out
        assert link["run"]("unlink") == 0
        assert not link["paths"]["credentials_path"].exists()
        assert json.loads(link["paths"]["config_path"].read_text(encoding="utf-8"))["cloud"]["enabled"] is False

    def test_link_with_an_existing_device_token(self, link):
        created = link["run"]("link", "--url", "https://cloud.example", "--name", "店")
        assert created == 0
        token = load_credentials(link["paths"]["credentials_path"])["device_token"]
        link["paths"]["credentials_path"].unlink()
        assert link["run"]("link", "--url", "https://cloud.example", "--token", secret=token) == 0
        assert load_credentials(link["paths"]["credentials_path"])["store_name"] == "店"
        assert link["run"]("link", "--url", "https://cloud.example", "--token", secret="pkd_wrong") == 1
        assert load_credentials(link["paths"]["credentials_path"])["device_token"] == token   # 間違いでは上書きしない


def test_sync_state_tolerates_garbage(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("{not json", encoding="utf-8")
    assert SyncState.load(path).acked == {}
    path.write_text(json.dumps({"acked": [], "last_seq": "x"}), encoding="utf-8")
    state = SyncState.load(path)
    assert state.acked == {} and state.last_seq == 0
    Path(path).unlink()


class TestViewerLinkFromTheStore:
    """スタッフが iPad（店舗サーバーのスタッフ API）から閲覧リンク（QR）を出す（ADR-0067 2c）。"""

    def _store_app(self, shop, links):
        from api import store_server
        from core.menu import MenuMaster
        from core.order_request_repository import OrderRequestRepository

        players = PlayerRepository(shop["root"] / "players.json")
        sessions = SessionRepository(shop["root"] / "sessions.json", player_repo=players)
        app = store_server.create_store_app(
            {"session": {"log_dir": str(shop["root"] / "logs")}, "viewer_api": {"staff_token": "staff"}},
            player_repo=players, session_repo=sessions,
            ledger_repo=LedgerRepository(shop["root"] / "ledger.json", session_repo=sessions),
            order_repo=OrderRequestRepository(shop["root"] / "order_requests.json", session_repo=sessions),
            menu=MenuMaster(shop["root"] / "menu.json"), cloud_links=links)
        return TestClient(app), players

    def test_issue_login_and_revoke(self, cloud, shop):
        syncer = _syncer(cloud, shop)                       # まだ何も送っていない → 発行の前に送る
        store, _ = self._store_app(shop, syncer)
        staff = {"Authorization": "Bearer staff"}
        alice = shop["ids"]["alice"]
        res = store.post(f"/api/staff/players/{alice}/viewer-link", headers=staff)
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["url"].startswith("http") and "#link=pkl_" in body["url"]
        assert body["qr_image"].startswith("data:image/svg+xml")
        login = cloud["client"].post("/api/cloud/v1/auth/link", json={"link": body["url"].split("#link=")[1]})
        assert login.json()["player_id"] == alice
        res = store.post(f"/api/staff/players/{alice}/viewer-link/revoke", headers=staff)
        assert res.json() == {"player_id": alice, "revoked": 1}
        assert store.post(f"/api/staff/players/{alice}/viewer-link").status_code == 401   # スタッフだけ

    def test_merged_players_get_the_survivors_link(self, cloud, shop):
        store, players = self._store_app(shop, _syncer(cloud, shop))
        players.merge_players(shop["ids"]["bob"], shop["ids"]["alice"])
        res = store.post(f"/api/staff/players/{shop['ids']['alice']}/viewer-link",
                         headers={"Authorization": "Bearer staff"})
        assert res.json()["player_id"] == shop["ids"]["bob"]

    def test_errors(self, cloud, shop):
        staff = {"Authorization": "Bearer staff"}
        store, _ = self._store_app(shop, None)
        res = store.post(f"/api/staff/players/{shop['ids']['alice']}/viewer-link", headers=staff)
        assert res.status_code == 409 and res.json()["code"] == "cloud_not_linked"
        store, _ = self._store_app(shop, _syncer(cloud, shop))
        assert store.post(f"/api/staff/players/{'f' * 32}/viewer-link", headers=staff).status_code == 404
        cloud["bridge"].fail_with = TransportError("ConnectError")
        res = store.post(f"/api/staff/players/{shop['ids']['alice']}/viewer-link", headers=staff)
        assert res.status_code == 502 and res.json()["code"] == "cloud_unavailable"
