"""core/version.py — いま動いている版（ADR-0066）。

店舗 PC はインストーラ（`installer/install.ps1`）が、取ってきたブランチとコミットを
`installer/installed.json` に書く（更新のたび。git 管理外）。複数の店に配ると「どの店がどの版で動いて
いるか」が問い合わせの最初の質問になるので、API（`/api/health`）・店舗サーバーのログ・`main.py --version`
で同じものを見せる。

- `branch`: 取得元のブランチ（リリース版 = `stable` / 開発版 = `verify-v1` など）
- `commit`: コミット（40 桁。取れなかったときは空）
- `installed_at`: 更新した時刻（店舗 PC の時計）

開発の手元（インストーラを使わない checkout）では `installed.json` が無いので、git から読めれば使う。
"""
from __future__ import annotations

import json
import subprocess
from importlib import metadata
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALLED_PATH = REPO_ROOT / "installer" / "installed.json"
RELEASE_BRANCH = "stable"


def package_version() -> str:
    try:
        return metadata.version("pokerapp")
    except metadata.PackageNotFoundError:
        return "unknown"


def _read_installed(path: Path) -> Optional[dict]:
    try:
        info = json.loads(path.read_text(encoding="utf-8-sig"))   # PowerShell 5.1 は BOM を付ける
    except (OSError, ValueError):
        return None
    return info if isinstance(info, dict) else None


def _from_git(root: Path) -> Optional[dict]:
    if not (root / ".git").exists():
        return None
    try:
        commit = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True,
                                text=True, timeout=5, check=True).stdout.strip()
        branch = subprocess.run(["git", "-C", str(root), "rev-parse", "--abbrev-ref", "HEAD"],
                                capture_output=True, text=True, timeout=5, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return {"branch": branch, "commit": commit, "installed_at": None}


def version_info(installed_path: Path = INSTALLED_PATH, root: Path = REPO_ROOT) -> dict:
    """いま動いている版。分からない項目は None。"""
    info = _read_installed(installed_path) or _from_git(root) or {}
    commit = str(info.get("commit") or "") or None
    branch = str(info.get("branch") or "") or None
    return {
        "version": package_version(),
        "branch": branch,
        "commit": commit,
        "commit_short": commit[:7] if commit else None,
        "installed_at": info.get("installed_at") or None,
        "release": branch == RELEASE_BRANCH if branch else None,
    }


def describe(info: Optional[dict] = None) -> str:
    """人が読む 1 行（例: 「リリース版 stable @ 9ffc978（2026-10-11T12:00:00 に更新）」）。"""
    info = info or version_info()
    if not info.get("branch") and not info.get("commit"):
        return f"版 {info.get('version')}（取得元は記録されていません）"
    kind = "リリース版" if info.get("release") else "開発版"
    text = f"{kind} {info.get('branch') or '?'} @ {info.get('commit_short') or '?'}"
    if info.get("installed_at"):
        text += f"（{info['installed_at']} に更新）"
    return text
