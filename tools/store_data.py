"""店舗のテスト記録を、ほかのセッションからも使えるようにリポジトリに置く（オーナー 2026-10-28）。

- 文字のログ（音声を除いた pokerlogs・家で入れた真のアクション・聞き取りの印）は `data/store/<YYYY-MM-DD>/` に置く
  （開発のブランチ。clone すればすぐ使える）。一覧は `data/store/README.md`。
- 発話の音声（1 回のテストで 200 MB 前後）は、データ専用のブランチ `store-data`（開発の履歴とつながらない）の
  `audio/<YYYY-MM-DD>/` に元の分割 zip のまま置く。要るときだけ `audio` で取り出す（開発のブランチの clone を重くしない）。
  公開（Artifact など）はしない。

    python tools/store_data.py add <pokerlogs の zip…> [--truth <pokertruth の zip>…] [--labels <listen_*.json>…]
    python tools/store_data.py stash-audio <pokerlogs の zip…>     # 音声の zip を store-data に入れてコミット（push は別）
    python tools/store_data.py audio 2026-10-26 --out <フォルダ>     # 音声の zip を取り出す
    python tools/store_data.py list
    python tools/store_data.py prune-audio [--days 90] [--dry-run]   # 90 日を過ぎた音声を履歴ごと消す（push は別）

音声は 90 日で消す（店舗 PC と同じ約束 = ADR-0072 D2、オーナー 2026-11-18「store-data も合わせる」）。`prune-audio` は
期限を過ぎた日付の音声を除いた木で store-data を親の無い 1 つのコミットに作り直す（前の履歴に音声が残らない）。
push は `git push --force-with-lease=store-data:<前の先頭> origin store-data`（表示されるコマンド）。

使うときは zip を並べて渡す（真のアクションは後に並べた方が上書きする）:
    TZ=Asia/Tokyo python tools/eval_store.py data/store/2026-10-26/*.zip
"""
from __future__ import annotations

import argparse
import io
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

STORE = ROOT / "data" / "store"
FIXTURES = ROOT / "tests" / "fixtures" / "store"
AUDIO_BRANCH = "store-data"
INDEX_START = "<!-- index:start -->"
INDEX_END = "<!-- index:end -->"
_UPLOAD_PREFIX = re.compile(r"^[0-9a-f]{8}-")                 # アップロードの名前の頭（「f9f3586b-」）
_STAMP = re.compile(r"(pokerlogs|pokertruth)_(\d{8})_(\d{6})")


def clean_name(path: Path) -> str:
    return _UPLOAD_PREFIX.sub("", path.name)


def stamp_of(name: str) -> Optional[tuple[str, str, str]]:
    """(種類, 日付 YYYY-MM-DD, 「YYYYMMDD_HHMMSS」)。名前が違えば None。"""
    m = _STAMP.search(name)
    if not m:
        return None
    d = m.group(2)
    return m.group(1), f"{d[:4]}-{d[4:6]}-{d[6:]}", f"{m.group(2)}_{m.group(3)}"


def _batches(zips: list[Path], kind: str) -> dict[str, list[Path]]:
    out: dict[str, list[Path]] = {}
    for z in zips:
        st = stamp_of(clean_name(z))
        if st is None or st[0] != kind:
            raise SystemExit(f"{z.name}: {kind}_<日付>_<時刻>.zip の名前ではありません")
        out.setdefault(st[2], []).append(z)
    return out


def write_text_zip(parts: list[Path], out: Path) -> int:
    """分割 zip から音声（.wav）を除いて 1 つの zip にまとめる。入れたファイルの数。"""
    out.parent.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for part in sorted(parts, key=clean_name):
            with zipfile.ZipFile(part) as src:
                for info in src.infolist():
                    if info.is_dir() or info.filename.lower().endswith(".wav") or info.filename in seen:
                        continue
                    seen.add(info.filename)
                    dst.writestr(info, src.read(info.filename))
    return len(seen)


def _truth_created(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as zf:
            return str(json.loads(zf.read("truth_manifest.json")).get("created_at") or "")
    except (KeyError, zipfile.BadZipFile, json.JSONDecodeError):
        return ""


def latest_truth(zips: list[Path]) -> Path:
    """同じ名前の pokertruth が複数あれば、`truth_manifest.json` の作った時刻がいちばん新しいもの。"""
    return max(zips, key=_truth_created)


def _sessions_in(path: Path) -> set[str]:
    with zipfile.ZipFile(path) as zf:
        return {n.split("/")[0] for n in zf.namelist() if n.endswith(".events.jsonl") or n.endswith(".ground_truth.json")}


def add(logs: list[Path], truths: list[Path], labels: list[Path], store: Path = STORE) -> list[Path]:
    written: list[Path] = []
    for stamp, parts in _batches(logs, "pokerlogs").items():
        _, date, _ = stamp_of(stamp_name := f"pokerlogs_{stamp}")
        out = store / date / f"{stamp_name}_text.zip"
        n = write_text_zip(parts, out)
        audio = sorted(clean_name(p) for p in parts)
        (store / date / f"{stamp_name}_audio.json").write_text(
            json.dumps({"branch": AUDIO_BRANCH, "path": f"audio/{date}/", "zips": audio}, ensure_ascii=False, indent=1)
            + "\n", encoding="utf-8")
        print(f"{date}/{out.name}: {n} ファイル（音声の zip {len(audio)} 個は {AUDIO_BRANCH} ブランチ）")
        written.append(out)
    for stamp, versions in _batches(truths, "pokertruth").items():
        _, date, _ = stamp_of(f"pokertruth_{stamp}")
        out = store / date / f"pokertruth_{stamp}.zip"
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(latest_truth(versions), out)
        print(f"{out.name}: 真のアクション（{len(versions)} 版のうち最新）")
        written.append(out)
    for label in labels:
        name = clean_name(label)
        m = re.match(r"listen_([0-9a-f]{8})", name)
        folder = _folder_of(m.group(1), store) if m else None
        if folder is None:
            raise SystemExit(f"{label.name}: どの日のセッションか分かりません（先に pokerlogs を add してください）")
        shutil.copyfile(label, folder / name)
        print(f"{folder.name}/{name}: 聞き取りの印")
        written.append(folder / name)
    write_index(store)
    return written


def _folder_of(sid8: str, store: Path) -> Optional[Path]:
    for z in sorted(store.glob("*/pokerlogs_*_text.zip")):
        if any(s.startswith(sid8) for s in _sessions_in(z)):
            return z.parent
    return None


# ――― 一覧 ―――

def session_rows(store: Path = STORE) -> list[dict]:
    """セッションごとの一覧の行（同じセッションが複数の日のログにあれば、最初の日の行にまとめる）。"""
    from tools.eval_store import _read_json, find_sessions, open_input, truth_hands, truth_memos

    rows: dict[str, dict] = {}
    for folder in sorted(p for p in store.iterdir() if p.is_dir()):
        texts = sorted(folder.glob("pokerlogs_*_text.zip"))
        truths = sorted(folder.glob("pokertruth_*.zip"))
        if not texts:
            continue
        root, tmp = open_input([*texts, *truths])
        try:
            for files in find_sessions(root):
                record = _read_json(files.path(".json")) or {}
                hands = [h for h in record.get("hands") or [] if isinstance(h, dict)]
                gt_file = _read_json(files.path(".ground_truth.json"))
                gt = truth_hands(gt_file)["hands"]
                sid = files.session_id
                started = next((h.get("started_at") for h in hands if h.get("started_at")), "") or ""
                row = rows.setdefault(sid, {
                    "session": sid[:8], "date": started[:10] or folder.name, "folders": [],
                    "hands": len(hands), "truth": len(gt), "memos": len(truth_memos(gt_file)),
                    "truth_from": "", "labels": [], "fixture": any(FIXTURES.glob(f"*-{sid[:8]}")),
                })
                row["folders"].append(folder.name)
                if len(gt) >= row["truth"]:
                    row["truth"], row["memos"] = len(gt), len(truth_memos(gt_file))
                    row["truth_from"] = next((t.name for t in truths if sid in _sessions_in(t)), "店舗で入力")
                row["labels"] += [p.name for p in folder.glob(f"listen_{sid[:8]}*.json")]
        finally:
            if tmp is not None:
                shutil.rmtree(tmp, ignore_errors=True)
    return sorted((r for r in rows.values() if r["hands"] or r["truth"]), key=lambda r: (r["date"], r["session"]))


def index_table(rows: list[dict]) -> str:
    lines = ["| 日付 | セッション | ハンド | 真のアクション | メモ | 聞き取りの印 | 開発データ | ログの置き場所 |",
             "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        truth = f"{r['truth']}（{r['truth_from']}）" if r["truth"] else "—"
        lines.append(f"| {r['date']} | {r['session']} | {r['hands']} | {truth} | {r['memos'] or '—'} | "
                     f"{'・'.join(r['labels']) or '—'} | {'入れた' if r['fixture'] else '入れていない'} | "
                     f"{'・'.join(f'`{f}/`' for f in r['folders'])} |")
    return "\n".join(lines)


def write_index(store: Path = STORE) -> None:
    readme = store / "README.md"
    text = readme.read_text(encoding="utf-8") if readme.exists() else f"{INDEX_START}\n{INDEX_END}\n"
    head, _, rest = text.partition(INDEX_START)
    _, _, tail = rest.partition(INDEX_END)
    readme.write_text(f"{head}{INDEX_START}\n{index_table(session_rows(store))}\n{INDEX_END}{tail}", encoding="utf-8")


# ――― 音声（store-data ブランチ）―――

def _git(*args: str, cwd: Path = ROOT, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=check)


def audio_ref() -> str:
    """取り出しに使う参照（取ってきた origin/store-data があればそれ、無ければ手元の store-data）。"""
    for ref in (f"origin/{AUDIO_BRANCH}", AUDIO_BRANCH):
        if _git("rev-parse", "--verify", "--quiet", ref, check=False).returncode == 0:
            return ref
    raise SystemExit(f"{AUDIO_BRANCH} ブランチがありません（git fetch origin {AUDIO_BRANCH} を確かめてください）")


def archive_args(ref: str, date: str) -> list[str]:
    return ["archive", "--format=tar", ref, f"audio/{date}"]


def fetch_audio(date: str, out: Path) -> list[Path]:
    _git("fetch", "origin", f"{AUDIO_BRANCH}:refs/remotes/origin/{AUDIO_BRANCH}", check=False)
    tar = _git(*archive_args(audio_ref(), date)).stdout
    out.mkdir(parents=True, exist_ok=True)
    got = []
    with tarfile.open(fileobj=io.BytesIO(tar)) as tf:
        for m in tf.getmembers():
            if m.isfile() and m.name.endswith(".zip"):
                target = out / Path(m.name).name
                target.write_bytes(tf.extractfile(m).read())
                got.append(target)
    return got


def stash_audio(zips: list[Path]) -> None:
    """音声の zip を store-data ブランチ（別の作業用フォルダ）に入れてコミットする。push は `git push origin store-data`。"""
    work = Path(tempfile.mkdtemp(prefix="store_data_"))
    try:
        has_local = _git("rev-parse", "--verify", "--quiet", AUDIO_BRANCH, check=False).returncode == 0
        if not has_local:
            _git("fetch", "origin", f"{AUDIO_BRANCH}:{AUDIO_BRANCH}", check=False)
            has_local = _git("rev-parse", "--verify", "--quiet", AUDIO_BRANCH, check=False).returncode == 0
        if has_local:
            _git("worktree", "add", str(work / "wt"), AUDIO_BRANCH)
        else:
            _git("worktree", "add", "--detach", str(work / "wt"))
            _git("checkout", "--orphan", AUDIO_BRANCH, cwd=work / "wt")
            _git("rm", "-rf", "--quiet", ".", cwd=work / "wt", check=False)
            (work / "wt" / "README.md").write_text(
                "店舗のテスト記録の音声（pokerlogs の分割 zip）。開発のブランチの `data/store/` と組で使う。"
                "取り出しは `python tools/store_data.py audio <日付> --out <フォルダ>`。公開しない。\n", encoding="utf-8")
        wt = work / "wt"
        for z in zips:
            _, date, _ = stamp_of(clean_name(z))
            dest = wt / "audio" / date / clean_name(z)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(z, dest)
        _git("add", "-A", cwd=wt)
        if _git("diff", "--cached", "--quiet", cwd=wt, check=False).returncode != 0:
            stamps = sorted({stamp_of(clean_name(z))[2] for z in zips})
            _git("commit", "-q", "-m", f"音声: {'・'.join(stamps)}", cwd=wt)
        print(f"{AUDIO_BRANCH} にコミットしました。push: git push origin {AUDIO_BRANCH}")
    finally:
        _git("worktree", "remove", "--force", str(work / "wt"), check=False)
        shutil.rmtree(work, ignore_errors=True)


KEEP_DAYS = 90
_DATE_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def expired_dates(dates: list[str], today: str, days: int = KEEP_DAYS) -> list[str]:
    """日付のフォルダ（YYYY-MM-DD = テストの日）のうち、`days` 日を過ぎたもの（その日から `days` 日たった日に消す）。"""
    from datetime import date, timedelta

    limit = date.fromisoformat(today) - timedelta(days=days)
    return sorted(d for d in dates if _DATE_DIR.match(d) and date.fromisoformat(d) <= limit)


def prune_audio(today: str, days: int = KEEP_DAYS, *, ref: Optional[str] = None, cwd: Path = ROOT,
                dry_run: bool = False) -> tuple[list[str], Optional[str], Optional[str]]:
    """期限を過ぎた日付の音声を除き、store-data を親の無い 1 つのコミットに作り直す（手元の `refs/heads/store-data`）。
    (消した日付, 前の先頭, 新しい先頭)。消すものが無ければ (空, 前の先頭, None)。push はしない。"""
    if ref is None:
        for cand in (AUDIO_BRANCH, f"origin/{AUDIO_BRANCH}"):
            if _git("rev-parse", "--verify", "--quiet", cand, cwd=cwd, check=False).returncode == 0:
                ref = cand
                break
        else:
            raise SystemExit(f"{AUDIO_BRANCH} ブランチがありません（git fetch origin {AUDIO_BRANCH}）")
    old = _git("rev-parse", ref, cwd=cwd).stdout.decode().strip()
    root = _git("ls-tree", old, cwd=cwd).stdout.decode().splitlines()
    audio = next((line for line in root if line.endswith("\taudio")), None)
    if audio is None:
        return [], old, None
    entries = _git("ls-tree", f"{old}:audio", cwd=cwd).stdout.decode().splitlines()
    gone = expired_dates([e.split("\t", 1)[1] for e in entries], today, days)
    if not gone or dry_run:
        return gone, old, None
    kept = [e for e in entries if e.split("\t", 1)[1] not in gone]
    lines = [line for line in root if not line.endswith("\taudio")]
    if kept:
        audio_tree = subprocess.run(["git", "mktree"], cwd=cwd, input=("\n".join(kept) + "\n").encode(),
                                    capture_output=True, check=True).stdout.decode().strip()
        lines.append(f"040000 tree {audio_tree}\taudio")
    tree = subprocess.run(["git", "mktree"], cwd=cwd, input=("\n".join(lines) + "\n").encode() if lines else b"",
                          capture_output=True, check=True).stdout.decode().strip()
    message = f"音声: {days} 日を過ぎた音声を消した（{'・'.join(gone)}）。前の履歴は残さない"
    new = _git("commit-tree", tree, "-m", message, cwd=cwd).stdout.decode().strip()
    _git("update-ref", f"refs/heads/{AUDIO_BRANCH}", new, cwd=cwd)
    return gone, old, new


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="店舗のテスト記録をリポジトリに置く・取り出す")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="文字のログ・真のアクション・聞き取りの印を data/store/ に置く")
    a.add_argument("logs", type=Path, nargs="*", help="pokerlogs の zip（分割は全部）")
    a.add_argument("--truth", type=Path, action="append", default=[], help="pokertruth の zip")
    a.add_argument("--labels", type=Path, action="append", default=[], help="聞き取りの印 listen_<sid8>.json")
    s = sub.add_parser("stash-audio", help=f"音声の zip を {AUDIO_BRANCH} ブランチに入れる")
    s.add_argument("logs", type=Path, nargs="+")
    g = sub.add_parser("audio", help=f"{AUDIO_BRANCH} ブランチから音声の zip を取り出す")
    g.add_argument("date", help="YYYY-MM-DD")
    g.add_argument("--out", type=Path, default=Path("store_audio"))
    sub.add_parser("list", help="一覧を出す")
    pr = sub.add_parser("prune-audio", help=f"{KEEP_DAYS} 日を過ぎた音声を {AUDIO_BRANCH} から履歴ごと消す")
    pr.add_argument("--days", type=int, default=KEEP_DAYS)
    pr.add_argument("--today", default=None, help="YYYY-MM-DD（既定 = 今日）")
    pr.add_argument("--dry-run", action="store_true", help="消す日付を出すだけ")
    args = ap.parse_args(argv)
    if args.cmd == "add":
        add(args.logs, args.truth, args.labels)
    elif args.cmd == "stash-audio":
        stash_audio(args.logs)
    elif args.cmd == "audio":
        got = fetch_audio(args.date, args.out)
        print(f"{args.out}: {len(got)} 個" if got else f"{args.date} の音声はありません")
    elif args.cmd == "prune-audio":
        from datetime import date

        _git("fetch", "origin", f"{AUDIO_BRANCH}:refs/remotes/origin/{AUDIO_BRANCH}", check=False)
        gone, old, new = prune_audio(args.today or date.today().isoformat(), args.days, dry_run=args.dry_run)
        if not gone:
            print(f"{args.days} 日を過ぎた音声はありません")
        elif new is None:
            print(f"消す日付（--dry-run）: {'・'.join(gone)}")
        else:
            print(f"消した日付: {'・'.join(gone)}（{AUDIO_BRANCH} を親の無いコミット {new[:8]} に作り直した）")
            print(f"push: git push --force-with-lease={AUDIO_BRANCH}:{old} origin {AUDIO_BRANCH}")
    else:
        print(index_table(session_rows()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
