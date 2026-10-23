"""tools/second_ear.py

保存した発話の音声を **第 2 の耳**（日本語専用の音声認識 ReazonSpeech, `audio/second_ear.py`）で聞き直す
（聞き間違いの根本対策の測定, 2026-09-29 の検討 = `docs/worklog/2026-09-29-asr-root-fix-exploration.md`）。

発話ごとに、第 2 の耳が自由に聞いた文と、決まった候補（アクションの語・額・その組み合わせ）ごとの確からしさを
`logs/<sid>.ear.jsonl` に書く。`--whisper` を付けると、Whisper の別のやり方（プロンプトなし / 短い窓 /
第 2 の耳の上位の候補を Whisper で採点）も `logs/<sid>.whisper.jsonl` に書く。途中で止めても、次はその続きから。
評価は `tools/eval_store.py`（方式ごとに書き起こしを置き換えて再生し、真のアクションとの一致を比べる）。

使い方（店舗 PC。営業中は動かさない = CPU を使う）:

    python tools/second_ear.py                       # 真のアクションのあるセッション
    python tools/second_ear.py --session d0f055fb    # セッションを指定（ID の先頭でよい。複数回可）
    python tools/second_ear.py --all                 # すべてのセッション
    python tools/second_ear.py --whisper             # Whisper の別のやり方も（1 発話 数秒）

初回は第 2 の耳のモデル（約 170 MB）を、GitHub の配布物（約 713 MB）から取り出して `models/reazonspeech-k2-v2/`
に置く（一度だけ。インストーラ・更新も `--model-only` で取得する）。ライブの聞き取りも同じモデルで、Whisper が
読めなかった発話を聞き直す（`audio/recorder.py`, config `audio.second_ear`）。
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from audio import second_ear as se  # noqa: E402
from audio.recognizer import is_implausibly_long, is_prompt_echo, parse_actions  # noqa: E402
from core.constants import WHISPER_PROMPT_JA  # noqa: E402
from tools.rescore_audio import (  # noqa: E402
    SessionAudio,
    default_threads,
    find_sessions,
    load_wav,
    read_jsonl,
)

EAR_SUFFIX = ".ear.jsonl"
WHISPER_SUFFIX = ".whisper.jsonl"
# 短い窓（10 ms のフレーム）: 録音の上限 5 秒 + 前後の余白。発話ごとに変えない（窓の長さで結果が変わらないように）
SHORT_WINDOW = 600
# Whisper で採点する第 2 の耳の候補の数
WHISPER_SCORED = 5
# 第 2 の耳の候補を「アクションらしい」とみる確からしさの差（自由に聞いた文と比べて。表示用）
SHOW_LLR = -3.0


def ear_path(session: SessionAudio) -> Path:
    return session.transcripts.with_name(session.session_id + EAR_SUFFIX)


def whisper_path(session: SessionAudio) -> Path:
    return session.transcripts.with_name(session.session_id + WHISPER_SUFFIX)


def has_truth(session: SessionAudio) -> bool:
    return session.transcripts.with_name(session.session_id + ".ground_truth.json").exists()


def pick_sessions(root: Path, only: Optional[list[str]], all_sessions: bool) -> list[SessionAudio]:
    """指定が無ければ、真のアクションのあるセッション（無ければいちばん新しい 1 つ）。"""
    sessions = find_sessions(root, only)
    if only or all_sessions:
        return sessions
    with_truth = [s for s in sessions if has_truth(s)]
    if with_truth or not sessions:
        return with_truth
    return [max(sessions, key=lambda s: s.transcripts.stat().st_mtime)]     # ID は時刻順ではない


def utterances(session: SessionAudio, done: set) -> list[dict]:
    """音声のある、声として聞き取りに回した発話（まだ聞いていないもの）。"""
    rows = [r for r in read_jsonl(session.transcripts)
            if r.get("audio_file") and not r.get("no_speech") and r["audio_file"] not in done]
    return [r for r in rows if session.audio_path(r["audio_file"]) is not None]


def whisper_noise(row: dict) -> bool:
    """ライブの Whisper の書き起こしが雑音（声でない・幻聴）か、アクションとして読めなかったか。"""
    text = (row.get("text") or "").strip()
    if not text or is_prompt_echo(text) or is_implausibly_long(text, float(row.get("audio_sec") or 0.0)):
        return True
    return not parse_actions(text)


def run_ear(session: SessionAudio, ear: Any, *, limit: Optional[int] = None,
            log: Callable[[str], None] = print) -> list[float]:
    """1 セッションの発話を第 2 の耳で聞き、`<sid>.ear.jsonl` に追記する。1 発話の秒数の列を返す。"""
    out_path = ear_path(session)
    rows = utterances(session, {r.get("audio_file") for r in read_jsonl(out_path)})
    if limit is not None:
        rows = rows[:max(0, limit)]
    times: list[float] = []
    for i, row in enumerate(rows, start=1):
        started = time.perf_counter()
        out: dict[str, Any] = {
            "utterance_start_ts": row.get("utterance_start_ts"), "heard_at": row.get("heard_at"),
            "audio_file": row["audio_file"], "whisper_text": row.get("text") or "",
            "at": datetime.now().isoformat(timespec="seconds"), "model": se.MODEL_NAME,
        }
        try:
            out["ear"] = ear.hear(load_wav(session.audio_path(row["audio_file"]))).to_dict()
        except Exception as e:  # noqa: BLE001 — 1 発話の失敗で全体を止めない
            out["error"] = f"{type(e).__name__}: {e}"
        out["sec"] = round(time.perf_counter() - started, 3)
        times.append(out["sec"])
        with out_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(out, ensure_ascii=False) + "\n")
        if i % 25 == 0 or i == len(rows):
            log(f"  {i}/{len(rows)} 発話")
    return times


def _mixed(value: int) -> str:
    """Whisper の書き方の「2千5百」「1万2千」（100 の倍数）。"""
    man, rest = divmod(value, 10000)
    sen, rest = divmod(rest, 1000)
    hyaku = rest // 100
    return "".join(f"{n}{unit}" for n, unit in ((man, "万"), (sen, "千"), (hyaku, "百")) if n)


def whisper_renderings(text: str) -> list[str]:
    """候補の文を Whisper の書き方でも（漢数字 → 算用数字 / 「2千5百」）。確からしさは書き方で変わるので、
    採点ではいちばん確からしい書き方を使う。"""
    from audio.recognizer import parse_amount_ex

    out = [text]
    kanji = "".join(ch for ch in text if ch in "〇一二三四五六七八九十百千万")
    value = parse_amount_ex(kanji).value if kanji else 0
    if value > 0:
        out.append(text.replace(kanji, str(value)))
        if value >= 1000 and value % 100 == 0:
            out.append(text.replace(kanji, _mixed(value)))
    return list(dict.fromkeys(out))


def run_whisper(session: SessionAudio, plain: Any, prompted: Any, *, limit: Optional[int] = None,
                log: Callable[[str], None] = print) -> dict[str, list[float]]:
    """Whisper の別のやり方で聞き、`<sid>.whisper.jsonl` に追記する。やり方ごとの 1 発話の秒数を返す。

    plain = プロンプトなしの採点器、prompted = ライブと同じプロンプトの採点器（同じモデル）。短い窓のエンコードは
    3 つのやり方で使い回す（秒数はそれぞれにエンコードを含めて数える）。
    """
    out_path = whisper_path(session)
    ear_rows = {r.get("audio_file"): r for r in read_jsonl(ear_path(session))}
    rows = utterances(session, {r.get("audio_file") for r in read_jsonl(out_path)})
    if limit is not None:
        rows = rows[:max(0, limit)]
    times: dict[str, list[float]] = {"noprompt": [], "short": [], "short_noprompt": [], "scores": []}
    for i, row in enumerate(rows, start=1):
        audio = load_wav(session.audio_path(row["audio_file"]))
        seconds = len(audio) / se.SAMPLE_RATE
        out: dict[str, Any] = {"utterance_start_ts": row.get("utterance_start_ts"), "audio_file": row["audio_file"],
                               "window_frames": SHORT_WINDOW, "at": datetime.now().isoformat(timespec="seconds")}
        try:
            t0 = time.perf_counter()
            text = plain.transcribe(plain.encode(audio), seconds)
            out["noprompt"] = {"text": text, "sec": round(time.perf_counter() - t0, 3)}
            t0 = time.perf_counter()
            short = plain.encode(audio, SHORT_WINDOW)
            encode_sec = time.perf_counter() - t0
            for key, rescorer in (("short", prompted), ("short_noprompt", plain)):
                t0 = time.perf_counter()
                text = rescorer.transcribe(short, seconds)
                out[key] = {"text": text, "sec": round(encode_sec + time.perf_counter() - t0, 3)}
            t0 = time.perf_counter()
            ear = (ear_rows.get(row["audio_file"]) or {}).get("ear") or {}
            texts = {c["text"]: whisper_renderings(c["text"]) for c in (ear.get("candidates") or [])[:WHISPER_SCORED]}
            flat = [r for rs in texts.values() for r in rs]
            scores = dict(zip(flat, plain.score(short, [plain.tok.encode(r) for r in flat]))) if flat else {}
            out["scores"] = [{"text": t, "logp": _best(scores, rs)} for t, rs in texts.items()]
            out["scores_sec"] = round(time.perf_counter() - t0, 3)
            for key in ("noprompt", "short", "short_noprompt"):
                times[key].append(out[key]["sec"])
            times["scores"].append(out["scores_sec"])
        except Exception as e:  # noqa: BLE001 — 1 発話の失敗で全体を止めない
            out["error"] = f"{type(e).__name__}: {e}"
        with out_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(out, ensure_ascii=False) + "\n")
        if i % 10 == 0 or i == len(rows):
            log(f"  {i}/{len(rows)} 発話")
    return times


def _best(scores: dict, renderings: list[str]) -> Optional[float]:
    values = [scores.get(r) for r in renderings if scores.get(r) is not None]
    return round(max(values), 3) if values else None


def rescued(session: SessionAudio, llr: float = SHOW_LLR) -> list[tuple[str, str, str, float]]:
    """ライブの Whisper が読めなかった（雑音・幻聴・読めない文）のに、第 2 の耳の候補が自由に聞いた文と同じくらい
    確からしい発話: (音声, Whisper, 第 2 の耳の候補, 確からしさの差)。何も聞こえなかった発話（自由に聞いた文が空）は
    出さない（空の文と比べた差は当てにならない）。"""
    transcripts = {r.get("audio_file"): r for r in read_jsonl(session.transcripts)}
    out = []
    for row in read_jsonl(ear_path(session)):
        ear = row.get("ear") or {}
        cands = ear.get("candidates") or []
        live = transcripts.get(row.get("audio_file")) or {}
        if not cands or ear.get("logp") is None or cands[0].get("logp") is None or not whisper_noise(live):
            continue
        if not (ear.get("text") or "").strip():
            continue
        diff = cands[0]["logp"] - ear["logp"]
        if diff >= llr:
            out.append((row["audio_file"], live.get("text") or "", cands[0]["text"], round(diff, 2)))
    return out


def _median_ms(values: list[float]) -> str:
    return f"{statistics.median(values) * 1000:.0f} ms" if values else "—"


def ensure_model(folder: Path, log: Callable[[str], None] = print) -> bool:
    if se.model_ready(folder):
        return True
    log(f"[second_ear] 第 2 の耳のモデルを取得します（配布物 約 713 MB を読みながら、要る約 170 MB を {folder} に）")
    last = [0]

    def report(count: int) -> None:
        mb = count // (50 * 1024 * 1024)
        if mb > last[0]:
            last[0] = mb
            log(f"  {mb * 50} MB")

    try:
        se.download_model(folder, report=report)
    except Exception as e:  # noqa: BLE001
        log(f"[second_ear] モデルを取得できませんでした: {type(e).__name__}: {e}")
        return False
    log("[second_ear] モデルを取得しました")
    return True


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="保存した発話の音声を第 2 の耳（ReazonSpeech）で聞き直す")
    ap.add_argument("root", nargs="?", default=None, help="logs/ か pack_logs の zip を展開したフォルダ（既定 logs/）")
    ap.add_argument("--session", action="append", help="セッション ID（先頭でよい、複数回可）")
    ap.add_argument("--all", action="store_true", help="すべてのセッション")
    ap.add_argument("--limit", type=int, default=None, help="セッションごとの発話の数（試すとき）")
    ap.add_argument("--threads", type=int, default=None, help="CPU スレッド数（既定: 物理コア数の見積もり）")
    ap.add_argument("--whisper", action="store_true",
                    help="Whisper の別のやり方も（プロンプトなし・短い窓・第 2 の耳の候補の採点。1 発話 数秒）")
    ap.add_argument("--model-dir", default=None, help="第 2 の耳のモデルの置き場（既定 models/reazonspeech-k2-v2）")
    ap.add_argument("--model-only", action="store_true",
                    help="モデルを取得するだけ（インストーラ・更新が使う。取得済みなら何もしない）")
    args = ap.parse_args(argv)
    if args.model_only:
        return 0 if ensure_model(Path(args.model_dir) if args.model_dir else se.model_dir(ROOT)) else 1

    try:
        from core.config import load_config

        config = load_config()
    except Exception:  # noqa: BLE001
        config = {}
    root = Path(args.root) if args.root else ROOT / ((config.get("session") or {}).get("log_dir") or "logs")
    sessions = pick_sessions(root, args.session, args.all)
    if not sessions:
        print(f"[second_ear] 聞き取りの記録（*.transcripts.jsonl）が {root} にありません")
        return 1
    threads = args.threads or default_threads()
    folder = Path(args.model_dir) if args.model_dir else se.model_dir(ROOT)
    if not ensure_model(folder):
        return 1
    try:
        ear = se.SecondEar.load(folder, threads=threads)
    except Exception as e:  # noqa: BLE001
        print(f"[second_ear] 第 2 の耳を読み込めませんでした: {type(e).__name__}: {e}"
              "（更新で入る部品が足りないときは、更新のコマンドをもう一度）")
        return 1
    print(f"[second_ear] 第 2 の耳（{se.MODEL_NAME}, CPU {threads} スレッド）: "
          f"{', '.join(s.session_id[:8] for s in sessions)}")
    ear_times: list[float] = []
    for session in sessions:
        print(f"[second_ear] {session.session_id}")
        ear_times += run_ear(session, ear, limit=args.limit)
    whisper_times: dict[str, list[float]] = {}
    if args.whisper:
        from tools.rescore_audio import WhisperRescorer

        audio_cfg = config.get("audio") or {}
        name = audio_cfg.get("whisper_model") or "medium"
        print(f"[second_ear] Whisper {name} を読み込んでいます（プロンプトなし・短い窓 {SHORT_WINDOW / 100:.0f} 秒・"
              "候補の採点）")
        plain = WhisperRescorer.load(name, language=audio_cfg.get("language") or "ja", prompt=None,
                                     beam_size=int(audio_cfg.get("beam_size") or 5), cpu_threads=threads)
        prompted = WhisperRescorer(plain.model, plain.tok, prompt=WHISPER_PROMPT_JA, beam_size=plain.beam_size)
        for session in sessions:
            print(f"[second_ear] Whisper {session.session_id}")
            for key, values in run_whisper(session, plain, prompted, limit=args.limit).items():
                whisper_times.setdefault(key, []).extend(values)
    print()
    print(f"[second_ear] 1 発話の時間（中央値）: 第 2 の耳 {_median_ms(ear_times)}"
          + "".join(f" / Whisper {label} {_median_ms(whisper_times.get(key, []))}"
                    for key, label in (("noprompt", "プロンプトなし"), ("short", "短い窓"),
                                       ("short_noprompt", "短い窓・プロンプトなし"), ("scores", "候補の採点"))
                    if whisper_times.get(key)))
    for session in sessions:
        found = rescued(session)
        if found:
            print(f"[second_ear] {session.session_id[:8]}: Whisper が読めなかったのに、第 2 の耳が候補を聞いた発話 "
                  f"{len(found)} 個")
            for audio_file, whisper, candidate, diff in found[:15]:
                print(f"    {audio_file}  Whisper「{whisper[:20]}」→ 第 2 の耳「{candidate}」（差 {diff}）")
    print(f"[second_ear] 結果: 各セッションの *{EAR_SUFFIX}" + (f" / *{WHISPER_SUFFIX}" if args.whisper else "")
          + "（pack_logs でまとめて送ってください）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
