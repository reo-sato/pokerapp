"""家の PC で真のアクションを入れる（店舗のログの zip → 入力画面 → 入れた分の zip）。

店では真のアクションの入力が間に合わないので、家で発話の音声を聞きながら入れる（オーナー, 2026-10-19）。

1. `pack_logs` の zip（分けた `_1of6.zip` … は全部）を、入力画面が読む形（平らな logs = `<sid>.json`・
   `audio/<sid>/*.wav`）で `home_logs/<zip の日時>/` に置く。2 回目からは置いてある真のアクションを上書きしない
   （家で入れた分を守る）。初めて置くときは店で入れた分を `<sid>.ground_truth.store.json` に写して残す。
2. 入力画面（`tools/ground_truth_ui.py` と同じもの）をこの PC だけで開き、ブラウザで開く。
3. ウィンドウで Enter を押すと、入れた真のアクションだけを `pokertruth_<日時>.zip` にまとめてデスクトップに置く
   （中は pack_logs と同じ並び = `<sid>/<sid>.ground_truth.json`。`eval_store.py <ログの zip…> <この zip>` の順で
   渡すと店で入れた分の代わりに読む）。

使い方（Windows）: zip を `home_truth.cmd` にドラッグする。何も渡さなければデスクトップとダウンロードから
いちばん新しい `pokerlogs_*` の組を探す。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import threading
import webbrowser
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.ground_truth_ui import make_server  # noqa: E402
from tools.pack_logs import desktop_dir  # noqa: E402

PORTS = range(8791, 8800)
GT_SUFFIX = ".ground_truth.json"
STORE_GT_SUFFIX = ".ground_truth.store.json"
_ZIP_NAME = re.compile(r"pokerlogs_(\d{8}_\d{6})(?:_(\d+)of(\d+))?", re.IGNORECASE)
_SID = re.compile(r"^[A-Za-z0-9_\-]{1,120}$")
_FILE = re.compile(r"^[A-Za-z0-9_\-.]{1,200}$")
# 入力画面に要らない・置くと邪魔になるもの（終わったテストの「進行中」を出さない）
_SKIP_SUFFIXES = (".live_hand.json",)


class SetError(Exception):
    """zip の組が使えない（欠けている・pokerlogs ではない）。"""


def zip_stamp(path: Path) -> Optional[tuple[str, Optional[int], Optional[int]]]:
    """`pokerlogs_<日時>[_NofM].zip` → (日時, N, M)。名前の前後に何か付いていても（ダウンロードの番号など）読む。"""
    m = _ZIP_NAME.search(path.name)
    if not m or path.suffix.lower() != ".zip":
        return None
    return m.group(1), (int(m.group(2)) if m.group(2) else None), (int(m.group(3)) if m.group(3) else None)


def pick_set(paths: list[Path]) -> tuple[str, list[Path]]:
    """渡された zip から 1 つの組（同じ日時）を選ぶ。複数の組なら新しい方。欠けていれば SetError。"""
    groups: dict[str, dict[int, Path]] = {}
    totals: dict[str, int] = {}
    for p in paths:
        info = zip_stamp(p)
        if info is None:
            continue
        stamp, part, total = info
        groups.setdefault(stamp, {}).setdefault(part or 1, p)
        totals[stamp] = max(totals.get(stamp, 1), total or 1)
    if not groups:
        raise SetError("pokerlogs_<日時>.zip が見つかりません（店舗 PC の「ログをまとめる (音声付き)」で作った zip を渡してください）")
    stamp = max(groups)
    have = groups[stamp]
    missing = [n for n in range(1, totals[stamp] + 1) if n not in have]
    if missing:
        raise SetError(
            f"pokerlogs_{stamp} の zip が足りません: " + "・".join(f"{n}of{totals[stamp]}" for n in missing)
            + "（分けた zip は全部いっしょに渡してください）"
        )
    return stamp, [have[n] for n in sorted(have)]


def find_zips(folders: list[Path]) -> list[Path]:
    out = []
    for folder in folders:
        if folder.is_dir():
            out.extend(p for p in folder.glob("*.zip") if zip_stamp(p) is not None)
    return out


def _target(name: str) -> Optional[tuple[str, str]]:
    """zip の中の名前 → (セッション, 置き場所の相対パス)。セッションのファイルでなければ None。"""
    parts = name.replace("\\", "/").split("/")
    if len(parts) == 2:
        sid, fname = parts
        if (_SID.match(sid) and _FILE.match(fname) and fname.startswith(sid + ".")
                and not fname.endswith(_SKIP_SUFFIXES)):
            return sid, fname
    if len(parts) == 3 and parts[1] == "audio":
        sid, _, fname = parts
        if _SID.match(sid) and _FILE.match(fname) and fname.lower().endswith(".wav"):
            return sid, f"audio/{sid}/{fname}"
    return None


def unpack(zips: list[Path], dest: Path) -> dict:
    """zip の組を入力画面の形で `dest` に置く。すでに置いてある真のアクションは上書きしない。"""
    dest.mkdir(parents=True, exist_ok=True)
    sessions: dict[str, dict] = {}
    for z in zips:
        with zipfile.ZipFile(z) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                target = _target(info.filename)
                if target is None:
                    continue
                sid, rel = target
                s = sessions.setdefault(sid, {"files": 0, "audio": 0, "store_truth": False, "kept_truth": False})
                out = dest / rel
                if rel.endswith(GT_SUFFIX) and "/" not in rel:
                    s["store_truth"] = True
                    store_copy = dest / (sid + STORE_GT_SUFFIX)
                    if not store_copy.exists():
                        store_copy.write_bytes(zf.read(info))
                    if out.exists():
                        s["kept_truth"] = True
                        continue
                out.parent.mkdir(parents=True, exist_ok=True)
                if out.exists() and out.stat().st_size == info.file_size and rel.startswith("audio/"):
                    s["audio"] += 1
                    continue
                with zf.open(info) as src, open(out, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                s["audio" if rel.startswith("audio/") else "files"] += 1
    return sessions


def pack_truth(log_dir: Path, stamp: str, out_dir: Path) -> Optional[Path]:
    """入れた真のアクションだけを zip に（`<sid>/<sid>.ground_truth.json`）。無ければ None。"""
    files = sorted(p for p in log_dir.glob("*" + GT_SUFFIX) if not p.name.endswith(STORE_GT_SUFFIX))
    if not files:
        return None
    rows = []
    for p in files:
        sid = p.name[: -len(GT_SUFFIX)]
        try:
            hands = len((json.loads(p.read_text(encoding="utf-8")).get("hands") or []))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError, AttributeError):
            hands = None
        rows.append({"session_id": sid, "hands": hands})
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"pokertruth_{stamp}.zip"
    manifest = {"tool": "home_truth", "source": f"pokerlogs_{stamp}",
                "created_at": datetime.now().isoformat(timespec="seconds"), "sessions": rows}
    tmp = out.with_suffix(".zip.tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        # manifest.json にしない: ログの zip と一緒に展開すると、ログの manifest を上書きする
        zf.writestr("truth_manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        for p in files:
            sid = p.name[: -len(GT_SUFFIX)]
            zf.write(p, f"{sid}/{p.name}")
    tmp.replace(out)
    return out


def _serve(log_dir: Path):
    last: Optional[OSError] = None
    for port in PORTS:
        try:
            return make_server(log_dir, "127.0.0.1", port, None, evaluation=False), port
        except OSError as e:     # ほかのものが使っている
            last = e
    raise SetError(f"入力画面を開けません（ポート {PORTS.start}〜{PORTS.stop - 1} が使えません: {last}）")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="家の PC で真のアクションを入れる（店舗のログの zip から）")
    ap.add_argument("zips", nargs="*", type=Path, help="pokerlogs_<日時>[_NofM].zip（分けた zip は全部）。省くとデスクトップとダウンロードから探す")
    ap.add_argument("--dest", type=Path, default=ROOT / "home_logs", help="置き場所（既定 home_logs/）")
    ap.add_argument("--out", type=Path, default=None, help="入れた分の zip を置く場所（既定 デスクトップ）")
    ap.add_argument("--no-browser", action="store_true", help="ブラウザを開かない")
    args = ap.parse_args(argv)

    try:
        zips = [p for p in args.zips if p.is_file()] or find_zips([desktop_dir(), Path.home() / "Downloads"])
        stamp, parts = pick_set(zips)
    except SetError as e:
        print(f"[家で入力] {e}")
        return 1
    log_dir = args.dest / stamp
    print(f"[家で入力] pokerlogs_{stamp}（zip {len(parts)} 個）を {log_dir} に置いています…")
    sessions = unpack(parts, log_dir)
    for sid, s in sorted(sessions.items()):
        note = "家で入れた分を使います" if s["kept_truth"] else ("店で入れた分から続けます" if s["store_truth"] else "")
        print(f"  セッション {sid[:8]}: 音声 {s['audio']} 件 {note}")
    try:
        server, port = _serve(log_dir)
    except SetError as e:
        print(f"[家で入力] {e}")
        return 1
    url = f"http://127.0.0.1:{port}/"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"[家で入力] 入力画面: {url}  （開かなければブラウザにこのアドレスを入れてください）")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        input("[家で入力] 入れ終わったら、このウィンドウで Enter を押してください（入れた分を zip にまとめます）\n")
    except (KeyboardInterrupt, EOFError):
        print()
    finally:
        server.shutdown()
        server.server_close()
    out = pack_truth(log_dir, stamp, args.out or desktop_dir())
    if out is None:
        print("[家で入力] 保存した真のアクションがありません")
    else:
        print(f"[家で入力] まとめました: {out}")
        print("[家で入力] この zip を送ってください（途中でやめても、もう一度同じ zip を渡せば続きから入れられます）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
