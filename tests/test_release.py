"""tests/test_release.py

ADR-0066: リリース版（`stable` ブランチ）と、いま動いている版の記録。

- `tools/release.py promote` は開発版（`verify-v1`）のコミットを `stable` に**前に進めるだけ**で、日付のタグを付ける。
  開発版に入っていないコミット・巻き戻し・未コミットの変更があれば断る。
- いま動いている版（`core/version.py`）は `installer/installed.json`（インストーラが書く）か git から読み、
  `/api/health`・`main.py --version` に出す。
- インストーラは更新の前にコミットを確かめ、そのコミットの zip を取り、`installed.json` に記録する。
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import release  # noqa: E402

from core import version  # noqa: E402


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                          check=True).stdout.strip()


def _commit(work: Path, name: str) -> str:
    (work / name).write_text(name, encoding="utf-8")
    _git(work, "add", name)
    _git(work, "commit", "-q", "-m", name)
    return _git(work, "rev-parse", "HEAD")


@pytest.fixture()
def repos(tmp_path: Path):
    """origin（bare）と、開発版 verify-v1 を持つ作業用 clone。"""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True, capture_output=True)
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "t")
    _git(work, "checkout", "-q", "-b", "verify-v1")
    first = _commit(work, "a.txt")
    _git(work, "push", "-q", "origin", "verify-v1")
    return {"origin": origin, "work": work, "first": first}


class TestPromote:
    def test_the_first_release_creates_stable_and_a_dated_tag(self, repos):
        work = repos["work"]
        result = release.promote(work, today=date(2026, 10, 11))
        assert result["tag"] == "release-20261011" and result["previous"] is None
        assert _git(repos["origin"], "rev-parse", "stable") == repos["first"]
        assert _git(repos["origin"], "rev-parse", "release-20261011^{commit}") == repos["first"]

    def test_it_only_moves_forward_and_numbers_same_day_tags(self, repos):
        work = repos["work"]
        release.promote(work, today=date(2026, 10, 11))
        second = _commit(work, "b.txt")
        _git(work, "push", "-q", "origin", "verify-v1")
        result = release.promote(work, today=date(2026, 10, 11))
        assert result["tag"] == "release-20261011-2" and result["previous"] == repos["first"]
        assert len(result["commits"]) == 1
        assert _git(repos["origin"], "rev-parse", "stable") == second
        with pytest.raises(release.ReleaseError, match="既にリリース版"):
            release.promote(work, today=date(2026, 10, 11))
        with pytest.raises(release.ReleaseError, match="巻き戻し"):
            release.promote(work, ref=repos["first"], today=date(2026, 10, 12))
        assert _git(repos["origin"], "rev-parse", "stable") == second   # 巻き戻っていない

    def test_a_commit_outside_the_development_branch_is_refused(self, repos):
        work = repos["work"]
        _git(work, "checkout", "-q", "-b", "side")
        side = _commit(work, "side.txt")
        with pytest.raises(release.ReleaseError, match="入っていないコミット"):
            release.promote(work, ref=side)

    def test_dry_run_changes_nothing(self, repos):
        result = release.promote(repos["work"], dry_run=True, today=date(2026, 10, 11))
        assert result["tag"] == "release-20261011"
        out = subprocess.run(["git", "-C", str(repos["origin"]), "rev-parse", "--verify", "--quiet", "stable"],
                             capture_output=True, text=True)
        assert out.returncode != 0   # stable はまだ無い

    def test_uncommitted_changes_stop_it(self, repos):
        (repos["work"] / "a.txt").write_text("changed", encoding="utf-8")
        with pytest.raises(release.ReleaseError, match="未コミット"):
            release.promote(repos["work"])

    def test_status_lists_pending_commits(self, repos):
        work = repos["work"]
        release.promote(work, today=date(2026, 10, 11))
        _commit(work, "c.txt")
        _git(work, "push", "-q", "origin", "verify-v1")
        text = release.status(work)
        assert "release-20261011" in text and "まだリリースしていないコミット: 1" in text


class TestVersionInfo:
    def test_reads_what_the_installer_wrote(self, tmp_path: Path):
        path = tmp_path / "installed.json"
        sha = "9ffc9782dd26aa77e1ca4aecc3d948c7726e2a30"
        # PowerShell 5.1 の Set-Content -Encoding UTF8 は BOM を付ける
        path.write_bytes(b"\xef\xbb\xbf" + json.dumps(
            {"branch": "stable", "commit": sha, "installed_at": "2026-10-11T12:00:00"}).encode())
        info = version.version_info(installed_path=path, root=tmp_path)
        assert info["branch"] == "stable" and info["release"] is True and info["commit_short"] == "9ffc978"
        assert version.describe(info).startswith("リリース版 stable @ 9ffc978")

    def test_development_branch_is_not_a_release(self, tmp_path: Path):
        path = tmp_path / "installed.json"
        path.write_text(json.dumps({"branch": "verify-v1", "commit": "", "installed_at": None}), encoding="utf-8")
        info = version.version_info(installed_path=path, root=tmp_path)
        assert info["release"] is False and info["commit"] is None
        assert version.describe(info).startswith("開発版 verify-v1 @ ?")

    def test_unknown_when_nothing_is_recorded(self, tmp_path: Path):
        info = version.version_info(installed_path=tmp_path / "missing.json", root=tmp_path)
        assert info["branch"] is None and info["release"] is None
        assert "記録されていません" in version.describe(info)


def test_health_reports_the_version(tmp_path: Path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    import api.server as server
    from core.player_repository import PlayerRepository
    from core.session_repository import SessionRepository

    monkeypatch.setattr(server, "version_info", lambda: {
        "version": "1.0.0", "branch": "stable", "commit": "a" * 40, "commit_short": "aaaaaaa",
        "installed_at": None, "release": True})
    players = PlayerRepository(tmp_path / "players.json")
    client = TestClient(server.create_app(players, SessionRepository(tmp_path / "sessions.json", player_repo=players),
                                          tmp_path / "logs"))
    body = client.get("/api/health").json()
    assert body["status"] == "ok" and body["branch"] == "stable" and body["release"] is True
    assert body["commit"] == "a" * 40


def test_the_installer_pins_the_commit_and_records_it():
    text = (REPO_ROOT / "installer" / "install.ps1").read_bytes().decode("utf-8-sig")
    assert "function Resolve-Commit" in text and "api.github.com/repos/$Repo/commits/$Ref" in text
    assert 'zip/$($script:Commit)' in text                    # そのコミットの zip を取る
    assert '$pass += @("-Commit", $Commit)' in text           # 更新後のインストーラに引き継ぐ
    assert "installed.json" in text
    assert "    Write-InstalledVersion\r\n    Start-StoreServer" in text


def test_main_prints_the_version():
    out = subprocess.run([sys.executable, str(REPO_ROOT / "main.py"), "--version"],
                         capture_output=True, text=True, cwd=REPO_ROOT, check=True)
    assert "@" in out.stdout or "記録されていません" in out.stdout
