"""tools/session_setup_ui.py: 起動時の卓の設定画面（ADR-0071）。"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from core.blind_structure import BlindStructureStore, TournamentClock, parse_structure
from tools import session_setup_ui as ui

STRUCTURE = {
    "name": "標準", "starting_stack": 30000,
    "levels": [
        {"sb": 100, "bb": 200, "ante": 0, "minutes": 20},
        {"sb": 200, "bb": 400, "ante": 400, "minutes": 20},
        {"break": True, "minutes": 10},
        {"sb": 300, "bb": 600, "ante": 600, "minutes": 20},
    ],
}


@pytest.fixture
def store(tmp_path):
    s = BlindStructureStore(tmp_path / "structures.json")
    s.save(STRUCTURE)
    return s


def _raw(**over):
    return {**ui.DEFAULT_SETUP, "seats": [2, 4, 5], "names": {"2": "あ", "5": "  "}, "stacks": {"4": "15000"},
            "default_stack": 20000, "button": 4, **over}


class TestBuildConfig:
    def test_ring(self, store):
        cfg = ui.build_session_config(_raw(), store)
        assert [(p["seat"], p["name"], p["stack"], p["named"]) for p in cfg["players"]] == [
            (2, "あ", 20000, True), (4, "Player4", 15000, False), (5, "Player5", 20000, False)]
        assert (cfg["sb"], cfg["bb"]) == (100, 200)
        assert cfg["first_button"] == 4 and cfg["button_seat"] == 2      # 1 つ手前（端末の入力と同じ）
        assert cfg["table"] == {"mode": "ring", "sb": 100, "bb": 200, "ante": 0}

    def test_tournament_mid_level(self, store):
        cfg = ui.build_session_config(_raw(mode="tournament", structure="標準", level=2, elapsed_min=5), store)
        assert (cfg["sb"], cfg["bb"]) == (200, 400)
        t = cfg["table"]
        assert (t["mode"], t["ante"], t["level"], t["elapsed_min"]) == ("tournament", 400, 2, 5.0)
        assert parse_structure(t["structure"]).name == "標準"

    @pytest.mark.parametrize("over, field", [
        ({"seats": [3]}, "seats"),
        ({"seats": [3, 12]}, "seats"),
        ({"sb": 300, "bb": 200}, "sb"),
        ({"bb": 0}, "bb"),
        ({"button": 7}, "button"),
        ({"stacks": {"4": "0"}}, "stack_4"),
        ({"mode": "tournament", "structure": "無い"}, "structure"),
        ({"mode": "tournament", "structure": "標準", "level": 9}, "level"),
        ({"mode": "x"}, "mode"),
    ])
    def test_errors_name_the_field(self, store, over, field):
        with pytest.raises(ui.SetupError) as e:
            ui.build_session_config(_raw(**over), store)
        assert field in {x["field"] for x in e.value.errors}


class TestClockFiles:
    def test_resume_from_saved_clock(self, tmp_path):
        clock = TournamentClock.start(parse_structure(STRUCTURE), 1000.0, level=2, elapsed_sec=600)
        ui.save_clock(tmp_path, "sid", clock, now=1000.0)
        # 30 分後 = レベル 2 の残り 10 分 + ブレイク 10 分 + レベル 3 の 10 分
        r = ui.resumable_clock(tmp_path, now=1000.0 + 1800)
        assert (r["structure"], r["level"], r["elapsed_min"]) == ("標準", 3, 10.0)
        assert ui.resumable_clock(tmp_path, now=1000.0 + ui.CLOCK_RESUME_MAX_SEC + 1) is None

    def test_resume_in_break_counts_from_the_level_before(self, tmp_path):
        clock = TournamentClock.start(parse_structure(STRUCTURE), 0.0, level=2, elapsed_sec=1200 + 180)
        ui.save_clock(tmp_path, "sid", clock, now=0.0)
        r = ui.resumable_clock(tmp_path, now=0.0)
        assert (r["level"], r["elapsed_min"]) == (2, 23.0)
        again = TournamentClock.start(parse_structure(STRUCTURE), 0.0, level=r["level"], elapsed_sec=r["elapsed_min"] * 60)
        assert again.current.is_break and again.remaining(0.0) == 420

    def test_status(self, tmp_path):
        clock = TournamentClock.start(parse_structure(STRUCTURE), 0.0)
        ui.save_clock(tmp_path, "sid", clock, now=0.0)
        st = ui.clock_status(tmp_path, "sid", now=60.0)
        assert st["remaining_sec"] == 1140 and st["next"]["bb"] == 400
        assert ui.clock_status(tmp_path, "other") is None


def _call(base: str, path: str, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as res:
            return res.status, json.loads(res.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


class TestServer:
    @pytest.fixture
    def server(self, store, tmp_path):
        app = ui.SetupApp(store, tmp_path)
        srv = ui.start_server(app, port=0)
        yield app, f"http://127.0.0.1:{srv.server_address[1]}"
        srv.shutdown()
        srv.server_close()

    def test_start_remembers_and_waits(self, server, tmp_path):
        app, base = server
        status, data = _call(base, "/api/setup")
        assert status == 200 and data["last"]["mode"] == "ring" and data["structures"][0]["name"] == "標準"
        status, data = _call(base, "/api/start", _raw(sb=0, log_dir=str(tmp_path)))
        assert status == 400 and data["errors"][0]["field"] == "sb" and not app.done.is_set()
        got: list = []
        waiter = threading.Thread(target=lambda: got.append(ui.wait_for_setup(app, base, open_browser=False)))
        waiter.start()
        status, data = _call(base, "/api/start", _raw(log_dir=str(tmp_path)))
        waiter.join(timeout=5)
        assert status == 200 and got and got[0]["table"]["mode"] == "ring"
        # 次はその値で開く
        assert ui.load_last_setup(tmp_path)["seats"] == [2, 4, 5]
        assert _call(base, "/api/setup")[1]["started"] is True
        assert _call(base, "/api/start", _raw(log_dir=str(tmp_path)))[0] == 400     # 2 度は始めない

    def test_structure_edit(self, server):
        app, base = server
        status, data = _call(base, "/api/structures", {"structure": {**STRUCTURE, "name": "ターボ"}})
        assert status == 200 and [s["name"] for s in data["structures"]] == ["標準", "ターボ"]
        status, data = _call(base, "/api/structures", {"structure": {**STRUCTURE, "name": "x", "levels": []}})
        assert status == 400 and "1 行以上" in data["errors"][0]["message"]
        status, data = _call(base, "/api/structures/delete", {"name": "ターボ"})
        assert [s["name"] for s in data["structures"]] == ["標準"]

    def test_clock_goes_to_the_logger_queue(self, server, tmp_path):
        app, base = server
        assert _call(base, "/api/clock", {"cmd": "next"})[0] == 400        # 始める前
        sent: list[str] = []
        app.attach_session("sid", sent.append)
        assert _call(base, "/api/clock", {"cmd": "pause"})[0] == 200 and sent == ["pause"]
        ui.save_clock(tmp_path, "sid", TournamentClock.start(parse_structure(STRUCTURE), 0.0))
        assert _call(base, "/api/clock")[1]["clock"]["structure"] == "標準"

    def test_page(self, server):
        _, base = server
        with urllib.request.urlopen(base + "/", timeout=5) as res:
            assert "卓の設定" in res.read().decode()
