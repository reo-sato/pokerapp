"""tools/dealer_voice.py — ディーラーの声の照合をオフラインで確かめる（台帳 I の I1, 2026-10-28。ライブは変えない）

店舗の zip（`pack_logs_audio.cmd` の音声付き）に入っている **読み上げ集**（`corpus/<日時>_<名前>/`, 名前を入れて読んだもの）
から、読んだ人ごとに声を登録し、同じ zip のセッションの発話ごとに「登録した声とどれだけ近いか」を出す。

- 発話は 3 つに分けて近さを比べる: アクションに読めた発話 / 決まり文句（Whisper の幻聴）/ それ以外の読めなかった発話。
  アクションに読めたのに近さが低い発話 = お客さん・隣の卓の声をアクションと取り違えた疑い（聞いて確かめる一覧に出す）。
- 登録が 2 人以上なら、10 分ごとにいちばん近い登録を数える（ディーラーの交代を声だけで追えるか）。
- 読み上げ集が無ければ、セッションごとにアクションに読めた発話で仮に登録する（`auto:<sid>`。その発話自身も平均に入るので
  近さは少し高めに出る）。
- オーナーが聞いた決まり文句の 51 発話（`docs/dogfood/canned51-labels.json`）と同じ音声があれば、配る人のアクション（action）
  と雑談・物音（none）で近さを比べる（同じディーラーの雑談は分けられない見込み = 0.5 に近いはず）。

出力: `<out>/<sid>.voice.jsonl`（発話ごとの近さ）と、端末への要約。

使い方（開発側）:

    python tools/dealer_voice.py fetch                                   # モデル（約 28 MB）を models/speaker/ に
    python tools/dealer_voice.py score pokerlogs_20261026_1of2.zip pokerlogs_20261026_2of2.zip
    python tools/dealer_voice.py score <zip…> --model eres2net --low 0.45 --list 40 --out logs/voice
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from audio import dealer_voice as dv  # noqa: E402
from audio.recognizer import is_implausibly_long, is_prompt_echo, parse_actions  # noqa: E402
from tools.eval_store import open_input  # noqa: E402
from tools.rescore_audio import find_sessions, load_wav, read_jsonl  # noqa: E402

JST = timezone(timedelta(hours=9))
CANNED_LABELS = ROOT / "docs" / "dogfood" / "canned51-labels.json"
# 近さがこれ未満のアクションの発話を「ディーラー以外の声の疑い」として数える（I1 の結果で決め直す）
DEFAULT_LOW = 0.40
# 仮の登録に使う発話の長さの下限（短い発話は埋め込みがぶれる）
AUTO_MIN_SEC = 0.8
BUCKET_SEC = 600

ACTION, CANNED, OTHER = "action", "canned", "other"


def category(row: dict) -> str:
    text = (row.get("text") or "").strip()
    if text and is_prompt_echo(text):
        return CANNED
    if text and not is_implausibly_long(text, float(row.get("audio_sec") or 0.0)) and parse_actions(text):
        return ACTION
    return OTHER


def find_corpora(root: Path) -> dict[str, list[Path]]:
    """読み上げ集の句ごとの音声を、読んだ人の名前ごとに（1 本目のマイク `_m1` だけ）。"""
    out: dict[str, list[Path]] = defaultdict(list)
    for meta_path in sorted(root.rglob("meta.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(meta, dict) or meta.get("tool") != "read_corpus":
            continue
        name = str(meta.get("speaker") or "").strip() or meta_path.parent.name
        out[name].extend(sorted(p for p in meta_path.parent.glob("*_m1.wav")))
    return {k: v for k, v in out.items() if v}


def embed_file(embedder: dv.Embedder, path: Path) -> Optional[Any]:
    try:
        return embedder.embed(load_wav(path))
    except Exception:  # noqa: BLE001 — 1 発話の失敗で全体を止めない
        return None


def enroll_corpora(embedder: dv.Embedder, corpora: dict[str, list[Path]],
                   log: Callable[[str], None] = print) -> list[dv.Voice]:
    voices = []
    for name, files in corpora.items():
        voice = dv.enroll(name, (embed_file(embedder, p) for p in files))
        if voice is not None:
            voices.append(voice)
            log(f"  登録: {name}（読み上げ集の句 {voice.count} 個・登録の発話との近さの中央値 {voice.spread:.2f}）")
    return voices


def _jst(ts: Optional[float]) -> str:
    return datetime.fromtimestamp(ts, JST).strftime("%m/%d %H:%M:%S") if ts else "?"


def _quantiles(values: list[float]) -> str:
    if not values:
        return "—"
    values = sorted(values)

    def q(p: float) -> float:
        return values[min(len(values) - 1, int(p * (len(values) - 1) + 0.5))]

    return f"10% {q(0.1):.2f} / 中央 {q(0.5):.2f} / 90% {q(0.9):.2f}"


def auc(positives: list[float], negatives: list[float]) -> Optional[float]:
    """positives が negatives より高く出る確率（同点は半分）。"""
    if not positives or not negatives:
        return None
    wins = sum((p > n) + 0.5 * (p == n) for p in positives for n in negatives)
    return wins / (len(positives) * len(negatives))


def canned_labels() -> dict[str, str]:
    try:
        data = json.loads(CANNED_LABELS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {item["audio_file"]: item["kind"] for item in data.get("items", []) if item.get("audio_file")}


def score_session(session: Any, embedder: dv.Embedder, voices: list[dv.Voice], out_dir: Path,
                  log: Callable[[str], None] = print) -> list[dict]:
    rows = [r for r in read_jsonl(session.transcripts)
            if r.get("audio_file") and not r.get("no_speech") and session.audio_path(r["audio_file"]) is not None]
    measured: list[tuple[dict, Any]] = []
    for row in rows:
        vec = embed_file(embedder, session.audio_path(row["audio_file"]))
        if vec is not None:
            measured.append((row, vec))
    used = voices
    if not used:
        auto = [v for r, v in measured if category(r) == ACTION and float(r.get("audio_sec") or 0) >= AUTO_MIN_SEC]
        voice = dv.enroll(f"auto:{session.session_id[:8]}", auto)
        used = [voice] if voice is not None else []
        if voice is not None:
            log(f"  仮の登録（アクションに読めた {voice.count} 発話）・近さの中央値 {voice.spread:.2f}")
    out = []
    for row, vec in measured:
        best, scores = dv.nearest(vec, used)
        out.append({
            "audio_file": row["audio_file"], "utterance_start_ts": row.get("utterance_start_ts"),
            "audio_sec": row.get("audio_sec"), "text": row.get("text") or "", "category": category(row),
            "best": best, "similarity": scores.get(best) if best else None, "scores": scores,
            "model": embedder.name,
        })
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / f"{session.session_id}.voice.jsonl").open("w", encoding="utf-8") as f:
        for item in out:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
    return out


def report(session_id: str, items: list[dict], *, low: float, listing: int, labels: dict[str, str],
           log: Callable[[str], None] = print) -> dict:
    by_cat: dict[str, list[float]] = defaultdict(list)
    for it in items:
        if it["similarity"] is not None:
            by_cat[it["category"]].append(it["similarity"])
    log(f"\n■ {session_id}（測った発話 {len(items)}）")
    names = {ACTION: "アクションに読めた", CANNED: "決まり文句", OTHER: "それ以外"}
    for cat in (ACTION, CANNED, OTHER):
        vals = by_cat.get(cat, [])
        below = sum(1 for v in vals if v < low)
        log(f"  {names[cat]:<10} {len(vals):>4} 発話  近さ {_quantiles(vals)}  {low:.2f} 未満 {below}")
    summary: dict[str, Any] = {"session": session_id, "counts": {c: len(v) for c, v in by_cat.items()},
                               "low_actions": sum(1 for v in by_cat.get(ACTION, []) if v < low)}
    action_auc = auc(by_cat.get(ACTION, []), by_cat.get(CANNED, []) + by_cat.get(OTHER, []))
    if action_auc is not None:
        log(f"  アクションの発話がそれ以外より近く出る確率（AUC）{action_auc:.2f}")
        summary["auc_action_vs_rest"] = round(action_auc, 3)
    labelled = [(labels[it["audio_file"]], it["similarity"]) for it in items
                if it["audio_file"] in labels and it["similarity"] is not None]
    if labelled:
        pos = [s for k, s in labelled if k == "action"]
        neg = [s for k, s in labelled if k == "none"]
        a = auc(pos, neg)
        log(f"  オーナーが聞いた決まり文句: action {len(pos)}・none {len(neg)} → AUC {a:.2f}" if a is not None
            else f"  オーナーが聞いた決まり文句: {len(labelled)} 発話（片方しか無い）")
        summary["canned51"] = {"action": len(pos), "none": len(neg), "auc": None if a is None else round(a, 3)}
    if len({it["best"] for it in items if it["best"]}) > 1:
        buckets: dict[int, Counter] = defaultdict(Counter)
        for it in items:
            if it["category"] == ACTION and it["best"] and it["utterance_start_ts"]:
                buckets[int(it["utterance_start_ts"] // BUCKET_SEC)][it["best"]] += 1
        log("  10 分ごとのいちばん近い登録（アクションの発話）:")
        for b in sorted(buckets):
            counts = "・".join(f"{n} {c}" for n, c in buckets[b].most_common())
            log(f"    {_jst(b * BUCKET_SEC)[6:11]}〜  {counts}")
    lowest = sorted((it for it in items if it["category"] == ACTION and it["similarity"] is not None),
                    key=lambda it: it["similarity"])[:listing]
    if lowest:
        log("  近さの低いアクションの発話（聞いて確かめる。ディーラー以外の声か）:")
        for it in lowest:
            log(f"    {it['similarity']:.2f}  {_jst(it['utterance_start_ts'])}  {it['audio_file']}  「{it['text']}」")
    return summary


def ensure_model(name: str, log: Callable[[str], None] = print) -> Optional[Path]:
    path = dv.model_path(ROOT, name)
    if path.is_file():
        return path
    log(f"[dealer_voice] 話者のモデル {name} を取得します → {path}")
    try:
        dv.download_model(path, name)
    except Exception as e:  # noqa: BLE001
        log(f"[dealer_voice] 取得できませんでした: {type(e).__name__}: {e}")
        return None
    return path


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="ディーラーの声の照合をオフラインで確かめる（台帳 I）")
    sub = parser.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="話者のモデルを取得する")
    f.add_argument("--model", choices=sorted(dv.MODELS), default=dv.DEFAULT_MODEL)
    s = sub.add_parser("score", help="読み上げ集で声を登録し、セッションの発話ごとの近さを出す")
    s.add_argument("inputs", nargs="+", type=Path, help="pack_logs の zip（分けた zip は全部）/ 展開したフォルダ / logs")
    s.add_argument("--model", choices=sorted(dv.MODELS), default=dv.DEFAULT_MODEL)
    s.add_argument("--session", action="append", help="セッション ID の先頭（複数回可）")
    s.add_argument("--low", type=float, default=DEFAULT_LOW, help="アクションの発話を疑う近さ")
    s.add_argument("--list", type=int, default=20, dest="listing", help="近さの低いアクションの発話を何件出すか")
    s.add_argument("--no-corpus", action="store_true", help="読み上げ集を使わず、セッションごとに仮に登録する")
    s.add_argument("--out", type=Path, default=ROOT / "logs" / "voice")
    args = parser.parse_args(argv)

    path = ensure_model(args.model)
    if path is None:
        return 1
    if args.cmd == "fetch":
        print(f"[dealer_voice] {path}")
        return 0

    root, tmp = open_input(list(args.inputs))
    try:
        embedder = dv.OnnxEmbedder(path, args.model)
        corpora = {} if args.no_corpus else find_corpora(root)
        print(f"[dealer_voice] モデル {args.model}・読み上げ集 {len(corpora)} 人")
        voices = enroll_corpora(embedder, corpora)
        sessions = find_sessions(root, args.session)
        if not sessions:
            print("[dealer_voice] 聞き取りの記録（*.transcripts.jsonl）のあるセッションがありません")
            return 1
        labels = canned_labels()
        summaries = []
        for session in sessions:
            started = time.perf_counter()
            items = score_session(session, embedder, voices, args.out)
            if not items:
                print(f"\n■ {session.session_id}: 音声のある発話がありません（音声付きの zip か）")
                continue
            summary = report(session.session_id, items, low=args.low, listing=args.listing, labels=labels)
            per = (time.perf_counter() - started) / len(items)
            print(f"  1 発話 {per * 1000:.0f} ms")
            summaries.append(summary)
        if summaries:
            total = sum(s["counts"].get(ACTION, 0) for s in summaries)
            low_total = sum(s["low_actions"] for s in summaries)
            share = low_total / total if total else 0.0
            print(f"\n合計: アクションに読めた発話 {total} のうち近さ {args.low:.2f} 未満 {low_total}（{share:.1%}）"
                  f" → 出力 {args.out}")
        return 0
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
