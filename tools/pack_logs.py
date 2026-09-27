"""tools/pack_logs.py

テストのログを **1 つの zip** にまとめる（開発者にレビューしてもらうため, 2026-09-27）。

店舗 PC で「ログをまとめる (送付用)」（`pack_logs.cmd`）をダブルクリックすると、直近 12 時間のセッションの
ファイルをデスクトップの `pokerlogs_<日時>.zip` にまとめ、エクスプローラでその zip を選んだ状態で開く。
その 1 ファイルをチャットに添付すればよい。

入るもの（セッションごとに `<セッションID>/` の下）:
- `logs/<sid>.json`（ハンドの記録）/ `.events.jsonl`（センサーの入力）/ `.transcripts.jsonl`（聞き取った文）/
  `.table_state.json(l)`（卓状態）/ `.ground_truth.json`（真のアクション）/ `.control.jsonl` など `<sid>.*` の全部
- `logs/audio/<sid>/*.wav`（発話の音声。合計 25 MB までなら自動で入れる。`--audio` / `--no-audio` で指定）
- `pokerapp.log` はそのセッションの時間帯の行だけ / `config.json`（トークン類は伏せる）/ `rfid_cards.json` /
  `manifest.json`（入れたもの・インストールしたコードの指紋）

使い方:

    python tools/pack_logs.py                  # 直近 12 時間のセッション → デスクトップに zip
    python tools/pack_logs.py --hours 48       # 直近 48 時間
    python tools/pack_logs.py --session <sid>  # セッションを指定（複数回可）
    python tools/pack_logs.py --text           # zip を添付できないとき: テキスト 1 ファイル（音声なし）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import subprocess
import sys
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent

APP_LOG = "pokerapp.log"
# 音声はこの合計サイズまでなら既定で入れる（チャットに添付できる大きさに収める）
AUDIO_AUTO_LIMIT = 25 * 1024 * 1024
# pokerapp.log から切り出す範囲の前後の余白（起動・終了の行を含める）
LOG_MARGIN = timedelta(seconds=60)
DEFAULT_HOURS = 12.0

_LOG_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d{3} ")
_CREATED = re.compile(r"Created session ([0-9A-Za-z_\-]+)")
_SID_TIME = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{6})_")
_SECRET = re.compile(r"token|secret|password|passwd|api_?key", re.I)
_SESSION_SUFFIXES = (
    ".json", ".events.jsonl", ".transcripts.jsonl", ".table_state.json", ".table_state.jsonl",
    ".control.jsonl", ".ground_truth.json",
)
# どの版のコードで記録したかを突き合わせるための指紋（改行は LF に揃えてから sha256）
FINGERPRINT_FILES = (
    "main.py", "integration/engine.py", "core/poker_engine.py", "audio/recognizer.py",
    "audio/recorder.py", "rfid/reader_thread.py",
)


class PackError(Exception):
    """まとめられない（セッションが無い・指定したセッションが無い）。"""


@dataclass
class Session:
    session_id: str
    files: list[Path]
    audio: list[Path] = field(default_factory=list)

    @property
    def updated(self) -> float:
        return max(p.stat().st_mtime for p in self.files)


# ───────────────────────── 集める ─────────────────────────


def find_sessions(log_dir: Path) -> dict[str, Session]:
    """`logs/` のファイルをセッション ID（ファイル名の最初の "." まで）でまとめる。"""
    groups: dict[str, list[Path]] = {}
    if not log_dir.is_dir():
        return {}
    for p in sorted(log_dir.iterdir()):
        if not p.is_file() or p.name.startswith(APP_LOG) or "." not in p.name:
            continue
        groups.setdefault(p.name.split(".", 1)[0], []).append(p)
    sessions: dict[str, Session] = {}
    for sid, files in groups.items():
        if not sid or not any(p.name == sid + suffix for p in files for suffix in _SESSION_SUFFIXES):
            continue
        audio_dir = log_dir / "audio" / sid
        audio = sorted(audio_dir.glob("*.wav")) if audio_dir.is_dir() else []
        sessions[sid] = Session(sid, files, audio)
    return sessions


def select_sessions(
    sessions: dict[str, Session], *, ids: Optional[list[str]] = None, hours: float = DEFAULT_HOURS,
    all_sessions: bool = False, now: Optional[datetime] = None,
) -> list[Session]:
    """指定が無ければ直近 `hours` 時間に更新したセッション（無ければいちばん新しい 1 つ）。古い順。"""
    if ids:
        missing = [i for i in ids if i not in sessions]
        if missing:
            raise PackError(f"セッションが見つかりません: {', '.join(missing)}")
        return sorted((sessions[i] for i in dict.fromkeys(ids)), key=lambda s: s.updated)
    ordered = sorted(sessions.values(), key=lambda s: s.updated)
    if all_sessions:
        return ordered
    now_ts = (now or datetime.now()).timestamp()
    recent = [s for s in ordered if now_ts - s.updated <= hours * 3600]
    return recent or ordered[-1:]


def _first_time(path: Path) -> Optional[float]:
    """JSON Lines の最初の行の時刻（発話の開始 → イベントの時刻）。"""
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    return None
                for key in ("utterance_start_ts", "timestamp"):
                    value = data.get(key) if isinstance(data, dict) else None
                    if isinstance(value, (int, float)):
                        return float(value)
                return None
    except OSError:
        return None
    return None


def scan_created(app_log: Path) -> dict[str, datetime]:
    """pokerapp.log の「Created session <sid>」の時刻（ハンドロガーの起動）。"""
    out: dict[str, datetime] = {}
    if not app_log.is_file():
        return out
    with app_log.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            if "Created session" not in line:
                continue
            ts, created = _LOG_TS.match(line), _CREATED.search(line)
            if ts and created:
                try:
                    out.setdefault(created.group(1), datetime.strptime(ts.group(1), "%Y-%m-%d %H:%M:%S"))
                except ValueError:
                    pass
    return out


def session_window(session: Session, created: dict[str, datetime]) -> tuple[datetime, datetime]:
    """pokerapp.log から切り出す時間帯（起動 − 余白 〜 最後に書いた時刻 + 余白）。"""
    starts: list[datetime] = []
    m = _SID_TIME.match(session.session_id)
    if m:
        try:
            starts.append(datetime.strptime(m.group(1) + m.group(2), "%Y-%m-%d%H%M%S"))
        except ValueError:
            pass
    if session.session_id in created:
        starts.append(created[session.session_id])
    for p in session.files:
        if p.name.endswith((".events.jsonl", ".transcripts.jsonl")):
            t = _first_time(p)
            if t is not None:
                starts.append(datetime.fromtimestamp(t))
    mtimes = [p.stat().st_mtime for p in session.files + session.audio]
    start = min(starts) if starts else datetime.fromtimestamp(min(mtimes))
    return start - LOG_MARGIN, datetime.fromtimestamp(max(mtimes)) + LOG_MARGIN


def slice_app_log(app_log: Path, windows: list[tuple[datetime, datetime]]) -> str:
    """pokerapp.log のうち、どれかの時間帯に入る行（時刻の無い続きの行 = traceback も含む）。"""
    if not app_log.is_file() or not windows:
        return ""
    out: list[str] = []
    keep = False
    with app_log.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            m = _LOG_TS.match(line)
            if m:
                try:
                    ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
                    keep = any(a <= ts <= b for a, b in windows)
                except ValueError:
                    pass
            if keep:
                out.append(line)
    return "".join(out)


def redact(value: Any) -> Any:
    """config のトークン・パスワード類を伏せる（キーの名前で判断）。"""
    if isinstance(value, dict):
        return {
            k: ("***" if _SECRET.search(str(k)) and isinstance(v, str) and v else redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def code_fingerprint(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for rel in FINGERPRINT_FILES:
        try:
            data = (root / rel).read_bytes().replace(b"\r\n", b"\n")
        except OSError:
            continue
        out[rel] = hashlib.sha256(data).hexdigest()[:16]
    return out


def _hand_count(session: Session) -> Optional[int]:
    for p in session.files:
        if p.name == f"{session.session_id}.json":
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                return None
            hands = data.get("hands") if isinstance(data, dict) else None
            return len(hands) if isinstance(hands, list) else None
    return None


# ───────────────────────── まとめる ─────────────────────────


def pack(
    log_dir: Path, out_dir: Path, *, root: Path = ROOT, session_ids: Optional[list[str]] = None,
    hours: float = DEFAULT_HOURS, all_sessions: bool = False, audio: Optional[bool] = None,
    text: bool = False, now: Optional[datetime] = None,
) -> tuple[Path, dict]:
    """ログを zip（`text=True` ならテキスト 1 ファイル）にまとめ、(出力先, manifest) を返す。"""
    sessions = find_sessions(log_dir)
    if not sessions:
        raise PackError(f"{log_dir} にセッションのログがありません")
    now = now or datetime.now()
    chosen = select_sessions(sessions, ids=session_ids, hours=hours, all_sessions=all_sessions, now=now)
    app_log = log_dir / APP_LOG
    created = scan_created(app_log)
    windows = [session_window(s, created) for s in chosen]
    audio_bytes = sum(p.stat().st_size for s in chosen for p in s.audio)
    include_audio = not text and (audio if audio is not None else audio_bytes <= AUDIO_AUTO_LIMIT)

    # (zip の中の名前, 中身 = bytes か音声のパス)
    entries: list[tuple[str, Any]] = []
    for s in chosen:
        for p in sorted(s.files):
            entries.append((f"{s.session_id}/{p.name}", p.read_bytes()))
        if include_audio:
            for p in s.audio:
                entries.append((f"{s.session_id}/audio/{p.name}", p))
    log_text = slice_app_log(app_log, windows)
    if log_text:
        entries.append((APP_LOG, log_text.encode("utf-8")))
    config_path = root / "config.json"
    if config_path.is_file():
        try:
            config = redact(json.loads(config_path.read_text(encoding="utf-8-sig")))
            entries.append(("config.json", json.dumps(config, ensure_ascii=False, indent=2).encode("utf-8")))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            entries.append(("config.json.unreadable", b""))
    cards = root / "rfid_cards.json"
    if cards.is_file():
        entries.append(("rfid_cards.json", cards.read_bytes()))

    branch = root / "installer" / "branch.txt"
    manifest = {
        "tool": "pack_logs",
        "created_at": now.isoformat(timespec="seconds"),
        "sessions": [
            {
                "session_id": s.session_id,
                "updated_at": datetime.fromtimestamp(s.updated).isoformat(timespec="seconds"),
                "log_window": [a.isoformat(timespec="seconds"), b.isoformat(timespec="seconds")],
                "hands": _hand_count(s),
                "files": sorted(p.name for p in s.files),
                "audio_files": len(s.audio),
            }
            for s, (a, b) in zip(chosen, windows)
        ],
        "audio_bytes": audio_bytes,
        "audio_included": include_audio,
        "app_log_lines": log_text.count("\n"),
        "branch": branch.read_text(encoding="utf-8").strip() if branch.is_file() else None,
        "code_fingerprint": code_fingerprint(root),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
    }
    entries.insert(0, ("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")))

    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y%m%d_%H%M%S")
    if text:
        out = out_dir / f"pokerlogs_{stamp}.txt"
        with out.open("w", encoding="utf-8", newline="\n") as f:
            f.write("# pokerlogs テキスト版（tools/pack_logs.py --text）。音声は入りません。\n")
            f.write('# 各ファイルは "===== FILE <名前> (<バイト数> bytes) =====" の行から始まります。\n')
            for name, body in entries:
                data = body if isinstance(body, bytes) else b""
                f.write(f"===== FILE {name} ({len(data)} bytes) =====\n")
                chunk = data.decode("utf-8", errors="replace")
                f.write(chunk if chunk.endswith("\n") or not chunk else chunk + "\n")
            f.write("===== END =====\n")
        return out, manifest
    out = out_dir / f"pokerlogs_{stamp}.zip"
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, body in entries:
            if isinstance(body, Path):
                zf.write(body, name, compress_type=zipfile.ZIP_STORED)   # WAV はほとんど縮まない
            else:
                zf.writestr(name, body)
    return out, manifest


# ───────────────────────── 起動 ─────────────────────────


def desktop_dir() -> Path:
    """デスクトップ（OneDrive へ移された場合も）。分からなければ logs/。"""
    if sys.platform == "win32":
        try:
            import ctypes

            buf = ctypes.create_unicode_buffer(1024)
            # CSIDL_DESKTOPDIRECTORY = 0x10
            if ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, buf) == 0 and buf.value:
                return Path(buf.value)
        except Exception:  # noqa: BLE001 — 取れなければ既定へ
            pass
    home_desktop = Path.home() / "Desktop"
    return home_desktop if home_desktop.is_dir() else ROOT / "logs"


def _log_dir_from_config(root: Path) -> Path:
    try:
        config = json.loads((root / "config.json").read_text(encoding="utf-8-sig"))
        configured = config.get("session", {}).get("log_dir")
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, AttributeError):
        configured = None
    path = Path(configured or "logs")
    return path if path.is_absolute() else (root / path)


def reveal(path: Path) -> None:
    """エクスプローラでファイルを選んだ状態で開く（Windows のみ）。"""
    if sys.platform != "win32":
        return
    try:
        subprocess.Popen(f'explorer /select,"{path}"')
    except OSError:
        pass


def _mb(n: int) -> str:
    return f"{n / 1024 / 1024:.1f} MB"


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="テストのログを 1 つの zip にまとめる（レビューに送るため）")
    ap.add_argument("--log-dir", help="ログのフォルダ（既定: config の session.log_dir = logs）")
    ap.add_argument("--out-dir", help="出力先（既定: デスクトップ）")
    ap.add_argument("--session", action="append", default=[], help="セッション ID（複数回可）")
    ap.add_argument("--hours", type=float, default=DEFAULT_HOURS, help="直近何時間のセッションか（既定 12）")
    ap.add_argument("--all", action="store_true", help="すべてのセッション")
    audio = ap.add_mutually_exclusive_group()
    audio.add_argument("--audio", dest="audio", action="store_true", default=None,
                       help="発話の音声を必ず入れる（既定: 合計 25 MB までなら入れる）")
    audio.add_argument("--no-audio", dest="audio", action="store_false", help="音声を入れない")
    ap.add_argument("--text", action="store_true", help="zip ではなくテキスト 1 ファイル（音声なし）")
    ap.add_argument("--no-open", action="store_true", help="できたファイルをエクスプローラで開かない")
    args = ap.parse_args(argv)

    log_dir = Path(args.log_dir) if args.log_dir else _log_dir_from_config(ROOT)
    out_dir = Path(args.out_dir) if args.out_dir else desktop_dir()
    try:
        out, manifest = pack(
            log_dir, out_dir, session_ids=args.session or None, hours=args.hours,
            all_sessions=args.all, audio=args.audio, text=args.text,
        )
    except PackError as e:
        print(f"[pack] {e}")
        return 1
    names = " / ".join(
        f"{s['session_id']}（ハンド {s['hands'] if s['hands'] is not None else '?'}）"
        for s in manifest["sessions"]
    )
    print(f"[pack] セッション {len(manifest['sessions'])} つ: {names}")
    n_audio = sum(s["audio_files"] for s in manifest["sessions"])
    if n_audio:
        if manifest["audio_included"]:
            print(f"[pack] 発話の音声 {n_audio} 個（{_mb(manifest['audio_bytes'])}）を入れました")
        elif args.text:
            print("[pack] テキスト版には音声は入りません")
        else:
            print(f"[pack] 発話の音声 {n_audio} 個（{_mb(manifest['audio_bytes'])}）は入れていません"
                  "（入れるなら --audio）")
    print(f"[pack] できました: {out}（{_mb(out.stat().st_size)}）")
    print("[pack] この 1 ファイルをチャットに添付してください。")
    if not args.no_open:
        reveal(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
