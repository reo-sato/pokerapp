"""tools/offplay_audio.py

聞き流した（ハンドの外の）発話の音声 `logs/audio/<sid>/off_<話し始めの ms>.wav` を、**ライブと同じ経路**
（Whisper + 第 2 の耳 + 読み取り = `AudioThread._process_chunk`）で書き起こして `logs/<sid>.offplay.jsonl` に書く
（行の形は `<sid>.transcripts.jsonl` と同じ + `"offplay": true`）。

ライブがハンドを早く閉じると、次の配布まで聞き取りを止める（ADR-0063）。そのあいだの発話は認識に回さないが、
`audio.save_audio` なら音声だけは残す（2026-10-24）。これを書き起こすと、推定器（`tools/estimate.py` の
`inputs_from_logs`）がその続きの賭けを使える。記録（ライブ）は変えない。

使い方（店舗 PC で営業のあと = ロガーを閉じてから。モデルはライブと同じ config の audio.*）:

    python tools/offplay_audio.py                      # logs/ の全セッション（書き起こし済みの音声は飛ばす）
    python tools/offplay_audio.py --session 8d08c010   # 1 セッション（ID の先頭でよい）
    python tools/offplay_audio.py <zip を展開したフォルダ> --limit 5
"""
from __future__ import annotations

import argparse
import dataclasses
import queue
import sys
import threading
import wave
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.offplay import OFFPLAY_PREFIX, OFFPLAY_SUFFIX, merge_offplay, offplay_start, read_rows  # noqa: E402,F401


def _read_wav(path: Path) -> bytes:
    with wave.open(str(path), "rb") as f:
        return f.readframes(f.getnframes())


def transcribe_session(session, process: Callable[[bytes, float], list], *, limit: Optional[int] = None,
                       log: Callable[[str], None] = print) -> tuple[int, int]:
    """1 セッションの `off_*.wav` を書き起こして `<sid>.offplay.jsonl` に足す。(書いた行, 失敗) を返す。

    `process(audio_bytes, start)` は書き起こし（`audio.recorder.Transcript`）の並びを返す（何も聞こえなければ空）。
    書き起こし済み（同じ `audio_file` の行がある）の音声は飛ばす = 途中で止めても続きから。
    """
    from output.transcript_log import TranscriptLog

    out = session.transcripts.with_name(session.session_id + OFFPLAY_SUFFIX)
    done = {r.get("audio_file") for r in read_rows(out)}
    wavs = sorted({p.name: p for folder in session.audio_dirs if folder.is_dir()
                   for p in folder.glob(OFFPLAY_PREFIX + "*.wav")}.values(), key=lambda p: p.name)
    todo = [p for p in wavs if p.name not in done and offplay_start(p.name) is not None]
    if limit is not None:
        todo = todo[:limit]
    writer = TranscriptLog(out)
    written = failed = 0
    for i, path in enumerate(todo, 1):
        try:
            transcripts = process(_read_wav(path), offplay_start(path.name))
        except Exception as e:  # noqa: BLE001 — 1 つ読めなくても残りを続ける
            failed += 1
            log(f"  [{i}/{len(todo)}] {path.name}: 読めませんでした（{type(e).__name__}: {e}）")
            continue
        if not transcripts:
            # 何も聞こえなかった音も「書き起こし済み」に残す（次に回したとき聞き直さない）
            transcripts = [_silent(path, offplay_start(path.name))]
        for t in transcripts:
            writer.write(dataclasses.replace(t, audio_file=path.name), extra={"offplay": True})
            written += 1
        shown = " / ".join(t.text for t in transcripts if t.text) or "（声なし）"
        log(f"  [{i}/{len(todo)}] {path.name} {shown}")
    return written, failed


def _silent(path: Path, start: Optional[float]):
    from audio.recorder import Transcript

    sec = 0.0
    try:
        with wave.open(str(path), "rb") as f:
            sec = f.getnframes() / float(f.getframerate() or 16000)
    except (OSError, wave.Error):
        pass
    return Transcript(text="", confidence=None, events=(), audio_sec=sec, infer_sec=0.0,
                      utterance_start_ts=start, heard_at=start, noise=True, no_speech=True)


def live_processor(config: dict) -> tuple[Optional[Callable[[bytes, float], list]], Optional[str]]:
    """ライブと同じ設定の AudioThread（マイクは開かない）で書き起こす関数。読み込めなければ (None, 理由)。"""
    import main as app

    audio_cfg = dict(config.get("audio") or {}, enabled=True)
    heard: list = []
    second_ear = app._load_second_ear(audio_cfg)
    thread = app._make_audio_thread(dict(config, audio=audio_cfg), queue.Queue(), threading.Event(),
                                    on_transcript=heard.append, second_ear=second_ear)
    transcriber = getattr(thread, "_transcriber", None)
    if transcriber is None or not getattr(transcriber, "ready", False):
        return None, getattr(transcriber, "load_error", None) or "Whisper を読み込めませんでした"

    def process(audio_bytes: bytes, start: float) -> list:
        heard.clear()
        thread._process_chunk(audio_bytes, utterance_start_ts=start)   # noqa: SLF001
        return list(heard)

    return process, None


def main(argv: Optional[list[str]] = None) -> int:
    from tools.rescore_audio import _load_config, find_sessions

    ap = argparse.ArgumentParser(description="聞き流した発話の音声（off_*.wav）をライブと同じ経路で書き起こす")
    ap.add_argument("root", nargs="?", default=None, help="logs/ か pack_logs の zip を展開したフォルダ（既定 logs/）")
    ap.add_argument("--session", action="append", help="セッション ID（先頭でよい、複数回可）")
    ap.add_argument("--limit", type=int, default=None, help="セッションごとに書き起こす音声の数（試すとき）")
    args = ap.parse_args(argv)

    config = _load_config()
    root = Path(args.root) if args.root else ROOT / ((config.get("session") or {}).get("log_dir") or "logs")
    sessions = [s for s in find_sessions(root, args.session)
                if any(f.is_dir() and any(f.glob(OFFPLAY_PREFIX + "*.wav")) for f in s.audio_dirs)]
    if not sessions:
        print(f"[offplay] 聞き流した発話の音声（audio/<sid>/{OFFPLAY_PREFIX}*.wav）が {root} にありません"
              "（config の audio.save_audio が true のときだけ保存されます）")
        return 1
    print("[offplay] Whisper と第 2 の耳を読み込んでいます（ライブと同じ設定。ロガーを閉じてから動かす）")
    process, error = live_processor(config)
    if process is None:
        print(f"[offplay] 書き起こせません: {error}")
        return 1
    total = failed = 0
    for session in sessions:
        print(f"[offplay] {session.session_id}")
        n, bad = transcribe_session(session, process, limit=args.limit)
        total += n
        failed += bad
        if n == 0 and bad == 0:
            print("  書き起こす音声はありません（書き起こし済み）")
    print(f"[offplay] {total} 行を書きました（失敗 {failed}）。結果は各セッションの *{OFFPLAY_SUFFIX}")
    return 0 if failed == 0 or failed < total else 1


if __name__ == "__main__":
    sys.exit(main())
