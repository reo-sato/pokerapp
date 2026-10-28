"""tools/offplay_audio.py

聞き流した（ハンドの外の）発話の音声 `logs/audio/<sid>/off_<話し始めの ms>.wav` を、**ライブと同じ経路**
（Whisper + 第 2 の耳 + 読み取り = `AudioThread.transcribe_offline`）で書き起こして `logs/<sid>.offplay.jsonl` に書く
（行の形は `<sid>.transcripts.jsonl` と同じ + `"offplay": true`）。

ライブがハンドを早く閉じると、次の配布まで聞き取りを止める（ADR-0063）。そのあいだの発話は認識に回さないが、
`audio.save_audio` なら音声だけは残す（2026-10-24）。これを書き起こすと、推定器（`tools/estimate.py` の
`inputs_from_logs`）がその続きの賭けを使える。記録（ライブ）は変えない。

**書き起こしを後回しにしたセッション**（`audio.live_transcribe=false`, 2026-10-28）: プレー中の発話の音声
`<話し始めの ms>.wav` のうち、`<sid>.transcripts.jsonl` にまだ行の無いものも同じく書き起こし、`transcripts.jsonl` に
足す（`"deferred": true`、`heard_at` はその場で聞けていた場合の時刻）。後回しのセッションは音声のフォルダの印
`.deferred` で分かる（その場で書き起こすセッションは、何も聞こえなかった発話に行が無いので、印が無ければ見ない）。

**ふだんは自動**（2026-10-24）: ロガーを `q` で終えたとき（読み込んであるモデルをそのまま使う, `after_session`）と、
「ログをまとめる」（`tools/pack_logs.py`, `catch_up`）のときに、まだ書き起こしていない音声を書き起こす。どちらも
途中で止めてよい（次は続きから）。config `audio.transcribe_offplay`（既定 true）で止められる。

手で回すとき（店舗 PC で営業のあと = ロガーを閉じてから。モデルはライブと同じ config の audio.*）:

    python tools/offplay_audio.py                      # logs/ の全セッション（書き起こし済みの音声は飛ばす）
    python tools/offplay_audio.py --session 8d08c010   # 1 セッション（ID の先頭でよい）
    python tools/offplay_audio.py <zip を展開したフォルダ> --limit 5
"""
from __future__ import annotations

import argparse
import dataclasses
import queue
import re
import sys
import threading
import time
import wave
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.offplay import OFFPLAY_PREFIX, OFFPLAY_SUFFIX, merge_offplay, offplay_start, read_rows  # noqa: E402,F401

WORKER_WAIT_SEC = 120.0      # ロガーを終えたとき、聞き取りの作業スレッドが止まるのを待つ上限
DEFERRED_MARK = ".deferred"  # 書き起こしを後回しにしたセッションの音声のフォルダの印
_LIVE_WAV = re.compile(r"^(\d{10,})\.wav$")


def mark_deferred(audio_dir: Optional[Path], transcripts: Path) -> None:
    """書き起こしを後回しにするセッションの印（音声のフォルダの `.deferred`）を置き、書き起こしの記録を作っておく
    （何も書き起こす前に「ログをまとめる」になってもセッションが見つかるように）。"""
    try:
        if audio_dir is not None:
            Path(audio_dir).mkdir(parents=True, exist_ok=True)
            (Path(audio_dir) / DEFERRED_MARK).touch()
        Path(transcripts).parent.mkdir(parents=True, exist_ok=True)
        Path(transcripts).touch()
    except OSError:
        pass


def _live_start(name: str) -> Optional[float]:
    m = _LIVE_WAV.match(name)
    return int(m.group(1)) / 1000.0 if m else None


def _deferred(session) -> bool:
    return any((Path(folder) / DEFERRED_MARK).is_file() for folder in session.audio_dirs)


def _deferred_todo(session) -> list[Path]:
    """後回しのセッションの、まだ書き起こしの行の無いプレー中の発話の音声（話し始めの順）。"""
    if not _deferred(session):
        return []
    done = {r.get("audio_file") for r in read_rows(session.transcripts)}
    wavs = {p.name: p for folder in session.audio_dirs if Path(folder).is_dir()
            for p in Path(folder).glob("*.wav") if _live_start(p.name) is not None}
    return [wavs[n] for n in sorted(wavs) if n not in done]


def transcribe_deferred(session, process: Callable[[bytes, float, str], object], *,
                        log: Callable[[str], None] = print) -> tuple[int, int]:
    """後回しにした発話を書き起こして `<sid>.transcripts.jsonl` に足す。(書いた行, 失敗) を返す。"""
    from output.transcript_log import TranscriptLog

    writer = TranscriptLog(session.transcripts)
    todo = _deferred_todo(session)
    written = failed = 0
    for i, path in enumerate(todo, 1):
        try:
            transcript = process(_read_wav(path), _live_start(path.name), path.name)
        except Exception as e:  # noqa: BLE001 — 1 つ読めなくても残りを続ける
            failed += 1
            log(f"  [{i}/{len(todo)}] {path.name}: 読めませんでした（{type(e).__name__}: {e}）")
            continue
        writer.write(transcript)
        written += 1
        log(f"  [{i}/{len(todo)}] {path.name} {getattr(transcript, 'text', '') or '（声なし）'}")
    return written, failed


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


def processor_for(thread) -> Callable[[bytes, float], list]:
    """`AudioThread` で 1 発話を書き起こす関数（`transcribe_offline` = ライブと同じ読み方。保存・送出はしない）。"""

    def process(audio_bytes: bytes, start: float) -> list:
        transcript = thread.transcribe_offline(audio_bytes, start)
        return [transcript] if transcript is not None else []

    # 後回しにしたプレー中の発話（`transcripts.jsonl` に書く。何も聞こえなくても行を返す）
    process.deferred = lambda audio_bytes, start, name: thread.transcribe_deferred(audio_bytes, start, name)
    return process


def live_processor(config: dict) -> tuple[Optional[Callable[[bytes, float], list]], Optional[str]]:
    """ライブと同じ設定の AudioThread（マイクは開かない）で書き起こす関数。読み込めなければ (None, 理由)。"""
    import main as app

    audio_cfg = dict(config.get("audio") or {}, enabled=True)
    second_ear = app._load_second_ear(audio_cfg)
    thread = app._make_audio_thread(dict(config, audio=audio_cfg), queue.Queue(), threading.Event(),
                                    second_ear=second_ear)
    transcriber = getattr(thread, "_transcriber", None)
    if transcriber is None or not getattr(transcriber, "ready", False):
        return None, getattr(transcriber, "load_error", None) or "Whisper を読み込めませんでした"
    return processor_for(thread), None


def session_audio(log_dir: Path, session_id: str):
    """ログのフォルダ（`logs/`）の 1 セッション（`<sid>.transcripts.jsonl` の隣に書く・`audio/<sid>/` を読む）。"""
    from tools.rescore_audio import SessionAudio

    return SessionAudio(session_id, Path(log_dir) / f"{session_id}.transcripts.jsonl",
                        (Path(log_dir) / "audio" / session_id,))


def pending(session) -> int:
    """まだ書き起こしていない音声の数（`off_*.wav` + 後回しにしたプレー中の発話）。"""
    out = session.transcripts.with_name(session.session_id + OFFPLAY_SUFFIX)
    done = {r.get("audio_file") for r in read_rows(out)}
    names = {p.name for folder in session.audio_dirs if folder.is_dir() for p in folder.glob(OFFPLAY_PREFIX + "*.wav")}
    return len([n for n in names if n not in done and offplay_start(n) is not None]) + len(_deferred_todo(session))


def enabled(config: dict) -> bool:
    return bool((config.get("audio") or {}).get("transcribe_offplay", True))


def _run(sessions: list, process: Callable[[bytes, float], list], say: Callable[[str], None]) -> tuple[int, int]:
    """書き起こして、10 件ごとと最後に 1 行出す。Ctrl+C で止めてよい（次は続きから）。"""
    total = failed = 0
    count = sum(pending(s) for s in sessions)
    done = 0

    def log(_line: str) -> None:
        nonlocal done
        done += 1
        if done % 10 == 0 and done < count:
            say(f"[offplay]   {done}/{count}")

    deferred = getattr(process, "deferred", None)
    try:
        for session in sessions:
            if deferred is not None:      # 後回しにしたプレー中の発話を先に（評価・推定器の主な入力）
                n, bad = transcribe_deferred(session, deferred, log=log)
                total, failed = total + n, failed + bad
            n, bad = transcribe_session(session, process, log=log)
            total, failed = total + n, failed + bad
    except KeyboardInterrupt:
        say(f"[offplay] 途中で止めました（{done}/{count}）。残りは「ログをまとめる」か "
            "python tools/offplay_audio.py のときに続きから書き起こします")
        return total, failed
    say(f"[offplay] 書き起こしていない発話 {count} 件を書き起こしました（失敗 {failed}）")
    return total, failed


def after_session(audio_thread, log_dir: Path, session_id: str, config: dict,
                  say: Callable[[str], None] = print, clock: Callable[[], float] = time.monotonic,
                  sleep: Callable[[float], None] = time.sleep) -> int:
    """ロガーを終えたとき: そのセッションの聞き流した発話を、読み込んであるモデルで書き起こす（書いた行の数）。"""
    if audio_thread is None or not enabled(config) or not getattr(audio_thread, "asr_ready", True):
        return 0
    session = session_audio(log_dir, session_id)
    count = pending(session)
    if not count:
        return 0
    # 聞き取りの作業スレッドが残りの発話を片付けて止まるまで待つ（同じモデルを 2 つのスレッドから同時に使わない）
    deadline = clock() + WORKER_WAIT_SEC
    backlog = getattr(audio_thread, "worker_busy", None) or getattr(audio_thread, "backlog", None)
    while callable(backlog) and backlog() > 0:
        if clock() >= deadline:
            say("[offplay] 聞き取りがまだ終わらないので、聞き流した発話は「ログをまとめる」のときに書き起こします")
            return 0
        sleep(0.2)
    say(f"[offplay] 書き起こしていない発話 {count} 件を書き起こします（推定器・評価が使う。Ctrl+C で後回しにできます）")
    return _run([session], processor_for(audio_thread), say)[0]


def catch_up(log_dir: Path, session_ids: list[str], config: dict, say: Callable[[str], None] = print,
             loader: Callable[[dict], tuple] = live_processor) -> int:
    """ログをまとめる前: 選んだセッションのまだ書き起こしていない音声を書き起こす（書いた行の数）。

    書き起こすものが無ければモデルを読み込まない。読み込めなければ何もしない（まとめるのは止めない）。
    """
    if not enabled(config):
        return 0
    sessions = [s for s in (session_audio(log_dir, sid) for sid in session_ids) if pending(s)]
    if not sessions:
        return 0
    count = sum(pending(s) for s in sessions)
    say(f"[offplay] 書き起こしていない発話 {count} 件を先に書き起こします（Whisper を読み込みます。"
        "Ctrl+C で後回しにできます）")
    try:
        process, error = loader(config)
    except KeyboardInterrupt:
        say("[offplay] 後回しにしました")
        return 0
    if process is None:
        say(f"[offplay] 書き起こせないので、音声だけ入れます（{error}）")
        return 0
    return _run(sessions, process, say)[0]


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
                if _deferred(s) or any(f.is_dir() and any(f.glob(OFFPLAY_PREFIX + "*.wav")) for f in s.audio_dirs)]
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
        if _deferred(session):        # 書き起こしを後回しにしたプレー中の発話（transcripts.jsonl に足す）
            n, bad = transcribe_deferred(session, process.deferred)
            total += n
            failed += bad
        n, bad = transcribe_session(session, process, limit=args.limit)
        total += n
        failed += bad
        if n == 0 and bad == 0:
            print("  書き起こす音声はありません（書き起こし済み）")
    print(f"[offplay] {total} 行を書きました（失敗 {failed}）。結果は各セッションの *{OFFPLAY_SUFFIX}")
    return 0 if failed == 0 or failed < total else 1


if __name__ == "__main__":
    sys.exit(main())
