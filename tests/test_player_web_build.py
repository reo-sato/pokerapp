"""tests/test_player_web_build.py

ADR-0059: お客さん向け画面（`mobile/` の web 版）のビルドはリポジトリに含める（店舗 PC に Node を
入れない）。`mobile/` のソースを変えてビルドし直し忘れると、店舗の画面が古いままになるので CI で落とす。

正規手順: `mobile/` を編集 → `python scripts/build_player_web.py`（要 Node + mobile/node_modules）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import build_player_web as bpw  # noqa: E402


def test_the_committed_build_is_complete():
    assert bpw.check_build(bpw.OUTPUT_DIR) == []


def test_built_for_customers_on_the_same_origin():
    info = json.loads((bpw.OUTPUT_DIR / bpw.BUILD_INFO_NAME).read_text(encoding="utf-8"))
    assert info["api_url"] == "/"            # 画面を配信した PC・ポートの API を読む
    assert info["player_auth"] == "off"      # PIN / LINE・Google の導線を出さない
    assert info["staff_token"] is False      # ハンド訂正（staff write）の導線を出さない


def test_the_bundle_carries_no_staff_token():
    index = (bpw.OUTPUT_DIR / "index.html").read_text(encoding="utf-8")
    for src in bpw.script_sources(index):
        bundle = (bpw.OUTPUT_DIR / src.lstrip("/")).read_text(encoding="utf-8")
        assert 'HttpRepository("/",' in bundle.replace(" ", "")
        assert "EXPO_PUBLIC_" not in bundle   # 未展開の環境変数が残っていない


def test_the_build_matches_the_mobile_sources():
    problems = bpw.check()
    assert problems == [], (
        "お客さん向け画面のビルドが mobile/ のソースと揃っていません。"
        "`python scripts/build_player_web.py` で作り直してコミットしてください:\n"
        + "\n".join(problems)
    )


class TestSourceHash:
    def test_line_endings_do_not_matter(self, tmp_path: Path, monkeypatch):
        (tmp_path / "src").mkdir()
        (tmp_path / "App.tsx").write_bytes(b"a\nb\n")
        monkeypatch.setattr(bpw, "MOBILE_DIR", tmp_path)
        lf = bpw.source_hash()
        (tmp_path / "App.tsx").write_bytes(b"a\r\nb\r\n")
        assert bpw.source_hash() == lf   # Windows の checkout（CRLF）でも同じ

    def test_tests_are_not_inputs_but_screens_are(self, tmp_path: Path, monkeypatch):
        (tmp_path / "src").mkdir()
        (tmp_path / "App.tsx").write_text("x", encoding="utf-8")
        monkeypatch.setattr(bpw, "MOBILE_DIR", tmp_path)
        base = bpw.source_hash()
        (tmp_path / "src" / "foo.test.ts").write_text("test", encoding="utf-8")
        assert bpw.source_hash() == base
        (tmp_path / "src" / "Screen.tsx").write_text("screen", encoding="utf-8")
        assert bpw.source_hash() != base

    def test_build_settings_are_part_of_the_hash(self, tmp_path: Path, monkeypatch):
        (tmp_path / "App.tsx").write_text("x", encoding="utf-8")
        monkeypatch.setattr(bpw, "MOBILE_DIR", tmp_path)
        base = bpw.source_hash()
        monkeypatch.setattr(bpw, "BUILD_SETTINGS", {**bpw.BUILD_SETTINGS, "player_auth": "on"})
        assert bpw.source_hash() != base


def test_build_env_drops_developer_expo_public_values(monkeypatch):
    monkeypatch.setenv("EXPO_PUBLIC_STAFF_TOKEN", "secret")
    monkeypatch.setenv("EXPO_PUBLIC_API_URL", "http://192.168.1.10:8788")
    env = bpw._build_env()
    assert "EXPO_PUBLIC_STAFF_TOKEN" not in env
    assert env["EXPO_PUBLIC_API_URL"] == "/"
    assert env["EXPO_PUBLIC_PLAYER_AUTH"] == "off"
    assert env["EXPO_NO_DOTENV"] == "1"      # mobile/.env* の staff token も読ませない
