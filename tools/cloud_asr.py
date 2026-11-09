"""tools/cloud_asr.py

台帳 H の H1（作業ログ `docs/worklog/2026-10-28-cloud-asr-plan.md`）: 保存した発話の音声をクラウドの音声認識 API に
かけ、いまの Whisper（店舗 PC の medium）・第 2 の耳と同じ物差しで比べる。**オフラインの実験。ライブは変えない。**

1. `run`: 音声付きのログ（pack_logs の音声付き zip・展開したフォルダ・logs/）の発話を API に送り、
   `<sid>.asr.<方式>.jsonl` に書く（1 発話 1 行: 文・秒数・課金の目安。途中で止めても続きから）。
2. `compare`: 方式ごとに、書き起こしをその文に置き換えて
   - 配る人が言うアクションのうち発話の読みと合った割合（音声の改善を終える目安と同じ物差し,
     `tools/eval_store.py:listening_stop_metrics`）と、そのうちベット / レイズの額の行、
   - 読み直して再生した記録の、真のアクションとの一致（`evaluate_against_truth`）、
   - 1 発話の時間・課金の目安
   を、いまの Whisper（記録の書き起こし）と並べる。第 2 の耳を重ねる（ライブと同じ規則）/ 重ねないの両方を出す。

送るのは発話の音声だけ（名前・真のアクションは送らない）。卓の会話も入るので、送ってよいかはオーナーが決める
（作業ログの「オーナーに決めてほしいこと」）。鍵は環境変数（店舗 PC には置かない）:

    OPENAI_API_KEY（POKERAPP_OPENAI_API_KEY でも）/ GROQ_API_KEY / DEEPGRAM_API_KEY /
    Google: GOOGLE_CLOUD_PROJECT と `gcloud auth print-access-token`（CLOUDSDK_AUTH_ACCESS_TOKEN でも）

使い方:

    python tools/cloud_asr.py run pokerlogs_<日時>_1of3.zip pokerlogs_<日時>_2of3.zip --provider openai-4o --dry-run
    python tools/cloud_asr.py run <zip…> --provider openai-4o --limit 20       # 試しに 20 発話
    python tools/cloud_asr.py run <zip…> --provider groq-turbo --out results/
    python tools/cloud_asr.py compare <zip…> --results results/

zip は展開した一時フォルダに書けないので、`--out` のフォルダに `<sid>.asr.<方式>.jsonl` を書き、compare は
`--results` で読む（logs/ を直接渡したときは既定でそのフォルダ）。
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import math
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
import wave
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SUFFIX = ".asr.{name}.jsonl"
TIMEOUT_SEC = 60.0


@dataclass(frozen=True)
class Provider:
    """1 つの方式（サービス + モデル）。料金は第三者のまとめの目安（2026-10, 公式で要確認）。"""
    name: str
    kind: str                   # openai / groq / deepgram / google
    model: str
    usd_per_min: float
    bill_unit_sec: float = 1.0  # この秒数単位で切り上げ
    min_bill_sec: float = 0.0   # 1 回の最低課金の秒数
    label: str = ""

    def billed_sec(self, audio_sec: float) -> float:
        sec = max(audio_sec, self.min_bill_sec)
        return math.ceil(sec / self.bill_unit_sec - 1e-9) * self.bill_unit_sec

    def cost_usd(self, audio_sec: float) -> float:
        return self.billed_sec(audio_sec) / 60.0 * self.usd_per_min


PROVIDERS: dict[str, Provider] = {p.name: p for p in (
    Provider("openai-whisper-1", "openai", "whisper-1", 0.006, label="OpenAI whisper-1"),
    Provider("openai-4o", "openai", "gpt-4o-transcribe", 0.006, label="OpenAI gpt-4o-transcribe"),
    Provider("openai-4o-mini", "openai", "gpt-4o-mini-transcribe", 0.003, label="OpenAI gpt-4o-mini-transcribe"),
    Provider("groq-v3", "groq", "whisper-large-v3", 0.111 / 60, min_bill_sec=10.0, label="Groq whisper-large-v3"),
    Provider("groq-turbo", "groq", "whisper-large-v3-turbo", 0.04 / 60, min_bill_sec=10.0,
             label="Groq whisper-large-v3-turbo"),
    Provider("deepgram-nova3", "deepgram", "nova-3", 0.0052, label="Deepgram Nova-3（多言語）"),
    Provider("google-chirp3", "google", "chirp_3", 0.016, bill_unit_sec=15.0, label="Google Chirp 3"),
)}


# ───────────────────────── API の呼び出し ─────────────────────────


class AsrError(Exception):
    pass


def _env(*names: str) -> Optional[str]:
    return next((os.environ[n] for n in names if os.environ.get(n)), None)


def missing_key(provider: Provider) -> Optional[str]:
    """鍵が無ければ、何を設定すればよいか。あれば None。"""
    if provider.kind == "openai" and not _env("POKERAPP_OPENAI_API_KEY", "OPENAI_API_KEY"):
        return "OPENAI_API_KEY（または POKERAPP_OPENAI_API_KEY）"
    if provider.kind == "groq" and not _env("GROQ_API_KEY"):
        return "GROQ_API_KEY"
    if provider.kind == "deepgram" and not _env("DEEPGRAM_API_KEY"):
        return "DEEPGRAM_API_KEY"
    if provider.kind == "google" and not _env("GOOGLE_CLOUD_PROJECT"):
        return "GOOGLE_CLOUD_PROJECT（と gcloud のログイン）"
    return None


def _post(url: str, body: bytes, headers: dict[str, str]) -> dict:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SEC) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise AsrError(f"HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise AsrError(f"接続できません: {e.reason}") from e


def _multipart(fields: dict[str, str], wav: bytes) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts = []
    for key, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="a.wav"\r\n'
                 "Content-Type: audio/wav\r\n\r\n".encode() + wav + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _google_token() -> str:
    token = _env("GOOGLE_ACCESS_TOKEN", "CLOUDSDK_AUTH_ACCESS_TOKEN")
    if token:
        return token
    try:
        return subprocess.run(["gcloud", "auth", "print-access-token"], check=True, capture_output=True,
                              text=True, timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError) as e:
        raise AsrError(f"Google のトークンを取れません（gcloud auth login）: {e}") from e


def transcribe(provider: Provider, wav: bytes, prompt: Optional[str]) -> str:
    """1 発話の WAV を書き起こした文。"""
    if provider.kind in ("openai", "groq"):
        key = _env("POKERAPP_OPENAI_API_KEY", "OPENAI_API_KEY") if provider.kind == "openai" else _env("GROQ_API_KEY")
        base = "https://api.openai.com/v1" if provider.kind == "openai" else "https://api.groq.com/openai/v1"
        fields = {"model": provider.model, "language": "ja", "response_format": "json", "temperature": "0"}
        if prompt:
            fields["prompt"] = prompt
        body, ctype = _multipart(fields, wav)
        data = _post(f"{base}/audio/transcriptions", body, {"Authorization": f"Bearer {key}", "Content-Type": ctype})
        return str(data.get("text") or "")
    if provider.kind == "deepgram":
        url = f"https://api.deepgram.com/v1/listen?model={provider.model}&language=ja&smart_format=true"
        data = _post(url, wav, {"Authorization": f"Token {_env('DEEPGRAM_API_KEY')}", "Content-Type": "audio/wav"})
        alts = (((data.get("results") or {}).get("channels") or [{}])[0].get("alternatives") or [{}])
        return str(alts[0].get("transcript") or "")
    if provider.kind == "google":
        project = _env("GOOGLE_CLOUD_PROJECT")
        region = _env("GOOGLE_SPEECH_REGION") or "us"
        url = (f"https://{region}-speech.googleapis.com/v2/projects/{project}/locations/{region}"
               "/recognizers/_:recognize")
        payload = {"config": {"autoDecodingConfig": {}, "model": provider.model, "languageCodes": ["ja-JP"]},
                   "content": base64.b64encode(wav).decode("ascii")}
        data = _post(url, json.dumps(payload).encode(), {"Authorization": f"Bearer {_google_token()}",
                                                         "Content-Type": "application/json"})
        return "".join(((r.get("alternatives") or [{}])[0].get("transcript") or "") for r in data.get("results") or [])
    raise AsrError(f"知らない方式: {provider.kind}")


# ───────────────────────── run ─────────────────────────


@dataclass
class Job:
    session_id: str
    row: dict
    path: Path


def _wav_sec(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as f:
            return f.getnframes() / float(f.getframerate() or 16000)
    except (OSError, wave.Error):
        return 0.0


def result_path(out_dir: Path, session_id: str, name: str) -> Path:
    return out_dir / f"{session_id}{SUFFIX.format(name=name)}"


def read_rows(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def jobs_for(root: Path, out_dir: Path, provider: Provider, *, sessions: Optional[list[str]] = None,
             include_no_speech: bool = False) -> list[Job]:
    """音声のある発話（`<sid>.transcripts.jsonl` の `audio_file`）のうち、まだその方式で書き起こしていないもの。
    声ではない音（VAD）はライブも Whisper にかけていないので、既定では送らない。"""
    from tools.rescore_audio import find_sessions

    jobs = []
    for s in find_sessions(root, sessions):
        done = {r.get("audio_file") for r in read_rows(result_path(out_dir, s.session_id, provider.name))
                if not r.get("error")}
        for row in read_rows(s.transcripts):
            name = row.get("audio_file")
            if not name or name in done or (row.get("no_speech") and not include_no_speech):
                continue
            path = s.audio_path(name)
            if path is not None:
                jobs.append(Job(s.session_id, row, path))
    return jobs


def run_jobs(jobs: list[Job], provider: Provider, out_dir: Path, *, workers: int = 4, prompt: Optional[str] = None,
             call: Callable[[Provider, bytes, Optional[str]], str] = transcribe,
             say: Callable[[str], None] = print) -> tuple[int, int]:
    """発話を送って `<sid>.asr.<方式>.jsonl` に書く。(書いた行, 失敗)。失敗の行も書く（compare では使わない・次は送り直す）。"""
    out_dir.mkdir(parents=True, exist_ok=True)

    def one(job: Job) -> dict:
        started = time.time()
        audio_sec = float(job.row.get("audio_sec") or 0.0) or _wav_sec(job.path)
        out = {"audio_file": job.row["audio_file"], "utterance_start_ts": job.row.get("utterance_start_ts"),
               "model": provider.model, "audio_sec": round(audio_sec, 3),
               "billed_sec": provider.billed_sec(audio_sec), "cost_usd": round(provider.cost_usd(audio_sec), 6)}
        try:
            out["text"] = call(provider, job.path.read_bytes(), prompt)
        except Exception as e:  # noqa: BLE001 — 1 つ失敗しても続ける
            out["error"] = f"{type(e).__name__}: {e}"
        out["seconds"] = round(time.time() - started, 3)
        return out

    written = failed = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for i, (job, row) in enumerate(zip(jobs, pool.map(one, jobs)), 1):
            with result_path(out_dir, job.session_id, provider.name).open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            written += 1
            failed += bool(row.get("error"))
            if row.get("error") or i % 20 == 0 or i == len(jobs):
                say(f"  [{i}/{len(jobs)}] {job.row['audio_file']} "
                    f"{row.get('error') or repr(row.get('text'))}（{row['seconds']} 秒）")
    return written, failed


# ───────────────────────── compare ─────────────────────────


def _substituted(transcripts: list[dict], texts: Optional[dict[str, str]], keep_ear: bool) -> list[dict]:
    """書き起こしの文を方式の文に置き換えた行（音声の無い発話・その方式で書き起こせなかった発話はそのまま）。"""
    out = []
    for r in transcripts:
        row = dict(r)
        name = r.get("audio_file")
        if texts is not None and name in texts:
            row["text"] = texts[name]
            row["noise"] = False                # 雑音の判定はライブの Whisper の文に付いたもの
        if not keep_ear:
            row.pop("ear", None)
        out.append(row)
    return out


def _amount_tokens(tokens: list[tuple]) -> list[tuple]:
    """ベット / レイズの行（`eval_store._speech_token` は額だけの `("wager", 額)`）。"""
    return [t for t in tokens if t and t[0] == "wager"]


def read_rates(gt: dict, windows: dict, transcripts: list[dict]) -> dict:
    """配る人が言うアクションのうち発話の読みと合った割合と、ベット / レイズ（額つき）の行だけの割合。"""
    from tools.eval_store import heard_tokens, is_excluded, matched_tokens, truth_tokens

    actions = read = amounts = amounts_read = 0
    for h in gt.get("hands") or []:
        if is_excluded(h) or h.get("hand_id") not in windows:
            continue
        truth = truth_tokens(h)
        heard = heard_tokens(transcripts, windows[h["hand_id"]])
        actions += len(truth)
        read += matched_tokens(truth, heard)
        ta, ha = _amount_tokens(truth), _amount_tokens(heard)
        amounts += len(ta)
        amounts_read += matched_tokens(ta, ha)
    return {"actions": actions, "read": read, "amounts": amounts, "amounts_read": amounts_read}


def compare_session(files, config: dict, results: Path, names: list[str]) -> list[dict]:
    """1 セッションの方式ごとの行（いまの Whisper + 各方式 × 第 2 の耳あり / なし）。

    どの行も、書き起こしの文を置き換えた行を**いまの読み取り**で読み直して再生する（`reparse_events`）。第 2 の耳ありは
    ライブと同じ規則（`second_ear.apply_ear`）をその文に重ねる。
    """
    from integration.replay import load_events
    from tools.eval_store import (
        _read_jsonl,
        evaluate_against_truth,
        evaluate_session,
        hand_windows,
        reparse_events,
        replay_session,
    )

    report = evaluate_session(files, config, None)
    transcripts = _read_jsonl(files.path(".transcripts.jsonl"))
    if not report.gt.get("hands") or not transcripts or report.setup is None:
        return []
    events = load_events(files.events)
    windows = hand_windows(report.reparsed or report.replayed)
    with_audio = {r["audio_file"] for r in transcripts if r.get("audio_file") and not r.get("no_speech")}
    sources: list[tuple[str, Optional[dict[str, str]], dict]] = [("live", None, {})]
    for name in names:
        rows = [r for r in read_rows(result_path(results, files.session_id, name)) if not r.get("error")]
        if rows:
            sources.append((name, {r["audio_file"]: r.get("text") or "" for r in rows}, {
                "utterances": len(rows), "covered": len({r["audio_file"] for r in rows} & with_audio),
                "with_audio": len(with_audio),
                "seconds": sorted(r.get("seconds") or 0.0 for r in rows),
                "cost_usd": sum(r.get("cost_usd") or 0.0 for r in rows),
                "audio_sec": sum(r.get("audio_sec") or 0.0 for r in rows)}))
    out = []
    for name, texts, info in sources:
        for keep_ear in (False, True):
            subst = _substituted(transcripts, texts, keep_ear)
            replayed = replay_session(reparse_events(events, subst), report.setup, report.flags, files.session_id)
            truth = evaluate_against_truth(report.gt, replayed)
            out.append({"session_id": files.session_id, "source": name, "ear": keep_ear,
                        **read_rates(report.gt, windows, subst),
                        "hands": truth.get("hands"), "hands_correct": truth.get("exact_hands"),
                        "action_accuracy": truth.get("action_accuracy"), **info})
    return out


def summarize(rows: list[dict]) -> list[dict]:
    """方式 × 第 2 の耳ごとに全セッションを足す。"""
    keys: dict[tuple, dict] = {}
    for r in rows:
        k = (r["source"], r["ear"])
        s = keys.setdefault(k, {"source": r["source"], "ear": r["ear"], "actions": 0, "read": 0, "amounts": 0,
                                "amounts_read": 0, "hands": 0, "hands_correct": 0, "seconds": [], "cost_usd": 0.0,
                                "audio_sec": 0.0, "utterances": 0, "covered": 0, "with_audio": 0})
        for f in ("actions", "read", "amounts", "amounts_read", "utterances", "covered", "with_audio"):
            s[f] += int(r.get(f) or 0)
        s["hands"] += int(r.get("hands") or 0)
        s["hands_correct"] += int(r.get("hands_correct") or 0)
        s["seconds"] += r.get("seconds") or []
        s["cost_usd"] += float(r.get("cost_usd") or 0.0)
        s["audio_sec"] += float(r.get("audio_sec") or 0.0)
    return list(keys.values())


def _pct(a: int, b: int) -> str:
    return f"{a}/{b}（{a / b:.0%}）" if b else "—"


def format_summary(summary: list[dict]) -> list[str]:
    lines = ["方式 | 第 2 の耳 | アクションの読み | 額の行 | 全部正しいハンド（再生） | 1 発話の時間（中央値・最大） | "
             "課金の目安"]
    for s in summary:
        label = "いまの Whisper（店舗 PC）" if s["source"] == "live" else (
            PROVIDERS[s["source"]].label if s["source"] in PROVIDERS else s["source"])
        secs = s["seconds"]
        timing = f"{secs[len(secs) // 2]:.2f}・{secs[-1]:.2f} 秒" if secs else "—"
        cost = (f"${s['cost_usd']:.3f}（音声 {s['audio_sec'] / 60:.1f} 分・{s['covered']}/{s['with_audio']} 発話）"
                if s["source"] != "live" else "—")
        lines.append(f"{label} | {'あり' if s['ear'] else 'なし'} | {_pct(s['read'], s['actions'])} | "
                     f"{_pct(s['amounts_read'], s['amounts'])} | {_pct(s['hands_correct'], s['hands'])} | "
                     f"{timing} | {cost}")
    return lines


# ───────────────────────── main ─────────────────────────


def _open(paths: list[Path]):
    from tools.eval_store import open_input

    return open_input(paths)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="保存した発話をクラウドの音声認識 API にかけて比べる（台帳 H の H1, オフライン）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="発話を API に送って <sid>.asr.<方式>.jsonl に書く")
    run.add_argument("path", type=Path, nargs="+", help="音声付きの zip（分けた zip は全部）/ 展開したフォルダ / logs")
    run.add_argument("--provider", required=True, choices=sorted(PROVIDERS))
    run.add_argument("--out", type=Path, help="結果のフォルダ（既定: logs/ ならそのフォルダ、zip なら ./cloud_asr_results）")
    run.add_argument("--session", action="append")
    run.add_argument("--limit", type=int)
    run.add_argument("--workers", type=int, default=4)
    run.add_argument("--no-prompt", action="store_true", help="ライブと同じプロンプトを付けない（OpenAI / Groq）")
    run.add_argument("--prompt", help="ライブと違うプロンプトを付ける（OpenAI / Groq。結果は --out を分けて書く）")
    run.add_argument("--include-no-speech", action="store_true", help="声ではない音（VAD）も送る")
    run.add_argument("--dry-run", action="store_true", help="送らずに件数と課金の目安だけ")
    cmp = sub.add_parser("compare", help="方式ごとに、いまの Whisper と同じ物差しで比べる")
    cmp.add_argument("path", type=Path, nargs="+")
    cmp.add_argument("--results", type=Path, help="run の --out（既定: run と同じ）")
    cmp.add_argument("--session", action="append")
    cmp.add_argument("--providers", nargs="*", help="比べる方式（既定: 結果のあるもの全部）")
    cmp.add_argument("--json", type=Path, help="セッションごとの行を JSON に書く")
    args = ap.parse_args(argv)
    logging.disable(logging.WARNING)

    root, cleanup = _open(args.path)
    try:
        default_out = root if cleanup is None else Path("cloud_asr_results")
        if args.cmd == "run":
            provider = PROVIDERS[args.provider]
            out_dir = args.out or default_out
            jobs = jobs_for(root, out_dir, provider, sessions=args.session, include_no_speech=args.include_no_speech)
            if args.limit:
                jobs = jobs[:args.limit]
            audio = sum(float(j.row.get("audio_sec") or 0.0) or _wav_sec(j.path) for j in jobs)
            cost = sum(provider.cost_usd(float(j.row.get("audio_sec") or 0.0) or _wav_sec(j.path)) for j in jobs)
            print(f"[asr] {provider.label}: {len(jobs)} 発話・音声 {audio / 60:.1f} 分・課金の目安 ${cost:.3f}"
                  f"（{provider.usd_per_min:g} $/分・{provider.bill_unit_sec:g} 秒単位・最低 {provider.min_bill_sec:g} 秒）")
            if args.dry_run or not jobs:
                if not jobs:
                    print("[asr] 送る発話がありません（音声の無いログ・書き起こし済み）")
                return 0
            need = missing_key(provider)
            if need:
                print(f"[asr] 鍵がありません: {need} を環境変数に入れてください", file=sys.stderr)
                return 2
            from core.constants import WHISPER_PROMPT_JA

            n, bad = run_jobs(jobs, provider, out_dir, workers=args.workers,
                              prompt=None if args.no_prompt else (args.prompt or WHISPER_PROMPT_JA))
            print(f"[asr] {n} 発話を書きました（失敗 {bad}）→ {out_dir}")
            return 0 if bad < n or n == 0 else 1
        from tools.eval_store import find_config, find_sessions

        results = args.results or default_out
        names = args.providers or sorted({p.name.split(".asr.", 1)[1][:-len(".jsonl")]
                                          for p in results.glob("*.asr.*.jsonl")})
        config = find_config(root)
        rows = []
        for files in find_sessions(root, args.session):
            rows += compare_session(files, config, results, names)
        if not rows:
            print("[asr] 比べられるセッションがありません（真のアクション・書き起こし・卓の設定が要る）", file=sys.stderr)
            return 1
        for line in format_summary(summarize(rows)):
            print(line)
        if args.json:
            args.json.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
        return 0
    finally:
        if cleanup is not None:
            shutil.rmtree(cleanup, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
