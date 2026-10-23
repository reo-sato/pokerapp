"""tests/test_staff_web.py

ADR-0064: スタッフ用 iPad 画面（`staff/` の web 版）を店舗 PC の API が `/staff/` で配信する。

- ビルドはリポジトリに含める（店舗 PC に Node を入れない）。`staff/` のソースを変えてビルドし直し
  忘れると店舗の画面が古いままになるので CI で落とす（`scripts/build_staff_web.py`）。
- スタッフトークンは画面に埋め込まない（ログイン画面で入力する）。
- お客さん向け画面（`/`）と API（`/api/`）の動きは変えない。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import build_staff_web as bsw  # noqa: E402


class TestCommittedBuild:
    def test_the_committed_build_is_complete(self):
        assert bsw.check_build(bsw.OUTPUT_DIR) == []

    def test_built_for_the_staff_path_on_the_same_origin(self):
        info = json.loads((bsw.OUTPUT_DIR / bsw.BUILD_INFO_NAME).read_text(encoding="utf-8"))
        assert info["api_url"] == "/"           # 画面を配信した PC・ポートの API を読む
        assert info["base_url"] == "/staff"     # JS は /staff/_expo/... に置く
        assert info["staff_token"] is False     # トークンは埋め込まない

    def test_the_bundle_reads_the_same_origin_api_and_has_no_unexpanded_env(self):
        index = (bsw.OUTPUT_DIR / "index.html").read_text(encoding="utf-8")
        scripts = bsw.script_sources(index)
        assert scripts and all(src.startswith("/staff/_expo/") for src in scripts)
        for src in scripts:
            bundle = (bsw.OUTPUT_DIR / src[len("/staff/"):]).read_text(encoding="utf-8")
            assert 'HttpStaffRepository("/",' in bundle.replace(" ", "") \
                or 'HttpStaffRepository("/")' in bundle.replace(" ", "")
            assert "process.env.EXPO_PUBLIC" not in bundle   # 未展開の環境変数が残っていない

    def test_the_build_matches_the_staff_sources(self):
        problems = bsw.check()
        assert problems == [], (
            "スタッフ用画面のビルドが staff/ のソースと揃っていません。"
            "`python scripts/build_staff_web.py` で作り直してコミットしてください:\n"
            + "\n".join(problems)
        )


class TestBuildScript:
    def test_scripts_outside_the_staff_path_are_reported(self, tmp_path: Path):
        (tmp_path / "_expo").mkdir()
        (tmp_path / "_expo" / "a.js").write_text("x", encoding="utf-8")
        (tmp_path / "index.html").write_text('<script src="/_expo/a.js"></script>', encoding="utf-8")
        (tmp_path / bsw.BUILD_INFO_NAME).write_text(json.dumps(bsw.BUILD_SETTINGS), encoding="utf-8")
        problems = bsw.check_build(tmp_path)
        assert any("/staff/ の外" in p for p in problems)   # 置き場所なしでビルドされた

    def test_line_endings_do_not_matter(self, tmp_path: Path, monkeypatch):
        (tmp_path / "App.tsx").write_bytes(b"a\nb\n")
        monkeypatch.setattr(bsw, "STAFF_DIR", tmp_path)
        lf = bsw.source_hash()
        (tmp_path / "App.tsx").write_bytes(b"a\r\nb\r\n")
        assert bsw.source_hash() == lf   # Windows の checkout（CRLF）でも同じ

    def test_tests_are_not_inputs_but_screens_and_config_are(self, tmp_path: Path, monkeypatch):
        (tmp_path / "src").mkdir()
        (tmp_path / "App.tsx").write_text("x", encoding="utf-8")
        monkeypatch.setattr(bsw, "STAFF_DIR", tmp_path)
        base = bsw.source_hash()
        (tmp_path / "src" / "foo.test.ts").write_text("test", encoding="utf-8")
        assert bsw.source_hash() == base
        (tmp_path / "src" / "Screen.tsx").write_text("screen", encoding="utf-8")
        with_screen = bsw.source_hash()
        assert with_screen != base
        (tmp_path / "app.config.js").write_text("module.exports = c => c;", encoding="utf-8")
        assert bsw.source_hash() != with_screen

    def test_build_env_drops_developer_values_and_sets_the_path(self, monkeypatch):
        monkeypatch.setenv("EXPO_PUBLIC_STAFF_TOKEN", "secret")
        monkeypatch.setenv("EXPO_PUBLIC_API_URL", "http://192.168.1.10:8788")
        env = bsw._build_env()
        assert "EXPO_PUBLIC_STAFF_TOKEN" not in env
        assert env["EXPO_PUBLIC_API_URL"] == "/"
        assert env["STAFF_WEB_BASE_URL"] == "/staff"
        assert env["EXPO_NO_DOTENV"] == "1"


pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from api.server import PLAYER_WEB_DIR, STAFF_WEB_DIR, create_app  # noqa: E402
from core.ledger_repository import LedgerRepository  # noqa: E402
from core.menu import MenuMaster  # noqa: E402
from core.order_request_repository import OrderRequestRepository  # noqa: E402
from core.player_repository import PlayerRepository  # noqa: E402
from core.session_repository import SessionRepository  # noqa: E402


def _client(tmp_path: Path, *, staff_web_dir: Path | None = STAFF_WEB_DIR,
            player_web_dir: Path | None = PLAYER_WEB_DIR) -> TestClient:
    players = PlayerRepository(tmp_path / "players.json")
    sessions = SessionRepository(tmp_path / "sessions.json", player_repo=players)
    return TestClient(create_app(
        players, sessions, tmp_path / "logs",
        ledger_repo=LedgerRepository(tmp_path / "ledger.json", session_repo=sessions),
        order_repo=OrderRequestRepository(tmp_path / "order_requests.json", session_repo=sessions),
        menu=MenuMaster(tmp_path / "menu.json"),
        orders_writable=True, staff_token="tok",
        player_web_dir=player_web_dir, staff_web_dir=staff_web_dir,
    ))


def _fake_build(root: Path, marker: str) -> Path:
    (root / "_expo" / "static" / "js" / "web").mkdir(parents=True)
    (root / "_expo" / "static" / "js" / "web" / "index-abc.js").write_text("x", encoding="utf-8")
    (root / "index.html").write_text(f'<div id="root">{marker}</div>', encoding="utf-8")
    return root


class TestStaffScreenIsServed:
    def test_the_staff_screen_and_its_scripts_load(self, tmp_path: Path):
        client = _client(tmp_path)
        page = client.get("/staff/")
        assert page.status_code == 200 and 'id="root"' in page.text
        assert "<title>店舗スタッフ</title>" in page.text
        for src in re.findall(r'<script[^>]*\bsrc="([^"]+)"', page.text):
            res = client.get(src)
            assert res.status_code == 200, src
            assert "javascript" in res.headers["content-type"]

    def test_without_the_trailing_slash_it_redirects(self, tmp_path: Path):
        res = _client(tmp_path).get("/staff", follow_redirects=False)
        assert res.status_code in (307, 308)
        assert res.headers["location"].endswith("/staff/")

    def test_the_entry_is_revalidated_but_hashed_scripts_are_not(self, tmp_path: Path):
        client = _client(tmp_path, staff_web_dir=_fake_build(tmp_path / "staff", "staff"))
        assert client.get("/staff/").headers["cache-control"] == "no-cache"
        assert "cache-control" not in client.get(
            "/staff/_expo/static/js/web/index-abc.js").headers

    def test_customers_still_get_their_screen_at_the_root(self, tmp_path: Path):
        client = _client(tmp_path,
                         staff_web_dir=_fake_build(tmp_path / "staff", "STAFF"),
                         player_web_dir=_fake_build(tmp_path / "player", "PLAYER"))
        assert "PLAYER" in client.get("/").text
        assert "STAFF" in client.get("/staff/").text

    def test_api_paths_are_unchanged(self, tmp_path: Path):
        client = _client(tmp_path)
        assert client.get("/api/health").status_code == 200
        missing = client.get("/api/no-such-endpoint")
        assert missing.status_code == 404
        assert missing.headers["content-type"].startswith("application/json")
        # 画面を配信しても staff API の認可は変わらない（トークンはログイン画面で入力する）
        assert client.get("/api/staff/sessions").status_code == 401
        ok = client.get("/api/staff/sessions", headers={"Authorization": "Bearer tok"})
        assert ok.status_code == 200

    def test_nothing_is_served_at_staff_without_a_build(self, tmp_path: Path):
        client = _client(tmp_path, staff_web_dir=tmp_path / "missing", player_web_dir=None)
        assert client.get("/staff/").status_code == 404
