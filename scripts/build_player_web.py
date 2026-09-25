"""scripts/build_player_web.py

お客さん向け画面（`mobile/` の web 版）をビルドして `api/static/player/` に置く（ADR-0059）。

店舗 PC の viewer API（`python main.py --viewer-api`）がこのビルドを `/` で配信する。スマホは
「PC の IP:8788」を開くだけでよい（画面と API が同じ PC・同じポート）。店舗 PC に Node を
入れないため、ビルドはリポジトリに含める。`mobile/` のソースを変えたらこのスクリプトで
作り直す（作り直し忘れは `tests/test_player_web_build.py` が CI で落とす）。

ビルドの設定（`BUILD_SETTINGS`）:
- EXPO_PUBLIC_API_URL=/        画面と同じ origin の API（`/api/...`）を読む
- EXPO_PUBLIC_PLAYER_AUTH=off  PIN / LINE・Google の導線を出さない（名前を選ぶだけ）
- EXPO_PUBLIC_STAFF_TOKEN は渡さない（お客さんの画面にハンド訂正の導線を出さない）。
  `mobile/.env*` も読ませない（開発者の staff token が混ざらないように）。

使い方:
    python scripts/build_player_web.py          # ビルドして置き換える（要 Node + mobile/node_modules）
    python scripts/build_player_web.py --check  # ソースとビルドが揃っているか（ビルドしない）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MOBILE_DIR = REPO_ROOT / "mobile"
OUTPUT_DIR = REPO_ROOT / "api" / "static" / "player"
BUILD_INFO_NAME = "build-info.json"

BUILD_SETTINGS = {"api_url": "/", "player_auth": "off", "staff_token": False}

# ビルド結果に効くファイル。無いものは飛ばす（足したら hash が変わる）。
_SOURCE_FILES = (
    "App.tsx", "index.ts", "app.json", "package.json", "package-lock.json", "tsconfig.json",
    "babel.config.js", "metro.config.js",
)
_SOURCE_DIRS = ("src", "public", "assets")
_BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".ttf", ".otf", ".woff", ".woff2"}


def source_files() -> list[Path]:
    """ビルドの入力（テストを除く）を決定的な順序で返す。"""
    files = [MOBILE_DIR / name for name in _SOURCE_FILES if (MOBILE_DIR / name).is_file()]
    for name in _SOURCE_DIRS:
        root = MOBILE_DIR / name
        if root.is_dir():
            files.extend(
                p for p in root.rglob("*")
                if p.is_file() and not p.name.endswith((".test.ts", ".test.tsx"))
            )
    return sorted(files, key=lambda p: p.relative_to(MOBILE_DIR).as_posix())


def source_hash() -> str:
    """ソースとビルド設定の hash。Windows の checkout（CRLF）でも同じ値になるよう改行を揃える。"""
    digest = hashlib.sha256()
    digest.update(json.dumps(BUILD_SETTINGS, sort_keys=True).encode("utf-8"))
    for path in source_files():
        data = path.read_bytes()
        if path.suffix.lower() not in _BINARY_SUFFIXES:
            data = data.replace(b"\r\n", b"\n")
        digest.update(b"\0" + path.relative_to(MOBILE_DIR).as_posix().encode("utf-8") + b"\0")
        digest.update(hashlib.sha256(data).digest())
    return "sha256:" + digest.hexdigest()


def script_sources(index_html: str) -> list[str]:
    return re.findall(r'<script[^>]*\bsrc="([^"]+)"', index_html)


def check_build(output_dir: Path) -> list[str]:
    """ビルドそのものの問題（入口・script・設定）を返す。"""
    index = output_dir / "index.html"
    info_path = output_dir / BUILD_INFO_NAME
    if not index.is_file() or not info_path.is_file():
        return [f"ビルドがありません: {output_dir}"]
    problems: list[str] = []
    scripts = script_sources(index.read_text(encoding="utf-8"))
    if not scripts:
        problems.append("index.html に script がありません")
    for src in scripts:
        if not (output_dir / src.lstrip("/")).is_file():
            problems.append(f"index.html の script がありません: {src}")
    info = json.loads(info_path.read_text(encoding="utf-8"))
    for key, value in BUILD_SETTINGS.items():
        if info.get(key) != value:
            problems.append(f"{BUILD_INFO_NAME} の {key} が {info.get(key)!r}（期待 {value!r}）")
    return problems


def check() -> list[str]:
    """リポジトリのビルドの問題を返す（空 = mobile/ のソースと揃っている）。"""
    problems = check_build(OUTPUT_DIR)
    if problems:
        return problems
    info = json.loads((OUTPUT_DIR / BUILD_INFO_NAME).read_text(encoding="utf-8"))
    if info.get("source_hash") != source_hash():
        return ["mobile/ のソースがビルドと違います（python scripts/build_player_web.py で作り直す）"]
    return []


def _build_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("EXPO_PUBLIC_")}
    env.update({
        "EXPO_PUBLIC_API_URL": BUILD_SETTINGS["api_url"],
        "EXPO_PUBLIC_PLAYER_AUTH": BUILD_SETTINGS["player_auth"],
        "EXPO_NO_DOTENV": "1",
        "EXPO_NO_TELEMETRY": "1",
        "CI": "1",
    })
    return env


def build() -> None:
    npx = shutil.which("npx")
    if npx is None:
        raise SystemExit("npx が見つかりません（Node.js を入れてください）")
    if not (MOBILE_DIR / "node_modules").is_dir():
        raise SystemExit("mobile/node_modules がありません（mobile/ で npm ci を実行してください）")
    with tempfile.TemporaryDirectory(prefix="player-web-") as tmp:
        out = Path(tmp) / "web"
        subprocess.run(
            [npx, "expo", "export", "--platform", "web", "--clear", "--output-dir", str(out)],
            cwd=MOBILE_DIR, env=_build_env(), check=True,
        )
        info = {**BUILD_SETTINGS, "source_hash": source_hash()}
        (out / BUILD_INFO_NAME).write_text(
            json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        problems = check_build(out)
        if problems:
            raise SystemExit("ビルドが不完全です:\n" + "\n".join(problems))
        if OUTPUT_DIR.exists():
            shutil.rmtree(OUTPUT_DIR)
        shutil.copytree(out, OUTPUT_DIR)
    print(f"built: {OUTPUT_DIR.relative_to(REPO_ROOT)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true",
                        help="ビルドせず検証のみ（揃っていなければ exit 1）")
    args = parser.parse_args()
    if args.check:
        problems = check()
        for p in problems:
            print(p, file=sys.stderr)
        return 1 if problems else 0
    build()
    return 0


if __name__ == "__main__":
    sys.exit(main())
