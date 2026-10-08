"""tools/review_page.py

開発データ（`tests/fixtures/store/<日付>-<sid 8 桁>/`）のハンドを、真のアクションの入力画面（`tools/ground_truth_ui.py`）と
同じ形で見直すページを作る（オーナー 2026-10-08: 店舗 PC を触れないときに、確認待ちのハンドを見直して直す）。

- ハンドごとに: 卓の設定・**記録**（いまの規則での読み直し = `SessionEstimator.windows()` の直しの無い再生）・**推定**
  （推定器の 1 番）・**真のアクション**（`expected.json` の `truth`）・**時系列**（窓の発話の Whisper の文・第 2 の耳の文・
  オーナーの聞き取りのラベル・読み取ったアクション、ボードの札、席の札の離脱と戻り）・確認してほしい問い。
- 発話には音声の番号を付ける。音声はページに入れない（店舗の音声は公開しない）。`--audio` を渡すと、その番号の名前
  （`<sid 8 桁>_h<ハンド>_<番号>_<+秒>.wav`）で発話の WAV を `--audio-out` にコピーする（チャットで送る）。
- ページは 1 枚の HTML（材料を埋め込む）。直した真のアクションは claude.ai のページの共有データ（`db` の `reviews`、
  文書 `<sid 8 桁>_<ハンド>` = `validate_gt_hand` と同じ形の `hand`）に保存する。読み戻しは `load_reviews`。

使い方:

    python tools/review_page.py tests/fixtures/store/2026-10-06-e82f5005:15 tests/fixtures/store/2026-09-29-d0f055fb:8 \\
        --questions questions.json --out review.html --audio <店舗ログを展開したフォルダ> --audio-out <WAV の置き場>
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from pathlib import Path
from typing import Any, Iterable, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TEMPLATE = Path(__file__).resolve().parent / "review_page_template.html"
DATA_MARK = "/*REVIEW_DATA*/null"
STREETS = ("preflop", "flop", "turn", "river")


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _estimator(folder: Path):
    from integration.estimator import SessionEstimator
    from integration.replay import load_events
    from integration.world_replay import PresenceTimeline

    expected = json.loads((folder / "expected.json").read_text(encoding="utf-8"))
    setup = expected["setup"]
    presence = PresenceTimeline.from_rows(_read_jsonl(folder / "presence.jsonl"))
    transcripts = _read_jsonl(folder / "transcripts.jsonl")
    flags = {k: bool(setup.get(k)) for k in ("auto_new_hand", "auto_winner", "rfid_folds")}
    est = SessionEstimator(load_events(folder / "events.jsonl"), transcripts, presence, setup, flags,
                           expected["session_id"])
    return est, expected


def _labels(folder: Path) -> dict[float, dict]:
    path = folder / "utterance_labels.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {round(float(k), 3): v for k, v in (data.get("labels") or {}).items()}


def _rows(hand: Optional[dict]) -> list[dict]:
    out = []
    for a in (hand or {}).get("actions") or []:
        row = {"street": a.get("street"), "seat": a.get("seat"), "action": a.get("action"),
               "amount": int(a.get("amount") or 0)}
        if a.get("raw_text"):
            row["heard"] = a["raw_text"]
        if a.get("needs_review"):
            row["review"] = True
        out.append(row)
    return out


def _record(hand: Optional[dict]) -> dict:
    hand = hand or {}
    return {
        "board": list(hand.get("board") or []),
        "actions": _rows(hand),
        "winner_seat": hand.get("winner_seat"),
        "winner_source": hand.get("winner_source"),
        "button_seat": hand.get("button_seat"),
        "players": [{"seat": p.get("seat"), "hole_cards": list(p.get("hole_cards") or [])}
                    for p in hand.get("players") or []],
    }


def wav_name(sid8: str, hand_id: int, number: int, offset: float) -> str:
    """送る発話の WAV の名前（ページの音声の番号と同じ）。"""
    return f"{sid8}_h{hand_id}_{number:02d}_+{offset:.0f}s.wav"


def _timeline(est, w, labels: dict[float, dict], sid8: str) -> list[dict]:
    from audio.recognizer import parse_actions

    items: list[dict] = []
    number = 0
    for r in est.transcripts:
        start = float(r["utterance_start_ts"])
        if not (w.start - 0.5 <= start < w.end):
            continue
        number += 1
        offset = start - w.start
        text = r.get("text") or ""
        ear = (r.get("ear") or {}).get("text") or ""
        try:
            parsed = [f"{e.action}{(' ' + str(e.amount)) if e.amount else ''}" for e in parse_actions(text)]
        except Exception:                                   # 材料づくりは読み取りの失敗で止めない
            parsed = []
        label = labels.get(round(start, 3))
        items.append({
            "kind": "speech", "t": round(offset, 1), "start": start, "no": number,
            "wav": wav_name(sid8, w.hand_id, number, offset),
            "text": text, "ear": ear, "parsed": parsed,
            "label": (label or {}).get("label"), "label_note": (label or {}).get("note"),
            "label_item": (label or {}).get("item"),
        })
    for card in (w.base.get("board_timeline") or []):
        from integration.estimator import _epoch

        t = _epoch(card.get("dealt_at"))
        if t is not None:
            items.append({"kind": "board", "t": round(t - w.start, 1), "card": card.get("card"),
                          "index": card.get("index")})
    if est.presence is not None:
        for seat, t, back in est.presence.departures(w.start, w.end):
            items.append({"kind": "leave", "t": round(t - w.start, 1), "seat": seat,
                          "back": None if back is None else round(back - w.start, 1)})
    items.sort(key=lambda x: (x["t"], 0 if x["kind"] != "speech" else 1))
    return items


def build_hand(folder: Path, hand_id: int, question: str = "", estimate: bool = True) -> dict:
    """1 ハンドのページの材料。"""
    est, expected = _estimator(folder)
    sid = expected["session_id"]
    sid8 = sid[:8]
    w = next(x for x in est.windows() if x.hand_id == hand_id)
    truth = next((h.get("truth") for h in expected["hands"] if h["hand_id"] == hand_id), None) or {}
    best = None
    if estimate:
        result = est.estimate_hand(w)
        best = dict(_record(result.best.hand), reasons=list(result.reasons))
    setup = expected["setup"]
    stacks = {p["seat"]: p.get("stack_start") for p in w.base.get("players") or []}
    return {
        "key": f"{sid8}_{hand_id}",
        "session_id": sid,
        "folder": folder.name,
        "hand_id": hand_id,
        "question": question,
        "blinds": w.base.get("blinds") or {"sb": setup.get("sb"), "bb": setup.get("bb")},
        "seats": [{"seat": p["seat"], "name": p.get("name"), "stack": stacks.get(p["seat"])}
                  for p in setup["players"]],
        "record": _record(w.base),
        "estimate": best,
        "truth": truth,
        "timeline": _timeline(est, w, _labels(folder), sid8),
    }


def render(hands: list[dict], template: Path = TEMPLATE) -> str:
    """材料を埋め込んだページ。`</script>` で埋め込みが切れないように `<` を逃がす。"""
    page = template.read_text(encoding="utf-8")
    data = json.dumps({"hands": hands}, ensure_ascii=False).replace("<", "\\u003c")
    if DATA_MARK not in page:
        raise ValueError("テンプレートに材料の置き場がありません")
    return page.replace(DATA_MARK, data)


def copy_audio(hands: Iterable[dict], roots: list[Path], out: Path) -> list[Path]:
    """発話の WAV を番号の名前でコピーする（見つからないものは飛ばす）。"""
    out.mkdir(parents=True, exist_ok=True)
    copied = []
    for h in hands:
        sid8 = h["session_id"][:8]
        audio_dirs = [d for root in roots for d in root.glob(f"**/{sid8}*/audio") if d.is_dir()]
        for item in h["timeline"]:
            if item["kind"] != "speech":
                continue
            name = f"{int(item['start'] * 1000)}.wav"
            src = next((d / name for d in audio_dirs if (d / name).exists()), None)
            if src is None:
                continue
            dst = out / item["wav"]
            shutil.copyfile(src, dst)
            copied.append(dst)
    return copied


def load_reviews(rows: Iterable[dict]) -> dict[str, dict]:
    """ページの共有データ（`reviews` の文書）を、検査した真のアクションに（キー → hand）。不正な文書は飛ばす。"""
    from tools.ground_truth_ui import GroundTruthError, validate_gt_hand

    out: dict[str, dict] = {}
    for row in rows:
        data = row.get("data", row)
        try:
            out[str(row.get("id") or data.get("key"))] = validate_gt_hand(data.get("hand"))
        except (GroundTruthError, AttributeError):
            continue
    return out


def _parse_target(text: str) -> tuple[Path, int]:
    folder, _, hand = text.rpartition(":")
    return Path(folder), int(hand)


def main(argv: Optional[list[str]] = None) -> int:
    logging.disable(logging.WARNING)
    ap = argparse.ArgumentParser(description="開発データのハンドを真のアクションの入力画面と同じ形で見直すページを作る")
    ap.add_argument("targets", nargs="+", help="<fixture のフォルダ>:<ハンド番号>")
    ap.add_argument("--questions", type=Path, help="キー（<sid 8 桁>_<ハンド>）→ 問いの文の JSON")
    ap.add_argument("--out", type=Path, required=True, help="書き出す HTML")
    ap.add_argument("--audio", type=Path, action="append", default=[], help="店舗のログを展開したフォルダ（WAV を探す）")
    ap.add_argument("--audio-out", type=Path, help="番号の名前で WAV をコピーするフォルダ")
    ap.add_argument("--no-estimate", action="store_true", help="推定器を回さない（速い）")
    args = ap.parse_args(argv)
    questions: dict[str, Any] = json.loads(args.questions.read_text(encoding="utf-8")) if args.questions else {}
    hands = []
    for target in args.targets:
        folder, hand_id = _parse_target(target)
        key = f"{folder.name.rsplit('-', 1)[-1]}_{hand_id}"
        hands.append(build_hand(folder, hand_id, questions.get(key, ""), estimate=not args.no_estimate))
    args.out.write_text(render(hands), encoding="utf-8")
    print(f"ページ: {args.out}（{len(hands)} ハンド）")
    if args.audio and args.audio_out:
        copied = copy_audio(hands, args.audio, args.audio_out)
        print(f"音声: {len(copied)} 個 → {args.audio_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
