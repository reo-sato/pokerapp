"""クラウドのイメージ（cloud/Dockerfile, ADR-0067 2e）が、クラウドのアプリの読むものをすべて持つこと。

イメージには店舗 PC の音声認識・RFID・データを入れない。COPY するフォルダだけで `cloud` のアプリが起き、
health と中継の受け口が答えることを、そのフォルダだけを写した場所で確かめる（docker が無い CI でも回る）。
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = ROOT / "cloud" / "Dockerfile"


def _copied() -> list[str]:
    paths = []
    for line in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        m = re.match(r"COPY\s+(\S+)\s+\S+$", line.strip())
        if m and m.group(1) != "pyproject.toml":
            paths.append(m.group(1))
    return paths


def test_the_image_copies_only_what_the_cloud_needs():
    assert _copied() == ["core", "api", "cloud", "docs/contracts/schemas"]
    ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").split()
    for secret in ("config.json", "cloud_credentials.json", "logs", "data", ".env"):
        assert secret in ignore


def test_the_image_installs_the_cloud_extra_only():
    cloud = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["optional-dependencies"]["cloud"]
    names = {re.split(r"[<>=\[ ]", dep)[0].lower() for dep in cloud}
    assert {"fastapi", "uvicorn"} <= names
    assert not names & {"faster-whisper", "pyaudio", "pyscard", "customtkinter"}


def test_the_app_starts_from_the_copied_folders(tmp_path):
    for rel in _copied():
        shutil.copytree(ROOT / rel, tmp_path / rel, ignore=shutil.ignore_patterns("__pycache__"))
    script = (
        "from fastapi.testclient import TestClient\n"
        "from cloud.app import CloudSettings, create_cloud_app, PLAYER_WEB_DIR\n"
        "from cloud.storage import MemoryStorage\n"
        "import cloud.records\n"
        "for name in set(cloud.records._SCHEMA_FOR.values()):\n"
        "    cloud.records._validator(name)\n"
        "app = create_cloud_app(MemoryStorage(), CloudSettings(admin_token='a' * 32, token_secret='s' * 32,"
        " player_web_dir=PLAYER_WEB_DIR))\n"
        "c = TestClient(app)\n"
        "assert c.get('/').status_code == 200\n"
        "assert c.get('/api/health').json()['status'] == 'ok'\n"
        "assert c.post('/api/cloud/v1/asr/transcriptions', content=b'x').status_code == 401\n"
        "import sys\n"
        "leaked = sorted(m for m in sys.modules if m.split('.')[0] in ('audio', 'integration', 'rfid', 'gui', 'tools'))\n"
        "assert not leaked, leaked\n"
    )
    result = subprocess.run([sys.executable, "-c", script], cwd=tmp_path, capture_output=True, text=True,
                            env={"PYTHONPATH": str(tmp_path), "PATH": "/usr/bin:/bin"}, timeout=60)
    assert result.returncode == 0, result.stderr[-2000:]
