"""tests/test_installer.py

Windows ワンステップインストーラ（`install.cmd` → `installer/install.ps1`）の整合検査（ADR-0057）。

ここでは Windows を持たない CI でも検査できることだけを固定する:

- 起動用 .cmd と .ps1 が揃っていて、.cmd が参照するファイルが存在する。
- .cmd は **ASCII のみ + CRLF**（日本語 Windows の cmd.exe は CP932 で読むので日本語を書くと化ける。
  ラベル / goto は LF だけだと誤動作することがある）。
- `-File` で実行する install.ps1 は **UTF-8 BOM 付き**（Windows PowerShell 5.1 は BOM が無いと ANSI として
  読み、日本語が化ける）。`irm … | iex` で流し込む bootstrap.ps1 は逆に **BOM 無し + `exit` 無し**
  （BOM は irm の戻り値に U+FEFF として残り得る / iex の中の exit はユーザーのウィンドウを閉じる）。
- 更新モードで保持するファイル一覧が `core/backup.py` のデータファイル一覧を漏れなく含む
  （店舗固有データを更新で消さない）。
- pwsh / powershell があれば構文解析と `-DryRun` の通し実行（GitHub の ubuntu ランナーには pwsh がある）。
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
CMD_FILES = [
    "install.cmd", "update.cmd", "uninstall.cmd",
    "start_logger.cmd", "start_monitor.cmd", "start_viewer.cmd", "start_ledger.cmd", "rfid_check.cmd",
]
PS1_FILES = ["installer/install.ps1", "installer/bootstrap.ps1"]


def _powershell() -> list[str] | None:
    for exe in ("pwsh", "powershell"):
        path = shutil.which(exe)
        if path:
            return [path, "-NoProfile"]
    return None


class TestFilesAndEncodings:
    @pytest.mark.parametrize("rel", CMD_FILES + PS1_FILES)
    def test_exists(self, rel):
        assert (ROOT / rel).is_file(), rel

    @pytest.mark.parametrize("rel", CMD_FILES)
    def test_cmd_is_ascii_crlf(self, rel):
        raw = (ROOT / rel).read_bytes()
        assert raw.isascii(), f"{rel}: cmd.exe は CP932 で読むので ASCII 以外を書かない"
        assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b""), f"{rel}: CRLF 固定"

    @pytest.mark.parametrize("rel", PS1_FILES)
    def test_ps1_is_crlf(self, rel):
        raw = (ROOT / rel).read_bytes()
        assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b""), f"{rel}: CRLF 固定"

    def test_install_ps1_has_utf8_bom(self):
        """-File で実行する install.ps1 は BOM 付き（5.1 は BOM 無しを ANSI として読み日本語が化ける）。"""
        raw = (ROOT / "installer/install.ps1").read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf"), "install.ps1: PowerShell 5.1 向けに UTF-8 BOM が要る"
        raw[3:].decode("utf-8")  # valid UTF-8

    def test_bootstrap_ps1_is_iex_safe(self):
        """bootstrap.ps1 は `irm … | iex` でユーザーの対話コンソールの中で実行される。

        - BOM は 5.1 の irm の戻り値の先頭に U+FEFF として残り得て iex が失敗するので付けない。
        - `exit` はユーザーの PowerShell ウィンドウごと閉じて結果が読めなくなるので書かない。
        - `& { }` で包み、変数や $ErrorActionPreference をセッションに残さない。
        """
        raw = (ROOT / "installer/bootstrap.ps1").read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf"), "bootstrap.ps1: iex 経由なので BOM を付けない"
        text = raw.decode("utf-8")
        assert not re.search(r"^\s*exit\b", text, re.M), "iex の中の exit は PowerShell ウィンドウを閉じる"
        assert re.search(r"^& \{", text, re.M), "全体を & { } で包む"
        assert "-ExecutionPolicy Bypass" in text and r"installer\install.ps1" in text

    def test_gitattributes_pins_crlf(self):
        text = (ROOT / ".gitattributes").read_text(encoding="utf-8")
        assert "*.cmd text eol=crlf" in text and "*.ps1 text eol=crlf" in text


class TestLaunchers:
    def test_cmd_wrappers_call_the_installer(self):
        for rel in ("install.cmd", "update.cmd", "uninstall.cmd"):
            text = (ROOT / rel).read_text(encoding="ascii")
            assert "-ExecutionPolicy Bypass" in text, rel
            assert r"installer\install.ps1" in text, rel
        assert "-Update" in (ROOT / "update.cmd").read_text(encoding="ascii")
        assert "-Uninstall" in (ROOT / "uninstall.cmd").read_text(encoding="ascii")

    def test_launchers_reference_existing_entry_points(self):
        for rel in ("start_logger.cmd", "start_monitor.cmd", "start_viewer.cmd", "start_ledger.cmd",
                    "rfid_check.cmd"):
            text = (ROOT / rel).read_text(encoding="ascii")
            assert r"venv\Scripts\python.exe" in text, rel
            for m in re.finditer(r'^"venv\\Scripts\\python\.exe" (\S+)', text, re.M):
                target = m.group(1).replace("\\", "/")
                assert (ROOT / target).is_file(), f"{rel}: {target} が無い"

    def test_logger_launcher_routes_logs_to_file(self):
        """--cli では RFID のログが入力行に割り込むので、ランチャは必ず --log-file を付ける（ISSUE-0034）。"""
        text = (ROOT / "start_logger.cmd").read_text(encoding="ascii")
        assert "--cli --log-file" in text

    def test_monitor_launcher_binds_lan(self):
        text = (ROOT / "start_monitor.cmd").read_text(encoding="ascii")
        assert "--host 0.0.0.0" in text and "--port 8790" in text

    def test_viewer_launcher_serves_customers_on_the_lan(self):
        """お客さんのスマホから開く（ADR-0059）。config を書き換えずに LAN へ出す。"""
        text = (ROOT / "start_viewer.cmd").read_text(encoding="ascii")
        assert "main.py --viewer-api --host 0.0.0.0 --port 8788" in text

    def test_every_shortcut_points_at_a_launcher(self):
        text = (ROOT / "installer/install.ps1").read_bytes()[3:].decode("utf-8")
        block = re.search(r"\$Shortcuts\s*=\s*@\((.*?)\n\)", text, re.S).group(1)
        targets = re.findall(r'Target\s*=\s*"([^"]+)"', block)
        assert "start_viewer.cmd" in targets
        for target in targets:
            assert target in CMD_FILES and (ROOT / target).is_file(), target


class TestUpdatePreservesShopData:
    def _preserved(self) -> tuple[set[str], set[str]]:
        text = (ROOT / "installer/install.ps1").read_bytes()[3:].decode("utf-8")
        files = re.search(r"\$PreservedFiles\s*=\s*@\((.*?)\)", text, re.S).group(1)
        dirs = re.search(r"\$PreservedDirs\s*=\s*@\((.*?)\)", text, re.S).group(1)
        return set(re.findall(r'"([^"]+)"', files)), set(re.findall(r'"([^"]+)"', dirs))

    def test_backup_data_files_are_all_preserved(self):
        """core/backup.py が「会計の source of truth」とする JSON は更新で上書きしない。"""
        backup_src = (ROOT / "core/backup.py").read_text(encoding="utf-8")
        data_files = set(re.findall(r'_ROOT\s*/\s*"([^"]+\.json)"', backup_src))
        assert data_files, "core/backup.py のデータ一覧を読めない（パターン変更?）"
        files, dirs = self._preserved()
        assert data_files <= files, f"更新で消える恐れ: {sorted(data_files - files)}"
        assert {"config.json", "rfid_cards.json", "menu.json"} <= files
        assert {"venv", "logs", "backups"} <= dirs

    def test_gitignored_runtime_files(self):
        text = (ROOT / ".gitignore").read_text(encoding="utf-8")
        assert "venv/" in text and "install.log" in text


class TestFieldFindings:
    """店舗 PC（Windows 11 Pro 初期状態）の実機導入で踏んだ罠の回帰ロック（2026-09-24）。"""

    def _install_ps1(self) -> str:
        return (ROOT / "installer/install.ps1").read_bytes()[3:].decode("utf-8")

    def test_winget_is_pinned_to_the_winget_source(self):
        """msstore ソースが証明書エラー（0x8a15005e）で落ちると、winget はソース指定を求めて何も入れない。"""
        assert '"--source", "winget"' in self._install_ps1()

    def test_python_org_fallback_after_winget_failure(self):
        text = self._install_ps1()
        body = re.search(r"function Install-Python \{(.*?)\n\}", text, re.S).group(1)
        assert "if (Find-Python) { return }" in body, "winget の成否は Find-Python で判定する"
        assert "Install-PythonFromPythonOrg" in body, "winget で入らなければ python.org に切り替える"
        assert "InstallLauncherAllUsers=0" in text, "per-user 導入でランチャが昇格を求めないように"

    def test_update_mode_continues_with_the_updated_installer(self):
        """-Update はファイルを差し替えても実行中の古いスクリプトで続きを走らせてしまう → 起動し直す。"""
        assert "-File $PSCommandPath" in self._install_ps1()

    def test_update_remembers_the_branch_it_was_installed_from(self):
        """update.cmd は -Branch を渡さない。覚えていないと既定の verify-v1 で別ブランチの導入を上書きする。"""
        text = self._install_ps1()
        assert '$PSBoundParameters.ContainsKey("Branch")' in text
        assert "branch.txt" in text
        boot = (ROOT / "installer/bootstrap.ps1").read_bytes().decode("utf-8")
        assert "branch.txt" in boot, "古い install.ps1 のままでも効くよう bootstrap 側でも書く"
        assert "installer/branch.txt" in (ROOT / ".gitignore").read_text(encoding="utf-8")

    def test_web_requests_skip_the_slow_progress_bar(self):
        for rel in PS1_FILES:
            raw = (ROOT / rel).read_bytes()
            text = raw[3:].decode("utf-8") if raw.startswith(b"\xef\xbb\xbf") else raw.decode("utf-8")
            assert '$ProgressPreference = "SilentlyContinue"' in text, rel


@pytest.mark.skipif(_powershell() is None, reason="pwsh / powershell が無い環境")
class TestPowerShell:
    @pytest.mark.parametrize("rel", PS1_FILES)
    def test_parses(self, rel):
        ps = _powershell()
        script = (
            "$errors = $null; $null = [System.Management.Automation.Language.Parser]::ParseFile("
            f"'{(ROOT / rel).as_posix()}', [ref]$null, [ref]$errors); "
            "if ($errors.Count -gt 0) { $errors | ForEach-Object { Write-Output $_.Message }; exit 1 } "
            "else { Write-Output 'parse ok'; exit 0 }"
        )
        r = subprocess.run(ps + ["-Command", script], capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stdout + r.stderr

    @pytest.mark.parametrize("extra", [[], ["-Update"]], ids=["install", "update"])
    def test_dry_run_walks_the_whole_flow(self, tmp_path: Path, extra: list[str]):
        """-DryRun は何も変更せずに全ステップを通る（ロジックの通し検査。Windows 以外でも動く）。"""
        ps = _powershell()
        # アプリフォルダ = installer/ の親。tmp にコピーして実行し、リポジトリを汚さない。
        app = tmp_path / "app"
        (app / "installer").mkdir(parents=True)
        shutil.copy(ROOT / "installer/install.ps1", app / "installer/install.ps1")
        shutil.copy(ROOT / "config_default.json", app / "config_default.json")
        r = subprocess.run(
            ps + ["-ExecutionPolicy", "Bypass", "-File", str(app / "installer/install.ps1"),
                  "-DryRun", "-NonInteractive", "-SkipModel", *extra],
            capture_output=True, text=True, timeout=300,
        )
        assert r.returncode == 0, r.stdout + r.stderr
        assert "完了" in r.stdout
        assert not (app / "install.log").exists()      # DryRun は書かない
        assert not (app / "config.json").exists()
        assert not (app / "venv").exists()

    def test_update_without_branch_uses_the_remembered_one(self, tmp_path: Path):
        ps = _powershell()
        app = tmp_path / "app"
        (app / "installer").mkdir(parents=True)
        shutil.copy(ROOT / "installer/install.ps1", app / "installer/install.ps1")
        shutil.copy(ROOT / "config_default.json", app / "config_default.json")
        (app / "installer/branch.txt").write_text("feature/store-pc\n", encoding="ascii")
        r = subprocess.run(
            ps + ["-ExecutionPolicy", "Bypass", "-File", str(app / "installer/install.ps1"),
                  "-DryRun", "-NonInteractive", "-SkipModel", "-Update"],
            capture_output=True, text=True, timeout=300,
        )
        assert r.returncode == 0, r.stdout + r.stderr
        assert "refs/heads/feature/store-pc" in r.stdout      # verify-v1 に戻らない
        assert (app / "installer/branch.txt").read_text(encoding="ascii").strip() == "feature/store-pc"
