"""tools/pack_logs.py

テストのログを **1 つの zip** にまとめる（開発者にレビューしてもらうため, 2026-09-27）。

店舗 PC で「ログをまとめる (送付用)」（`pack_logs.cmd`）をダブルクリックすると、直近 12 時間のセッションの
ファイルをデスクトップの `pokerlogs_<日時>.zip` にまとめ、エクスプローラでその zip を選んだ状態で開く。
その 1 ファイルをチャットに添付すればよい。「ログをまとめる (音声付き・送付用)」（`pack_logs_audio.cmd` = `--audio`）は
発話の音声を必ず全部入れる。1 つの zip が `PART_LIMIT`（25 MB = チャットに添付できる大きさ）を超えるときは
`pokerlogs_<日時>_1of3.zip`・`_2of3.zip`… に分ける（1 つ目にログと音声の一部、2 つ目以降は音声の続き。全部を同じ
フォルダに展開すると 1 つの zip と同じになる。`tools/eval_store.py` は分けた zip をまとめて渡せば読める）。

入るもの（セッションごとに `<セッションID>/` の下）:
- `logs/<sid>.json`（ハンドの記録）/ `.events.jsonl`（センサーの入力）/ `.transcripts.jsonl`（聞き取った文）/
  `.table_state.json(l)`（卓状態）/ `.ground_truth.json`（真のアクション）/ `.rescored.jsonl`（音声を採点し直した
  結果, `tools/rescore_audio.py`）/ `.control.jsonl` など `<sid>.*` の全部
- `logs/audio/<sid>/*.wav`（発話の音声。合計 25 MB までなら自動で入れる。`--audio` で必ず入れる（大きければ zip を
  分ける）/ `--no-audio` で入れない）
- `pokerapp.log` はそのセッションの時間帯の行だけ / `config.json`（トークン類は伏せる）/ `rfid_cards.json` /
  `manifest.json`（入れたもの・インストールしたコードの指紋）
- 読み上げ集（`logs/corpus/<フォルダ>/`, `tools/read_corpus.py`）: 同じ時間帯に録ったものを `corpus/<フォルダ>/` に
  （`meta.json`・`labels.jsonl`・`transcripts.jsonl` と句ごとの音声。ずっと録った `full_*.wav` は大きいので入れない。
  `--session` を指定したときは入れない）

使い方:

    python tools/pack_logs.py                  # 直近 12 時間のセッション → デスクトップに zip
    python tools/pack_logs.py --hours 48       # 直近 48 時間
    python tools/pack_logs.py --session <sid>  # セッションを指定（複数回可）
    python tools/pack_logs.py --audio          # 音声を全部入れる（25 MB を超えたら zip を分ける）
    python tools/pack_logs.py --text           # zip を添付できないとき: テキスト 1 ファイル（音声なし）
"""
from __future__ import annotations

import argparse
import hashlib
import io
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
# 1 つの zip の大きさの上限（チャットに添付できる大きさ）。超えるときは音声を 2 つ目以降の zip に分ける
PART_LIMIT = 25 * 1024 * 1024
# zip の 1 項目あたりの見出しの大きさの見積もり（ローカル + 中央ディレクトリ + 名前）
_ENTRY_OVERHEAD = 256
# pokerapp.log から切り出す範囲の前後の余白（起動・終了の行を含める）
LOG_MARGIN = timedelta(seconds=60)
DEFAULT_HOURS = 12.0
# 「Created session」の行（ロガーの起動）を探すのに、セッションの最初の記録（卓状態の履歴はロガーの起動の数秒後から
# 書かれる）からさかのぼる長さ
CREATED_LOOKBACK = timedelta(minutes=10)
# pokerapp.log の中を時刻で二分探索するとき、この大きさまで狭めたら先頭から読む
_SEEK_SLACK = 1 << 20

_LOG_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d{3} ")
_CREATED = re.compile(r"Created session ([0-9A-Za-z_\-]+)")
_SID_TIME = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{6})_")
_SECRET = re.compile(r"token|secret|password|passwd|api_?key", re.I)
_SESSION_SUFFIXES = (
    ".json", ".events.jsonl", ".transcripts.jsonl", ".table_state.json", ".table_state.jsonl",
    ".control.jsonl", ".ground_truth.json",
)
# 読み上げ集（`tools/read_corpus.py`）の置き場所（logs の下）
CORPUS_DIR = "corpus"
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


@dataclass
class Corpus:
    """読み上げ集 1 つ（`logs/corpus/<フォルダ>/`, `tools/read_corpus.py`）。"""

    name: str
    folder: Path
    files: list[Path]                       # meta.json / labels.jsonl / transcripts.jsonl
    audio: list[Path] = field(default_factory=list)   # 句ごとの WAV（ずっと録った full_*.wav は大きいので入れない）

    @property
    def updated(self) -> float:
        return max(p.stat().st_mtime for p in self.files + self.audio)


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


def find_corpora(log_dir: Path) -> dict[str, Corpus]:
    """`logs/corpus/` の読み上げ集（`meta.json` のあるフォルダ）。"""
    base = log_dir / CORPUS_DIR
    if not base.is_dir():
        return {}
    out: dict[str, Corpus] = {}
    for folder in sorted(base.iterdir()):
        if not folder.is_dir() or not (folder / "meta.json").is_file():
            continue
        files = sorted(p for p in folder.iterdir() if p.is_file() and p.suffix in (".json", ".jsonl"))
        audio = sorted(p for p in folder.glob("*.wav") if not p.name.startswith("full_"))
        out[folder.name] = Corpus(folder.name, folder, files, audio)
    return out


def select_corpora(
    corpora: dict[str, Corpus], *, hours: float = DEFAULT_HOURS, all_sessions: bool = False,
    now: Optional[datetime] = None,
) -> list[Corpus]:
    """直近 `hours` 時間に更新した読み上げ集（`all_sessions` なら全部）。古い順。"""
    ordered = sorted(corpora.values(), key=lambda c: c.updated)
    if all_sessions:
        return ordered
    now_ts = (now or datetime.now()).timestamp()
    return [c for c in ordered if now_ts - c.updated <= hours * 3600]


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
                # 卓状態の履歴の最初の行 = ロガーの起動の数秒後（engine が 1 秒ごとに書き始める）
                updated = data.get("updated_at") if isinstance(data, dict) else None
                if isinstance(updated, str):
                    try:
                        return datetime.fromisoformat(updated).timestamp()
                    except ValueError:
                        return None
                return None
    except OSError:
        return None
    return None


def _ts_key(t: datetime) -> str:
    """pokerapp.log の行頭の時刻と同じ形（文字列のまま大小を比べられる）。"""
    return t.strftime("%Y-%m-%d %H:%M:%S")


def _line_ts(line: str) -> Optional[str]:
    """行頭の時刻（"YYYY-MM-DD HH:MM:SS"）。時刻の無い続きの行（traceback）は None。"""
    if len(line) > 23 and line[19] == "," and line[10] == " " and line[4] == "-":
        return line[:19]
    return None


def _open_from(app_log: Path, key: Optional[str]) -> io.TextIOWrapper:
    """pokerapp.log を、時刻が `key` 以上の最初の行の少し手前から読めるように開く。

    ログは時刻の順に追記されるので二分探索で飛ぶ（店舗 2026-10-01: 閉じたマイクの警告で 3.7 GB になったログを、
    まとめるたびに頭から全部読んでいて長い時間がかかった）。
    """
    raw = app_log.open("rb")
    if key is not None:
        raw.seek(0, 2)
        lo, hi = 0, raw.tell()
        while hi - lo > _SEEK_SLACK:
            mid = (lo + hi) // 2
            raw.seek(mid)
            raw.readline()                       # 途中から読んだ行は捨てる
            ts = None
            for _ in range(1000):                # 時刻の無い続きの行を飛ばす
                line = raw.readline()
                if not line:
                    break
                ts = _line_ts(line.decode("utf-8", errors="replace"))
                if ts is not None:
                    break
            if ts is None or ts >= key:
                hi = mid
            else:
                lo = mid
        raw.seek(lo)
        if lo:
            raw.readline()
    return io.TextIOWrapper(raw, encoding="utf-8", errors="replace")


def scan_created(app_log: Path, since: Optional[datetime] = None,
                 until: Optional[datetime] = None) -> dict[str, datetime]:
    """pokerapp.log の「Created session <sid>」の時刻（ハンドロガーの起動）。`since`〜`until` の間だけ読む。"""
    out: dict[str, datetime] = {}
    if not app_log.is_file():
        return out
    end = _ts_key(until) if until is not None else None
    with _open_from(app_log, _ts_key(since) if since is not None else None) as f:
        for line in f:
            if end is not None:
                ts_text = _line_ts(line)
                if ts_text is not None and ts_text > end:
                    break
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
        if p.name.endswith((".events.jsonl", ".transcripts.jsonl", ".table_state.jsonl")):
            t = _first_time(p)
            if t is not None:
                starts.append(datetime.fromtimestamp(t))
    mtimes = [p.stat().st_mtime for p in session.files + session.audio]
    start = min(starts) if starts else datetime.fromtimestamp(min(mtimes))
    return start - LOG_MARGIN, datetime.fromtimestamp(max(mtimes)) + LOG_MARGIN


def slice_app_log(app_log: Path, windows: list[tuple[datetime, datetime]]) -> str:
    """pokerapp.log のうち、どれかの時間帯に入る行（時刻の無い続きの行 = traceback も含む）。

    同じ行（時刻を除いて同じ。続きの行ごと）が続けて出ていれば、最初の 1 つと「…さらに N 回」の 1 行に畳む
    （店舗 2026-10-01: 閉じたマイクの警告が 9 分で 3470 万行）。いちばん遅い時間帯の終わりで読むのをやめる。
    """
    if not app_log.is_file() or not windows:
        return ""
    keys = sorted((_ts_key(a), _ts_key(b)) for a, b in windows)
    last_end = max(b for _, b in keys)
    out: list[str] = []
    state = {"body": None, "repeats": 0, "last_ts": ""}

    def emit(record: list[str], ts: str) -> None:
        body = "".join([record[0][24:], *record[1:]])
        if body == state["body"]:
            state["repeats"] += 1
            state["last_ts"] = ts
            return
        close_run()
        out.extend(record)
        state["body"] = body

    def close_run() -> None:
        if state["repeats"]:
            out.append(f"{state['last_ts']} …（上の行がさらに {state['repeats']} 回続きました）\n")
        state["repeats"] = 0

    record: list[str] = []
    record_ts = ""
    keep = False
    with _open_from(app_log, keys[0][0]) as f:
        for line in f:
            ts = _line_ts(line)
            if ts is None:
                if keep and record:
                    record.append(line)
                continue
            if keep and record:
                emit(record, record_ts)
            record = []
            if ts > last_end:
                break
            keep = any(a <= ts <= b for a, b in keys)
            if keep:
                record = [line]
                record_ts = ts
            else:
                close_run()
                state["body"] = None             # 時間帯の外をはさんだら、同じ行でももう一度出す
        if keep and record:
            emit(record, record_ts)
    close_run()
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


def _corpus_manifest(corpus: Corpus) -> dict:
    meta: Any = {}
    try:
        meta = json.loads((corpus.folder / "meta.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        pass
    if not isinstance(meta, dict):
        meta = {}
    return {
        "name": corpus.name, "speaker": meta.get("speaker"), "round": meta.get("round"),
        "updated_at": datetime.fromtimestamp(corpus.updated).isoformat(timespec="seconds"),
        "files": [p.name for p in corpus.files], "audio_files": len(corpus.audio),
    }


# ───────────────────────── まとめる ─────────────────────────


def pack(
    log_dir: Path, out_dir: Path, *, root: Path = ROOT, session_ids: Optional[list[str]] = None,
    hours: float = DEFAULT_HOURS, all_sessions: bool = False, audio: Optional[bool] = None,
    text: bool = False, now: Optional[datetime] = None, part_bytes: Optional[int] = None,
) -> tuple[Path, dict]:
    """ログを zip（`text=True` ならテキスト 1 ファイル）にまとめ、(出力先, manifest) を返す。

    zip が `part_bytes`（既定 `PART_LIMIT`、0 = 分けない）を超えるときは音声を 2 つ目以降の zip に分ける。
    出力先は 1 つ目の zip、全部の名前は `manifest["parts"]`。
    """
    sessions = find_sessions(log_dir)
    corpora = find_corpora(log_dir)
    if not sessions and not corpora:
        raise PackError(f"{log_dir} にセッションのログがありません")
    now = now or datetime.now()
    chosen = (select_sessions(sessions, ids=session_ids, hours=hours, all_sessions=all_sessions, now=now)
              if sessions or session_ids else [])
    # 読み上げ集は、セッションを指定しないとき直近の分を一緒に入れる（無ければいちばん新しい 1 つ、セッションも無いとき）
    chosen_corpora = [] if session_ids else select_corpora(corpora, hours=hours, all_sessions=all_sessions, now=now)
    if not chosen and not chosen_corpora:
        chosen_corpora = sorted(corpora.values(), key=lambda c: c.updated)[-1:]
    app_log = log_dir / APP_LOG
    rough = [session_window(s, {}) for s in chosen]      # 起動の行を探す範囲（記録のファイルだけから）
    created = (scan_created(app_log, since=min(a for a, _ in rough) - CREATED_LOOKBACK,
                            until=max(b for _, b in rough)) if rough else {})
    windows = [session_window(s, created) for s in chosen]
    audio_bytes = (sum(p.stat().st_size for s in chosen for p in s.audio)
                   + sum(p.stat().st_size for c in chosen_corpora for p in c.audio))
    include_audio = not text and (audio if audio is not None else audio_bytes <= AUDIO_AUTO_LIMIT)

    # (zip の中の名前, 中身 = bytes か音声のパス)
    entries: list[tuple[str, Any]] = []
    for s in chosen:
        for p in sorted(s.files):
            entries.append((f"{s.session_id}/{p.name}", p.read_bytes()))
        if include_audio:
            for p in s.audio:
                entries.append((f"{s.session_id}/audio/{p.name}", p))
    for c in chosen_corpora:
        for p in c.files:
            entries.append((f"{CORPUS_DIR}/{c.name}/{p.name}", p.read_bytes()))
        if include_audio:
            for p in c.audio:
                entries.append((f"{CORPUS_DIR}/{c.name}/{p.name}", p))
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
        "corpora": [_corpus_manifest(c) for c in chosen_corpora],
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
    groups = split_entries(entries[1:], PART_LIMIT if part_bytes is None else part_bytes)
    names = ([f"pokerlogs_{stamp}.zip"] if len(groups) == 1
             else [f"pokerlogs_{stamp}_{i}of{len(groups)}.zip" for i in range(1, len(groups) + 1)])
    manifest["parts"] = names
    entries[0] = ("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
    groups[0].insert(0, entries[0])
    for name, group in zip(names, groups):
        with zipfile.ZipFile(out_dir / name, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for entry, body in group:
                if isinstance(body, Path):
                    zf.write(body, entry, compress_type=zipfile.ZIP_STORED)   # WAV はほとんど縮まない
                else:
                    zf.writestr(entry, body)
    return out_dir / names[0], manifest


def split_entries(entries: list[tuple[str, Any]], limit: int) -> list[list[tuple[str, Any]]]:
    """zip の中身を、1 つが `limit` バイト以下の組に分ける（0 以下 = 分けない）。

    ログ（bytes）はすべて 1 つ目に入れ、音声（Path）を順に詰めて、入りきらなければ次の組へ。大きさは圧縮前で
    見積もる（WAV は圧縮しないで入れるので見積もりどおり、ログは圧縮で小さくなる = 安全側）。
    """
    def size(body: Any) -> int:
        return (len(body) if isinstance(body, bytes) else body.stat().st_size) + _ENTRY_OVERHEAD

    logs = [e for e in entries if not isinstance(e[1], Path)]
    audio = [e for e in entries if isinstance(e[1], Path)]
    groups: list[list[tuple[str, Any]]] = [list(logs)]
    used = sum(size(body) for _, body in logs) + 4096          # manifest の見積もり
    for entry in audio:
        n = size(entry[1])
        if limit > 0 and used + n > limit and groups[-1]:
            groups.append([])
            used = 0
        groups[-1].append(entry)
        used += n
    return groups


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
                       help="発話の音声を必ず入れる（既定: 合計 25 MB までなら入れる）。zip が 25 MB を超えたら分ける")
    audio.add_argument("--no-audio", dest="audio", action="store_false", help="音声を入れない")
    ap.add_argument("--text", action="store_true", help="zip ではなくテキスト 1 ファイル（音声なし）")
    ap.add_argument("--no-open", action="store_true", help="できたファイルをエクスプローラで開かない")
    ap.add_argument("--part-mb", type=float, default=PART_LIMIT / 1024 / 1024,
                    help="1 つの zip の大きさの上限（MB, 既定 25。0 = 分けない）")
    args = ap.parse_args(argv)

    log_dir = Path(args.log_dir) if args.log_dir else _log_dir_from_config(ROOT)
    out_dir = Path(args.out_dir) if args.out_dir else desktop_dir()
    try:
        out, manifest = pack(
            log_dir, out_dir, session_ids=args.session or None, hours=args.hours,
            all_sessions=args.all, audio=args.audio, text=args.text,
            part_bytes=int(args.part_mb * 1024 * 1024),
        )
    except PackError as e:
        print(f"[pack] {e}")
        return 1
    names = " / ".join(
        f"{s['session_id']}（ハンド {s['hands'] if s['hands'] is not None else '?'}）"
        for s in manifest["sessions"]
    )
    print(f"[pack] セッション {len(manifest['sessions'])} つ: {names}")
    if manifest["corpora"]:
        corpora = " / ".join(f"{c['name']}（{c['speaker'] or '?'}・句の音声 {c['audio_files']}）" for c in manifest["corpora"])
        print(f"[pack] 読み上げ集 {len(manifest['corpora'])} つ: {corpora}")
    n_audio = (sum(s["audio_files"] for s in manifest["sessions"])
               + sum(c["audio_files"] for c in manifest["corpora"]))
    if n_audio:
        if manifest["audio_included"]:
            print(f"[pack] 発話の音声 {n_audio} 個（{_mb(manifest['audio_bytes'])}）を入れました")
        elif args.text:
            print("[pack] テキスト版には音声は入りません")
        else:
            print(f"[pack] 発話の音声 {n_audio} 個（{_mb(manifest['audio_bytes'])}）は入れていません"
                  "（入れるなら --audio）")
    parts = [out.parent / name for name in manifest.get("parts") or [out.name]]
    if len(parts) == 1:
        print(f"[pack] できました: {out}（{_mb(out.stat().st_size)}）")
        print("[pack] この 1 ファイルをチャットに添付してください。")
    else:
        print(f"[pack] 大きいので {len(parts)} 個の zip に分けました（1 つ {args.part_mb:g} MB まで）:")
        for part in parts:
            print(f"[pack]   {part}（{_mb(part.stat().st_size)}）")
        print(f"[pack] {len(parts)} 個すべてをチャットに添付してください。")
    if not args.no_open:
        reveal(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
