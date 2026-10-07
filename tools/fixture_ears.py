"""tools/fixture_ears.py

開発データ（`tests/fixtures/store/<日付>-<sid 8 桁>/transcripts.jsonl`）の全発話に、第 2 の耳の結果を足す
（短い語の聞き間違いの進め方 2, `docs/worklog/2026-10-07-short-word-plan.md`）。

ライブは 2026-10-07 から全発話を第 2 の耳で聞いて記録する（`audio/recorder.py`。記録だけの耳は `ear_wanted` = 偽）。
それより前の開発データは、ライブが使った発話にだけ耳がある。保存した発話の音声（店舗の logs）から残りの発話を聞き、
ライブと同じ形で足す:

- 耳の無い行: `ear` = 第 2 の耳の結果（`offline` = 真）、`ear_wanted` = 偽（読み直し・再生・推定器の今の読みは
  変わらない, `second_ear.used_ear`）。
- ライブの耳の行で短い語の無い古い形: `words`・`frames` だけを足す（候補・額の表はライブのまま）。

音声のファイル名は話し始めの時刻（`int(utterance_start_ts * 1000).wav`, `AudioThread._save_audio`）。店舗の音声は
リポジトリに入れない（足すのは聞いた結果の文と確からしさだけ）。もう足した行はそのまま（何度流してもよい）。

使い方:

    python tools/fixture_ears.py tests/fixtures/store/2026-10-07-782c457d --audio <logs を展開したフォルダ>
    python tools/fixture_ears.py tests/fixtures/store/* --audio <フォルダ> --audio <フォルダ> --model-dir <モデル>
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from audio import second_ear as se  # noqa: E402
from tools.rescore_audio import SessionAudio, default_threads, find_sessions, load_wav, read_jsonl  # noqa: E402


def wav_name(utterance_start_ts: float) -> str:
    """ライブが保存した発話の音声のファイル名（`AudioThread._save_audio` と同じ）。"""
    return f"{int(utterance_start_ts * 1000)}.wav"


def merge_ear(row: dict, heard: dict) -> str:
    """書き起こしの 1 行に第 2 の耳の結果を足す。"added"（耳の無い行）/ "enriched"（ライブの耳に短い語を足した）/
    "kept"（もう短い語がある）。"""
    ear = row.get("ear")
    if ear:
        if "words" in ear:
            return "kept"
        for key in ("words", "frames"):
            if key in heard:
                ear[key] = heard[key]
        return "enriched"
    row["ear"] = {**heard, "offline": True}
    row["ear_wanted"] = False
    return "added"


def needs_ear(row: dict) -> bool:
    ear = row.get("ear")
    return row.get("utterance_start_ts") is not None and not (ear and "words" in ear)


def session_audio(roots: Iterable[Path], sid8: str) -> Optional[SessionAudio]:
    """sid の先頭 8 桁のセッションのうち、音声のファイルがいちばん多い場所（同じ zip を 2 回展開したときなど）。"""
    best: tuple[int, Optional[SessionAudio]] = (0, None)
    for root in roots:
        for session in find_sessions(Path(root), [sid8]):
            n = sum(len(list(d.glob("*.wav"))) for d in session.audio_dirs if d.is_dir())
            if n > best[0]:
                best = (n, session)
    return best[1]


def add_ears(folder: Path, session: SessionAudio, ear: Any, *, dry_run: bool = False,
             log: Callable[[str], None] = print) -> dict[str, int]:
    """開発データの 1 セッションの書き起こしに第 2 の耳の結果を足す（`dry_run` なら数えるだけ）。"""
    path = folder / "transcripts.jsonl"
    rows = read_jsonl(path)
    counts = {"added": 0, "enriched": 0, "kept": 0, "no_audio": 0, "failed": 0}
    todo = [row for row in rows if needs_ear(row)]
    counts["kept"] = len(rows) - len(todo)
    started = time.perf_counter()
    for i, row in enumerate(todo, start=1):
        audio = session.audio_path(wav_name(float(row["utterance_start_ts"])))
        if audio is None:
            counts["no_audio"] += 1
            continue
        if dry_run:
            counts["added" if not row.get("ear") else "enriched"] += 1
            continue
        try:
            heard = ear.hear(load_wav(audio)).to_dict()
        except Exception as e:  # noqa: BLE001 — 1 発話の失敗で全体を止めない
            log(f"  {audio.name}: 聞けませんでした（{type(e).__name__}: {e}）")
            counts["failed"] += 1
            continue
        counts[merge_ear(row, heard)] += 1
        if i % 50 == 0:
            log(f"  {i}/{len(todo)} 発話（{time.perf_counter() - started:.0f} 秒）")
    if not dry_run and (counts["added"] or counts["enriched"]):
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    return counts


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="開発データの全発話に第 2 の耳の結果を足す")
    ap.add_argument("fixtures", nargs="+", help="tests/fixtures/store/<日付>-<sid 8 桁> のフォルダ")
    ap.add_argument("--audio", action="append", required=True,
                    help="店舗の logs（pack_logs の zip を展開したフォルダ）。複数回可")
    ap.add_argument("--model-dir", default=None, help="第 2 の耳のモデルの置き場（既定 models/reazonspeech-k2-v2）")
    ap.add_argument("--threads", type=int, default=None, help="CPU スレッド数")
    ap.add_argument("--dry-run", action="store_true", help="聞かずに、足す行の数だけ数える")
    args = ap.parse_args(argv)
    roots = [Path(r) for r in args.audio]
    ear = None
    if not args.dry_run:
        folder = Path(args.model_dir) if args.model_dir else se.model_dir(ROOT)
        if not se.model_ready(folder):
            print(f"第 2 の耳のモデルがありません: {folder}（python tools/second_ear.py --model-only）")
            return 1
        ear = se.SecondEar.load(folder, threads=args.threads or default_threads())
    for fixture in (Path(f) for f in args.fixtures):
        if not (fixture / "transcripts.jsonl").is_file():
            continue
        sid8 = fixture.name.rsplit("-", 1)[-1]
        session = session_audio(roots, sid8)
        if session is None:
            print(f"{fixture.name}: 音声が見つかりません")
            continue
        counts = add_ears(fixture, session, ear, dry_run=args.dry_run)
        print(f"{fixture.name}: 足した {counts['added']}・短い語を足した {counts['enriched']}・そのまま {counts['kept']}"
              f"・音声なし {counts['no_audio']}・失敗 {counts['failed']}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
