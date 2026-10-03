"""tools/eval_store.py

店舗のログを **いまのコードで再生して評価する**（ADR-0056 追記 1 の S0 = 事後推定へ向けた評価の土台）。

入力は pack_logs の zip（`pokerlogs_<日時>.zip`）・それを展開したフォルダ・`logs/` のどれでもよい。セッションごとに:

1. 記録（`<sid>.json`）から卓の設定（席・名前・持ち点・ブラインド・1 ハンド目のボタン）を取り、
   `<sid>.events.jsonl` を再生する（`integration.replay`）。
2. 記録時の結果と再生の結果を突き合わせる。同じコードなら違いは再現性の不具合、違うコードなら修正の効果。
3. 真のアクション（`<sid>.ground_truth.json`）があれば、記録・再生それぞれの一致率（アライメント =
   `measure_capture_accuracy` と同じ計算）と、**要確認の精度と再現率**（要確認を付けた行のうち誤りの割合 /
   誤りのうち要確認が付いた割合）、アクションの出所ごとの正誤、聞き取りの自信（正しい行 / 誤った行）。
4. `--timeline`: ハンドごとに発話・RFID の信号・入力を時刻順に並べ、真のアクション・記録・再生の行を比べる。
5. `--export-fixture DIR`: 真のアクションのあるセッションを回帰テスト用に書き出す（`tests/test_store_fixtures.py`
   が、真のアクションとの一致が書き出したときより悪くならないことを確かめる）。
6. 聞き取りの読み直し（S2）: 書き起こし（`<sid>.transcripts.jsonl`）を**いまの読み取り**で読み直して再生する
   （記録したときの読み取りとの差 = 読み取りの修正の効果）。音声を採点し直した結果（`<sid>.rescored.jsonl`,
   `tools/rescore_audio.py`）があれば、いちばん確からしい文で読み直した再生も並べ、書き起こしと違った発話を出す。
7. 第 2 の耳（`<sid>.ear.jsonl`）と Whisper の別のやり方（`<sid>.whisper.jsonl`, どちらも `tools/second_ear.py`）
   があれば、方式ごと（`_ROUTE_LABELS`）に書き起こしを置き換えて再生し、真のアクションとの一致を並べる。

使い方:

    python tools/eval_store.py pokerlogs_20260927_152645.zip
    python tools/eval_store.py logs --session 8d08c010 --timeline
    python tools/eval_store.py pokerlogs_20260927_152645.zip --export-fixture tests/fixtures/store
    python tools/eval_store.py logs --json
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import statistics
import sys
import tempfile
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

# リポジトリ直下を import path に入れる（他の tools/ と同じ規約）。
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from audio.recognizer import is_implausibly_long, is_prompt_echo, is_question, parse_actions  # noqa: E402
from audio.second_ear import agreed_candidate, apply_ear  # noqa: E402
from core.events import AudioEvent, RFIDEvent  # noqa: E402
from core.game_state import PlayerState  # noqa: E402
from integration.replay import load_events, replay_events  # noqa: E402
from integration.world_replay import PresenceTimeline  # noqa: E402
from tools.measure_capture_accuracy import (  # noqa: E402
    _align_actions,
    hand_fully_correct,
    measure_hand,
    measure_session,
    row_correct,
)
from tools.pack_logs import code_fingerprint  # noqa: E402
from tools.test_script import MARKS_SUFFIX, SCRIPT_SUFFIX, script_steps, script_truth, session_table  # noqa: E402

# 真のアクションのファイルで、ハンドの中身ではない項目（入力した人・時刻・入れ方）
_GT_META = ("annotator", "annotated_at", "source")
# アクションの出所（`actor_source`）の分け方
_SOURCE_GROUPS = {
    "engine_prior": "音声", "spoken_seat": "音声", "spoken_position": "音声", "spoken_fold": "音声",
    "implied": "補った", "rfid_departure": "RFID", "rfid_muck": "RFID",
}
_SIGNAL_NAMES = {
    "leave": "札が離れた", "muck": "札が中央を通過", "return": "札が戻った", "street": "ボードでストリート",
    "spoken_fold": "「フォールド」の保留", "reinterpret": "解釈し直し", "confirm": "確定", "showdown": "ショーダウン",
}


# ――― 入力 ―――

@dataclass
class SessionFiles:
    """1 セッションのファイル（`<sid>.events.jsonl` と同じフォルダの `<sid>.*`）。"""

    session_id: str
    folder: Path

    def path(self, suffix: str) -> Optional[Path]:
        p = self.folder / f"{self.session_id}{suffix}"
        return p if p.exists() else None

    @property
    def events(self) -> Path:
        return self.folder / f"{self.session_id}.events.jsonl"


def open_input(path: "Path | list[Path]") -> tuple[Path, Optional[Path]]:
    """zip なら一時フォルダに展開する。(読むフォルダ, あとで消す一時フォルダ) を返す。

    zip は複数でもよい（pack_logs が大きいログを `_1of3.zip`… に分けたもの）: 同じ一時フォルダに全部展開する。
    """
    paths = path if isinstance(path, list) else [path]
    zips = [p for p in paths if p.is_file() and p.suffix.lower() == ".zip"]
    if not zips:
        return paths[0], None
    tmp = Path(tempfile.mkdtemp(prefix="eval_store_"))
    for z in zips:
        with zipfile.ZipFile(z) as zf:
            zf.extractall(tmp)
    return tmp, tmp


def find_sessions(root: Path, only: Optional[list[str]] = None) -> list[SessionFiles]:
    sessions = []
    for events in sorted(root.rglob("*.events.jsonl")):
        sid = events.name[: -len(".events.jsonl")]
        if only and not any(sid.startswith(prefix) for prefix in only):
            continue
        sessions.append(SessionFiles(sid, events.parent))
    return sessions


def _read_json(path: Optional[Path]) -> Optional[Any]:
    if path is None:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def _read_jsonl(path: Optional[Path]) -> list[dict]:
    if path is None:
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def find_config(root: Path) -> dict:
    """pack_logs の zip は直下に `config.json`、`logs/` はその親（アプリのフォルダ）にある。"""
    for folder in (root, root.parent):
        data = _read_json(folder / "config.json" if (folder / "config.json").exists() else None)
        if isinstance(data, dict):
            return data
    return {}


# ――― 卓の設定と再生 ―――

def session_setup(record: dict) -> Optional[dict]:
    """記録から卓の設定を取る: 席・名前・最初に配られたときの持ち点・最初のハンドのブラインドとボタン・
    ハンドごとの持ち点（`hand_stacks`）。

    ボタンは `create_game_state` と同じく「1 ハンド目のボタンの 1 つ手前の席」で渡す（main.py と同じ計算）。
    `hand_stacks` = ハンドごとの記録の持ち点。再生は各ハンドをこの持ち点から始める（真のアクションのオールインの額は
    記録の持ち点から決めているので、前のハンドの違いを持ち越すと、直したハンドのあとが違って見える。店舗 2026-09-29:
    第 2 の耳で 2 ハンド目の額が直ると、8 ハンド目のオールインの額が真のアクションと合わなくなった）。
    """
    hands = [h for h in (record or {}).get("hands") or [] if isinstance(h, dict)]
    if not hands:
        return None
    players: dict[int, dict] = {}
    for hand in hands:
        for p in hand.get("players") or []:
            seat = p.get("seat")
            if isinstance(seat, int) and seat not in players and p.get("stack_start") is not None:
                players[seat] = {"seat": seat, "name": p.get("name") or f"P{seat}", "stack": int(p["stack_start"])}
    if len(players) < 2:
        return None
    first = hands[0]
    blinds = first.get("blinds") or {}
    seats = sorted(players)
    button = first.get("button_seat")
    prior = seats[(seats.index(button) - 1) % len(seats)] if button in seats else None
    hand_stacks = {
        str(h["hand_id"]): {str(p["seat"]): int(p["stack_start"]) for p in h.get("players") or []
                            if isinstance(p.get("seat"), int) and p.get("stack_start") is not None}
        for h in hands if isinstance(h.get("hand_id"), int)
    }
    return {
        "players": [players[s] for s in seats],
        "sb": int(blinds.get("sb") or 100), "bb": int(blinds.get("bb") or 200),
        "button_prior": prior, "first_hand_id": first.get("hand_id"),
        "hand_stacks": {k: v for k, v in hand_stacks.items() if v},
    }


def replay_flags(config: dict, events: list, script: Optional[dict] = None) -> dict:
    """店舗の設定どおりに再生する（無ければ店舗の既定: 手札でハンド開始・勝者の自動判定・札の離脱でフォールド）。

    声だけの台本（`script` の kind が voice）は RFID を使わずに動いた（`main.py --script voice`）。店の設定の RFID は
    有効でも、札の離脱でフォールドにしない（していると 30 ハンド中 11 ハンドが記録と違う再生になった, 2026-09-30）。
    """
    engine = config.get("engine") or {}
    rfid = config.get("rfid") or {}
    if script is not None and script.get("kind") == "voice":
        presence = False
    elif rfid:
        presence = bool(rfid.get("enabled")) and rfid.get("transport") == "pcsc"
    else:
        presence = any(isinstance(e, RFIDEvent) and e.role == "seat" for e in events)
    return {
        "auto_new_hand": bool(engine.get("auto_new_hand", True)),
        "auto_winner": bool(engine.get("auto_winner", True)),
        "rfid_folds": bool(engine.get("rfid_folds", True)) and presence,
    }


def replay_session(events: "list | Path", setup: dict, flags: dict, session_id: str,
                   on_action: Optional[Callable[[Any], None]] = None) -> list[dict]:
    """events.jsonl（または読み込んだ入力の列）を再生して、記録（`<sid>.json`）と同じ形のハンドの辞書を返す。

    `on_action`: 再生中の通知（反映できなかった発話も来る。`integration/estimator.py` が採点に使う）。
    """
    if isinstance(events, Path):
        events = load_events(events)
    players = [PlayerState(seat=p["seat"], name=p["name"], stack=p["stack"]) for p in setup["players"]]
    # JSON（fixture の expected.json）のキーは文字列。hand_stacks の無い古い fixture は持ち点を持ち越す
    hand_stacks = {int(h): {int(s): int(v) for s, v in seats.items()}
                   for h, seats in (setup.get("hand_stacks") or {}).items()}
    with tempfile.TemporaryDirectory(prefix="eval_replay_") as tmp:
        replay_events(
            events, backend="pokerkit", players=players, sb=setup["sb"], bb=setup["bb"],
            session_id=session_id, out_dir=tmp, button_seat=setup.get("button_prior"), close_open_hand=True,
            hand_stacks=hand_stacks or None, on_action=on_action, **flags,
        )
        data = _read_json(Path(tmp) / f"{session_id}.json") or {}
    return list(data.get("hands") or [])


# ――― 聞き取りの読み直し（S2）―――

def _is_noise(row: dict, text: str) -> bool:
    """声ではない音・雑音への幻聴（ライブと同じ判定を、いまのコードで）。"""
    return bool(row.get("no_speech")) or not text or is_prompt_echo(text) or is_implausibly_long(
        text, float(row.get("audio_sec") or 0.0))


def script_order_events(script: dict, marks: list[dict], transcripts: list[dict]) -> list:
    """声だけの台本のセッションの入力を、台本の画面を押した順に並べ直す（発話は話し始めの順）。

    ロガーは「始める」を押した時刻より前に話し始めた発話を聞き取ってから、台本のハンドを始める
    （`ControlConsumerThread`）。この順で再生すれば、聞き取りが遅れても台本のハンドと発話の対応は押した時刻どおり。
    2026-09-30 の台本はハンドの開始が最大 60 秒遅れて次のハンドの行が前のハンドに入り、記録は評価に使えなかった。
    書き起こしはいまの読み取りで読み直す（ライブの第 2 の耳の結果も同じ規則で重ねる）。
    """
    from core.control_queue import ControlCommand, command_to_audio_event, script_hand_text

    by_n = {int(h["n"]): h for h in script.get("hands") or []}
    events: list = []
    for m in marks:
        t = m.get("t")
        if not isinstance(t, (int, float)):
            continue
        if m.get("event") == "start" and m.get("hand") in by_n:
            spec = by_n[m["hand"]]
            stacks = {int(k): int(v) for k, v in spec["stacks"].items()}
            events.append(AudioEvent(action="script_hand", amount=0, timestamp=float(t),
                                     raw_text=script_hand_text(int(spec["button"]), stacks)))
        elif m.get("event") == "winner":
            args = {k: m[k] for k in ("seat", "seats") if k in m}
            ev = command_to_audio_event(ControlCommand("script", "winner", args, ""), lambda: float(t) - 0.001)
            if ev is not None:
                events.append(ev)            # 次のハンドの「始める」と同じ時刻に送った = その前に
    for row in transcripts:
        start = row.get("utterance_start_ts")
        if start is None:
            continue
        text = (row.get("text") or "").strip()
        parsed = [] if _is_noise(row, text) else parse_actions(
            text, confidence=row.get("confidence"), utterance_start_ts=start)
        parsed, _ = apply_ear(parsed, text, row.get("ear"), question=is_question(text), utterance_start_ts=start,
                              confidence=row.get("confidence"))
        for i, ev in enumerate(parsed):
            ev.timestamp = float(start) + 0.001 * (i + 1)
        events.extend(parsed)
    return sorted(events, key=lambda e: e.timestamp)


def script_setup(script: dict) -> dict:
    """台本の卓（`replay_session` の setup の形）。"""
    table = session_table(script)
    return {"players": [{"seat": p["seat"], "name": p["name"], "stack": p["stack"]} for p in table["players"]],
            "sb": table["sb"], "bb": table["bb"], "button_prior": table["button_seat"], "first_hand_id": 1}


def score_script_order(script: dict, marks: list[dict], hands: list[dict]) -> dict:
    """`script_order_events` を再生したハンド（k 回目の「始める」= ハンド k）を台本と比べる（やり直しは後の方）。"""
    by_n = {int(h["n"]): h for h in script.get("hands") or []}
    starts = [m for m in marks if m.get("event") == "start" and m.get("hand") in by_n]
    latest = {m["hand"]: k + 1 for k, m in enumerate(starts)}
    got = {h.get("hand_id"): h for h in hands}
    rows = total = winners = exact = 0
    missed: list[dict] = []
    for n, hand_id in sorted(latest.items()):
        spec = by_n[n]
        truth = {"hand_id": hand_id, "actions": spec["actions"], "winner_seat": spec.get("winner_seat"), "board": []}
        hand = got.get(hand_id)
        if hand is None:
            total += len(spec["actions"])
            missed.append({"hand": n, "scenario": spec.get("scenario"), "rows": 0, "total": len(spec["actions"]),
                           "winner": False})
            continue
        acc = measure_hand(truth, hand)
        rows, total = rows + acc.action_correct, total + acc.action_total
        winners += bool(acc.winner_match)
        if acc.action_correct == acc.action_total and acc.winner_match:
            exact += 1
        else:
            missed.append({"hand": n, "scenario": spec.get("scenario"), "rows": acc.action_correct,
                           "total": acc.action_total, "winner": bool(acc.winner_match)})
    return {"hands": len(latest), "rows": rows, "total": total, "winners": winners, "exact": exact, "missed": missed}


def reparse_events(events: list, transcripts: list[dict], texts: Optional[dict[Any, str]] = None) -> list:
    """音声のアクションを、書き起こしから**いまの読み取り**で読み直したものに置き換えた入力の列。

    texts: 音声のファイル名（または発話の始まりの時刻 `utterance_start_ts`。音声のファイル名を残さない fixture 用）
    → 読み直す文（採点し直した文・第 2 の耳の候補）。空の文 = アクションにしない。無い
    発話は書き起こしのまま。打った入力（書き起こしに無い行）と RFID の入力はそのまま。記録に行があった発話は
    その位置・時刻に、無かった発話は聞き取った時刻の位置に入れる。
    ライブで第 2 の耳が聞き直した結果（行の `ear`）があれば、いまの規則（`second_ear.apply_ear`: 読めない発話の
    聞き直し・額の無いベット / レイズの額）で重ねる（ライブと同じ。texts で置き換えた発話には使わない）。
    """
    rows = {r["utterance_start_ts"]: r for r in transcripts if r.get("utterance_start_ts") is not None}
    live_time: dict[float, float] = {}
    for e in events:
        if isinstance(e, AudioEvent) and e.utterance_start_ts in rows:
            live_time.setdefault(e.utterance_start_ts, e.timestamp)
    reparsed: dict[float, list[AudioEvent]] = {}
    for start, row in rows.items():
        name = row.get("audio_file") or ""
        key = name if texts is not None and name and name in texts else start
        overridden = texts is not None and key in texts
        text = ((texts[key] if overridden else row.get("text")) or "").strip()
        parsed = [] if _is_noise(row, text) else parse_actions(
            text, confidence=row.get("confidence"), utterance_start_ts=start)
        if not overridden:      # 第 2 の耳が無い発話も（意味のない単発の語を音の近さで額と読む, ライブと同じ）
            parsed, _ = apply_ear(parsed, text, row.get("ear"), question=is_question(text), utterance_start_ts=start,
                                  confidence=row.get("confidence"))
        at = live_time.get(start, row.get("heard_at") or start)
        for ev in parsed:
            ev.timestamp = at
        if parsed:
            reparsed[start] = parsed
    out: list = []
    for e in events:
        if isinstance(e, AudioEvent) and e.utterance_start_ts in rows:
            out.extend(reparsed.pop(e.utterance_start_ts, []))
            continue
        out.append(e)
    rest = sorted((ev for evs in reparsed.values() for ev in evs), key=lambda ev: ev.timestamp)
    merged: list = []
    i = 0
    for e in out:
        while i < len(rest) and rest[i].timestamp < e.timestamp:
            merged.append(rest[i])
            i += 1
        merged.append(e)
    merged.extend(rest[i:])
    return merged


def _read_actions(text: str, row: dict) -> Optional[list[AudioEvent]]:
    """いまの読み取りで読んだアクション（雑音なら None）。"""
    return None if _is_noise(row, text) else parse_actions(text)


def _actions_text(text: str, row: dict) -> str:
    events = _read_actions(text, row)
    if events is None:
        return "（雑音）"
    parts = [e.action + (f" {e.amount}" if e.amount else "") + (f" 席{e.seat}" if e.seat else "")
             + ("（音の近さ）" if "fuzzy_keyword" in e.parse_flags else "") for e in events]
    return ", ".join(parts) or "（読まない）"


def _action_keys(text: str, row: dict) -> Optional[list[tuple]]:
    events = _read_actions(text, row)
    return None if events is None else [(e.action, e.amount, e.seat, e.position) for e in events]


def listening_summary(transcripts: list[dict], rescored: list[dict]) -> dict:
    """採点し直した発話のうち、いちばん確からしい文が書き起こしと違ったもの（読んだアクションの違いも）。"""
    rows = {r.get("audio_file"): r for r in transcripts if r.get("audio_file")}
    changed = []
    n = errors = 0
    for r in rescored:
        if r.get("error") or not r.get("best"):
            errors += 1 if r.get("error") else 0
            continue
        n += 1
        heard, best = (r.get("text") or "").strip(), r["best"].strip()
        if heard == best:
            continue
        row = rows.get(r.get("audio_file")) or {}
        scores = {x.get("text"): x.get("logprob") for x in r.get("scored") or []}
        margin = (scores[best] - scores[heard]) if heard in scores and best in scores else None
        changed.append({"audio_file": r.get("audio_file"), "utterance_start_ts": r.get("utterance_start_ts"),
                        "heard": heard, "best": best, "margin": None if margin is None else round(margin, 2),
                        "before": _actions_text(heard, row), "after": _actions_text(best, row),
                        "actions_changed": _action_keys(heard, row) != _action_keys(best, row)})
    return {
        "rescored": n, "errors": errors, "changed_text": len(changed),
        "changed_actions": sum(1 for c in changed if c["actions_changed"]), "changed": changed,
    }


# ――― 音声の改善を終える目安（オーナー決定 2026-10-02 = 作業計画の監査 §5 の止める規則）―――
# 直すのに使っていない新しいセッションで、配る人のアクションの発話が 90% 以上正しく読め、決まり文句に化けるのが
# 5% 以下。これが 2 回続いたら音声の改善を終える（上限 4 セッション・2 週間。`docs/dogfood/estimator-v1-preregistration.md`）
STOP_READ_RATE = 0.90
STOP_CANNED_RATE = 0.05
# 1 セッションでは決めない（オーナー決定 2026-10-03 = 監査 3 回目: 12〜35 発話では 90% 読めていても「2 回続く」が
# 0.4 前後 = 偶然で決まる）。真のアクションのあるハンドが 10 以上のセッションの直近 2 つを合算し、配る人が言う
# アクションが 100 以上のときだけ判定する
STOP_POOL_SESSIONS = 2
STOP_POOL_MIN_ACTIONS = 100
STOP_MIN_HANDS = 10
_SPOKEN_ACTIONS = frozenset({"fold", "check", "call", "bet", "raise", "allin"})


def _speech_token(action: str, amount: Any) -> Optional[tuple]:
    """発話の読みと真のアクションを比べる形。ベット・レイズは額だけ（店ではどちらも額だけを言い、どちらかは engine が
    決める）、ほかはアクションの種類だけ（コールの額は言わないことが多い）。チェックアラウンドは続くチェックと対応する。"""
    if action in ("bet", "raise"):
        return ("wager", int(amount or 0))
    if action in _SPOKEN_ACTIONS or action == "check_around":
        return (action, 0)
    return None


def truth_tokens(hand: dict) -> list[tuple]:
    """真のアクションのうち、配る人が言う行。ショーダウンで見せずに降りた行と、ハンドを終わらせる最後のフォールド
    （札の離脱と勝者で分かるので言わないことが多い）は数えない。"""
    actions = [a for a in hand.get("actions") or [] if isinstance(a, dict)]
    showdown = any(a.get("street") == "showdown" for a in actions)
    rows = [a for a in actions if a.get("action") in _SPOKEN_ACTIONS and a.get("street") != "showdown"]
    if rows and rows[-1].get("action") == "fold" and not showdown:      # 全員降りて終わったハンドの最後のフォールド
        rows = rows[:-1]
    return [_speech_token(a["action"], a.get("amount")) for a in rows]


def heard_tokens(transcripts: list[dict], window: tuple[float, float]) -> list[tuple]:
    """ハンドの時間帯に話し始めた発話を、ライブと同じ規則（いまの読み取り + 第 2 の耳）で読んだアクション。"""
    start, end = window
    rows = sorted((r for r in transcripts if isinstance(r.get("utterance_start_ts"), (int, float))
                   and start <= r["utterance_start_ts"] < end), key=lambda r: r["utterance_start_ts"])
    out: list[tuple] = []
    for row in rows:
        text = (row.get("text") or "").strip()
        at = row["utterance_start_ts"]
        parsed = [] if _is_noise(row, text) else parse_actions(
            text, confidence=row.get("confidence"), utterance_start_ts=at)
        parsed, _ = apply_ear(parsed, text, row.get("ear"), question=is_question(text), utterance_start_ts=at,
                              confidence=row.get("confidence"))
        out.extend(t for t in (_speech_token(e.action, e.amount) for e in parsed) if t is not None)
    return out


def matched_tokens(truth: list[tuple], heard: list[tuple]) -> int:
    """真のアクションの行のうち、発話の読みと順に対応づけられた数（最長共通部分列。チェックアラウンドは続く
    チェック 1 つ以上と対応する）。"""
    n, m = len(truth), len(heard)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            best = max(dp[i - 1][j], dp[i][j - 1])
            if heard[j - 1] == truth[i - 1]:
                best = max(best, dp[i - 1][j - 1] + 1)
            elif heard[j - 1][0] == "check_around":
                k = 0
                while k < i and truth[i - 1 - k][0] == "check":
                    k += 1
                    best = max(best, dp[i - k][j - 1] + k)
            dp[i][j] = best
    return dp[n][m]


def script_windows(script: dict, marks: list[dict]) -> dict[int, tuple[float, float]]:
    """台本のハンド n の時間帯 = 台本の画面で「始める」を押してから次の「始める」まで（やり直しは後の方）。"""
    known = {int(h["n"]) for h in script.get("hands") or []}
    starts = sorted((float(m["t"]), int(m["hand"])) for m in marks
                    if m.get("event") == "start" and m.get("hand") in known and isinstance(m.get("t"), (int, float)))
    out: dict[int, tuple[float, float]] = {}
    for i, (t, n) in enumerate(starts):
        out[n] = (t, starts[i + 1][0] if i + 1 < len(starts) else t + 600.0)
    return out


def listening_stop_metrics(gt: dict, hands: list[dict], transcripts: list[dict],
                           windows: Optional[dict[int, tuple[float, float]]] = None) -> dict:
    """音声の改善を終える目安: 真のアクションのある各ハンドで、配る人が言うアクションのうち発話の読みと合った割合と、
    認識に回した発話のうち決まり文句（`is_prompt_echo`）になった割合。ハンドの時間帯は再生したハンド（`hands`）から、
    台本のセッションは台本の画面を押した時刻から（`windows`）。"""
    windows = hand_windows(hands) if windows is None else windows
    per_hand = []
    for h in gt.get("hands") or []:
        hid = h.get("hand_id")
        truth = truth_tokens(h)
        if hid not in windows or not truth:
            continue
        per_hand.append({"hand_id": hid, "actions": len(truth),
                         "read": matched_tokens(truth, heard_tokens(transcripts, windows[hid]))})
    actions = sum(p["actions"] for p in per_hand)
    read = sum(p["read"] for p in per_hand)
    texts = [t for t in ((r.get("text") or "").strip() for r in transcripts) if t]
    canned = sum(1 for t in texts if is_prompt_echo(t))
    read_rate = read / actions if actions else None
    canned_rate = canned / len(texts) if texts else None
    meets = (read_rate is not None and canned_rate is not None
             and read_rate >= STOP_READ_RATE and canned_rate <= STOP_CANNED_RATE)
    return {"hands": len(per_hand), "actions": actions, "read": read, "read_rate": read_rate,
            "utterances": len(texts), "canned": canned, "canned_rate": canned_rate, "meets": meets,
            "per_hand": per_hand}


def pooled_stop_metrics(sessions: list[tuple[str, float, dict]]) -> Optional[dict]:
    """音声の改善を終える目安の判定（合算）。`sessions`: (セッション ID, 始まりの時刻, `listening_stop_metrics`)。

    真のアクションのあるハンドが `STOP_MIN_HANDS` 以上のセッションのうち、始まりの新しい `STOP_POOL_SESSIONS` 個を
    合算する。合算のアクションが `STOP_POOL_MIN_ACTIONS` 未満・セッションが足りなければ判定しない（`decided` = False）。
    """
    usable = sorted((s for s in sessions if s[2] and s[2].get("hands", 0) >= STOP_MIN_HANDS), key=lambda s: s[1])
    if not sessions:
        return None
    recent = usable[-STOP_POOL_SESSIONS:]
    actions = sum(s[2]["actions"] for s in recent)
    read = sum(s[2]["read"] for s in recent)
    utterances = sum(s[2]["utterances"] for s in recent)
    canned = sum(s[2]["canned"] for s in recent)
    read_rate = read / actions if actions else None
    canned_rate = canned / utterances if utterances else None
    decided = len(recent) == STOP_POOL_SESSIONS and actions >= STOP_POOL_MIN_ACTIONS and canned_rate is not None
    meets = bool(decided and read_rate >= STOP_READ_RATE and canned_rate <= STOP_CANNED_RATE)
    return {"sessions": [s[0] for s in recent], "short": [s[0] for s in sessions if s not in usable],
            "actions": actions, "read": read, "read_rate": read_rate, "utterances": utterances, "canned": canned,
            "canned_rate": canned_rate, "decided": decided, "meets": meets}


def _short_sid(session_id: str) -> str:
    """表示用のセッションの短い名前（台本の「2026-09-30_165030_script_voice」は時刻の部分）。"""
    m = re.match(r"\d{4}-\d{2}-\d{2}_(\d{6})", session_id)
    return m.group(1) if m else session_id[:8]


def format_pooled_stop(pooled: dict) -> str:
    head = (f"音声の改善を終える目安（直近 {STOP_POOL_SESSIONS} セッションの合算・真のアクションのあるハンドが"
            f" {STOP_MIN_HANDS} 以上のセッションだけ）: ")
    if not pooled["sessions"]:
        return head + f"判定しない（{STOP_MIN_HANDS} ハンド以上のセッションがありません）"
    body = (f"{'・'.join(_short_sid(s) for s in pooled['sessions'])}: アクションの発話 {pooled['read']}/{pooled['actions']}"
            f"（{_fmt_pct(pooled['read_rate'])}）が正しく読めた・決まり文句 {pooled['canned']}/{pooled['utterances']}"
            f"（{_fmt_pct(pooled['canned_rate'])}）→ ")
    if not pooled["decided"]:
        need = []
        if len(pooled["sessions"]) < STOP_POOL_SESSIONS:
            need.append(f"{STOP_MIN_HANDS} ハンド以上のセッションが {STOP_POOL_SESSIONS} つ要る")
        if pooled["actions"] < STOP_POOL_MIN_ACTIONS:
            need.append(f"アクションの発話が合わせて {STOP_POOL_MIN_ACTIONS} 以上要る")
        return head + body + "判定しない（" + "・".join(need) + "）"
    return head + body + ("目安を満たす（音声の改善を終える）" if pooled["meets"] else "目安に届かない") + \
        f"（{STOP_READ_RATE:.0%} 以上・{STOP_CANNED_RATE:.0%} 以下）"


# ――― 第 2 の耳と Whisper の別のやり方（tools/second_ear.py, 2026-09-29）―――

# 第 2 の耳の候補を使う条件。厳しめ = 第 2 の耳が自由に聞いた文そのものが、いちばん確からしい候補と同じ
# アクションに読める（「のっぴょく」→ 耳「六百」）。ゆるめ = 候補の確からしさが自由に聞いた文より EAR_LOOSE 以上
# （崩れて聞こえた「これど」→ フォールドも入る。雑談への誤りも増える）。
EAR_LOOSE = -3.0
_ROUTE_LABELS = (
    ("ear", "第 2 の耳だけ（厳しめ）"),
    ("combo_strict", "組み合わせ（厳しめ）"),
    ("combo_loose", "組み合わせ（ゆるめ）"),
    ("combo_whisper", "組み合わせ（2 つの耳で採点）"),
    ("w_noprompt", "Whisper プロンプトなし"),
    ("w_short", "Whisper 短い窓"),
    ("w_short_noprompt", "Whisper 短い窓・プロンプトなし"),
)


@dataclass(frozen=True)
class EarBest:
    text: str        # いちばん確からしい候補（読み取りに渡す文）
    diff: float      # 候補の確からしさ − 自由に聞いた文の確からしさ
    agrees: bool     # 自由に聞いた文も、候補と同じアクションに読める（厳しめ）


def ear_best(row: dict) -> Optional[EarBest]:
    """第 2 の耳のいちばん確からしい候補。何も聞こえていなければ None（空の文と比べた差は当てにならない）。
    厳しめ（`agrees`）はライブと同じ規則（`second_ear.agreed_candidate`）。"""
    ear = row.get("ear") or {}
    cands = ear.get("candidates") or []
    if row.get("error") or not cands or ear.get("logp") is None or cands[0].get("logp") is None:
        return None
    if not (ear.get("text") or "").strip():
        return None
    return EarBest(cands[0]["text"], cands[0]["logp"] - ear["logp"], agreed_candidate(ear) is not None)


def whisper_reads(row: dict) -> bool:
    """ライブの Whisper の書き起こしを、いまの読み取りでアクションとして読めるか。"""
    text = (row.get("text") or "").strip()
    return not _is_noise(row, text) and bool(parse_actions(text))


def route_texts(transcripts: list[dict], ear_rows: list[dict], whisper_rows: list[dict]) -> dict[str, dict[str, str]]:
    """方式ごとの「音声のファイル名 → 読み直す文」（`reparse_events` の texts）。

    - 第 2 の耳だけ: 候補（厳しめ）。当てはまらなければアクションにしない。
    - 組み合わせ: Whisper が読めた発話は Whisper のまま。読めなかった発話（雑音・幻聴・読めない文）だけ第 2 の耳の候補。
    - 2 つの耳で採点: 組み合わせ（ゆるめ）の発話で、第 2 の耳の上位の候補から、2 つの耳の確からしさの和が
      いちばん大きいもの。
    - Whisper の別のやり方: その書き起こし。
    音声の無い発話（第 2 の耳で聞いていない）はどの方式でも書き起こしのまま。
    """
    live = {r.get("audio_file"): r for r in transcripts if r.get("audio_file")}
    routes: dict[str, dict[str, str]] = {}
    scores = {r.get("audio_file"): {s["text"]: s["logp"] for s in r.get("scores") or [] if s.get("logp") is not None}
              for r in whisper_rows if r.get("audio_file") and not r.get("error")}
    if ear_rows:
        only, strict, loose, both = {}, {}, {}, {}
        for row in ear_rows:
            name = row.get("audio_file")
            if not name or row.get("error"):
                continue
            best = ear_best(row)
            only[name] = best.text if best is not None and best.agrees else ""
            if best is None or whisper_reads(live.get(name) or {}):
                continue
            if best.agrees:
                strict[name] = best.text
            if best.diff >= EAR_LOOSE:
                loose[name] = best.text
                heard = scores.get(name) or {}
                joint = [(c["text"], c["logp"] + heard[c["text"]]) for c in row["ear"]["candidates"]
                         if c.get("logp") is not None and c["text"] in heard]
                if joint:
                    both[name] = max(joint, key=lambda x: x[1])[0]
        routes.update({"ear": only, "combo_strict": strict, "combo_loose": loose})
        if any(scores.values()):
            routes["combo_whisper"] = both
    for key, field_name in (("w_noprompt", "noprompt"), ("w_short", "short"), ("w_short_noprompt", "short_noprompt")):
        texts = {r["audio_file"]: (r.get(field_name) or {}).get("text") or "" for r in whisper_rows
                 if r.get("audio_file") and not r.get("error") and r.get(field_name)}
        if texts:
            routes[key] = texts
    return routes


def ear_summary(transcripts: list[dict], ear_rows: list[dict]) -> dict:
    """第 2 の耳で聞いた発話の数と、Whisper が読めなかったのに候補を聞いた発話（厳しめ / ゆるめ）。"""
    live = {r.get("audio_file"): r for r in transcripts if r.get("audio_file")}
    rescued = []
    for row in ear_rows:
        best = ear_best(row)
        name = row.get("audio_file")
        if best is None or not (best.agrees or best.diff >= EAR_LOOSE) or whisper_reads(live.get(name) or {}):
            continue
        rescued.append({"audio_file": name, "whisper": (live.get(name) or {}).get("text") or "",
                        "ear_text": (row.get("ear") or {}).get("text") or "", "candidate": best.text,
                        "diff": round(best.diff, 2), "strict": best.agrees})
    return {"heard": sum(1 for r in ear_rows if not r.get("error")), "errors": sum(1 for r in ear_rows if r.get("error")),
            "rescued_strict": sum(1 for r in rescued if r["strict"]), "rescued_loose": len(rescued), "rescued": rescued}


# ――― 比べる ―――

def _row(a: dict) -> tuple:
    return (a.get("street"), a.get("seat"), a.get("action"), a.get("amount"))


def _fmt_row(a: Optional[dict]) -> str:
    if a is None:
        return "—"
    amount = a.get("amount") or 0
    return f"{a.get('street')} 席{a.get('seat')} {a.get('action')}" + (f" {amount}" if amount else "")


def diff_record(live: list[dict], replayed: list[dict]) -> list[dict]:
    """記録と再生で違うハンド（行・勝者・ポット・ボード）。"""
    out = []
    rep_by_id = {h.get("hand_id"): h for h in replayed}
    live_ids = {h.get("hand_id") for h in live}
    for h in live:
        hid = h.get("hand_id")
        r = rep_by_id.get(hid)
        if r is None:
            out.append({"hand_id": hid, "what": "再生では記録されない"})
            continue
        la, ra = [_row(a) for a in h.get("actions") or []], [_row(a) for a in r.get("actions") or []]
        notes = []
        if la != ra:
            i = next((k for k, (x, y) in enumerate(zip(la, ra)) if x != y), min(len(la), len(ra)))
            notes.append(f"{i + 1} 行目から違う（記録 {len(la)} 行 / 再生 {len(ra)} 行）")
        for key, label in (("winner_seat", "勝者"), ("pot_total", "ポット"), ("board", "ボード")):
            if h.get(key) != r.get(key):
                notes.append(f"{label} {h.get(key)} → {r.get(key)}")
        if notes:
            out.append({"hand_id": hid, "what": "、".join(notes)})
    for r in replayed:
        if r.get("hand_id") not in live_ids:
            out.append({"hand_id": r.get("hand_id"), "what": "再生だけにある"})
    return out


def action_sources(hands: list[dict]) -> Counter:
    """記録された行の出所（音声 / 補った / RFID / その他）。"""
    return Counter(_SOURCE_GROUPS.get(a.get("actor_source") or "", a.get("actor_source") or "その他")
                   for h in hands for a in h.get("actions") or [])


def _correct(gt_action: dict, captured: dict) -> bool:
    return row_correct(gt_action, captured)


def _pct(num: int, den: int) -> Optional[float]:
    return num / den if den else None


def evaluate_against_truth(truth: dict, captured_hands: list[dict]) -> dict:
    """真のアクションとの一致率・要確認の精度と再現率・出所ごとの正誤・聞き取りの自信。"""
    captured = {"hands": captured_hands}
    acc = measure_session(captured, truth)
    cap_by_id = {h.get("hand_id"): h for h in captured_hands}
    flagged = wrong = flagged_wrong = missed = 0
    by_source: dict[str, list[int]] = {}
    conf_ok: list[float] = []
    conf_ng: list[float] = []
    for gt in truth.get("hands") or []:
        cap_actions = (cap_by_id.get(gt.get("hand_id")) or {}).get("actions") or []
        pairs, n_delete, _ = _align_actions(gt.get("actions") or [], cap_actions)
        missed += n_delete
        ok_ids = {id(c) for g, c in pairs if _correct(g, c)}
        for c in cap_actions:
            good = id(c) in ok_ids
            src = _SOURCE_GROUPS.get(c.get("actor_source") or "", c.get("actor_source") or "その他")
            by_source.setdefault(src, [0, 0])[0 if good else 1] += 1
            if c.get("needs_review"):
                flagged += 1
                flagged_wrong += 0 if good else 1
            if not good:
                wrong += 1
            conf = c.get("asr_confidence")
            if conf is not None and (c.get("source") or {}).get("audio"):
                (conf_ok if good else conf_ng).append(float(conf))
    blind_ids = {h.get("hand_id") for h in truth.get("hands") or [] if h.get("blind")}
    split = {True: [0, 0], False: [0, 0]}
    for h in acc.per_hand:
        split[h.hand_id in blind_ids][0] += h.action_correct
        split[h.hand_id in blind_ids][1] += h.action_total
    entry = [h.get("entry_sec") for h in truth.get("hands") or [] if isinstance(h.get("entry_sec"), (int, float))]
    # ハンドが丸ごと正しい数（ハンドの整合 = 目標の数字, オーナー 2026-09-30）
    exact = sum(1 for gt in truth.get("hands") or [] if hand_fully_correct(gt, cap_by_id.get(gt.get("hand_id"))))
    return {
        "hands": len(truth.get("hands") or []),
        "exact_hands": exact,
        # 記録を見ずに入れたハンドとそれ以外の一致率（大きく違えば、入れる人が記録に引きずられている）
        "blind_hands": len(blind_ids),
        "blind_accuracy": _pct(*split[True]), "seen_accuracy": _pct(*split[False]),
        "unsure_rows": sum(1 for h in truth.get("hands") or [] for a in h.get("actions") or [] if a.get("unsure")),
        "entry_sec_avg": round(sum(entry) / len(entry), 1) if entry else None,
        "action_accuracy": acc.action_accuracy, "board_accuracy": acc.board_accuracy,
        "winner_accuracy": acc.winner_seat_accuracy, "missed_hands": acc.missed_hands,
        "per_hand": [
            {"hand_id": h.hand_id, "correct": h.action_correct, "total": h.action_total,
             "winner_match": h.winner_match, "board_match": h.board_match}
            for h in acc.per_hand
        ],
        "wrong_rows": wrong, "missed_rows": missed, "flagged_rows": flagged, "flagged_wrong": flagged_wrong,
        "review_precision": _pct(flagged_wrong, flagged), "review_recall": _pct(flagged_wrong, wrong),
        "by_source": {k: {"correct": v[0], "wrong": v[1]} for k, v in sorted(by_source.items())},
        "asr_confidence": {"correct": _summary(conf_ok), "wrong": _summary(conf_ng)},
    }


def _summary(values: list[float]) -> Optional[dict]:
    if not values:
        return None
    return {"n": len(values), "min": round(min(values), 2), "median": round(statistics.median(values), 2),
            "max": round(max(values), 2)}


def truth_hands(gt_file: Optional[dict]) -> dict:
    """真のアクションのファイルを、ハンドの中身だけの形（`measure_session` の入力）にする。"""
    hands = []
    for h in (gt_file or {}).get("hands") or []:
        if isinstance(h, dict) and isinstance(h.get("hand_id"), int):
            hands.append({k: v for k, v in h.items() if k not in _GT_META})
    return {"hands": hands}


def truth_memos(gt_file: Optional[dict]) -> list[dict]:
    """真のアクションの各ハンドのメモ（気づいたこと）。オーナーの指示（2026-09-29）で**毎ハンド読む** = レポートの
    先頭に全部出す（ここに要望・仕様が書かれる）。"""
    out = []
    for h in (gt_file or {}).get("hands") or []:
        notes = h.get("notes") if isinstance(h, dict) else None
        if isinstance(notes, str) and notes.strip() and isinstance(h.get("hand_id"), int):
            out.append({"hand_id": h["hand_id"], "notes": notes.strip(), "annotated_at": h.get("annotated_at")})
    return sorted(out, key=lambda m: m["hand_id"])


# ――― セッションごとの評価 ―――

@dataclass
class SessionReport:
    session_id: str
    hands: int = 0
    note: str = ""
    same_code: Optional[bool] = None
    changed_files: list[str] = field(default_factory=list)
    differences: list[dict] = field(default_factory=list)
    sources: dict = field(default_factory=dict)
    truth: dict = field(default_factory=dict)          # "record" / "replay" → evaluate_against_truth
    setup: Optional[dict] = None
    flags: dict = field(default_factory=dict)
    live: list[dict] = field(default_factory=list)
    replayed: list[dict] = field(default_factory=list)
    reparsed: list[dict] = field(default_factory=list)   # 書き起こしをいまの読み取りで読み直した再生
    rescored: list[dict] = field(default_factory=list)   # 採点し直した文で読み直した再生
    listening: dict = field(default_factory=dict)        # 採点し直して書き起こしと違った発話
    routes: dict = field(default_factory=dict)           # 方式（_ROUTE_LABELS）→ その文で読み直した再生
    ear: dict = field(default_factory=dict)              # 第 2 の耳（ear_summary）
    gt: dict = field(default_factory=dict)
    memos: list[dict] = field(default_factory=list)      # 真のアクションのメモ（truth_memos）
    script: dict = field(default_factory=dict)           # 台本のハンド（tools/test_script.py）を真のアクションにした
    stop: dict = field(default_factory=dict)             # 音声の改善を終える目安（listening_stop_metrics）

    def to_json(self) -> dict:
        return {
            "session_id": self.session_id, "hands": self.hands, "note": self.note, "same_code": self.same_code,
            "changed_files": self.changed_files, "differences": self.differences, "sources": self.sources,
            "truth": self.truth, "setup": self.setup, "flags": self.flags, "listening": self.listening,
            "stop": self.stop,
            "reparse_differences": diff_record(self.replayed, self.reparsed) if self.reparsed else [],
            "ear": self.ear, "memos": self.memos, "script": self.script,
            "route_differences": {k: diff_record(self.reparsed, v) for k, v in self.routes.items()},
        }


def evaluate_session(files: SessionFiles, config: dict, recorded_code: Optional[dict]) -> SessionReport:
    report = SessionReport(files.session_id)
    record = _read_json(files.path(".json")) or {}
    report.live = [h for h in record.get("hands") or [] if isinstance(h, dict)]
    report.hands = len(report.live)
    if recorded_code:
        now = code_fingerprint(ROOT)
        report.changed_files = sorted(k for k, v in recorded_code.items() if now.get(k) != v)
        report.same_code = not report.changed_files
    report.setup = session_setup(record)
    report.sources = dict(action_sources(report.live))
    gt_file = _read_json(files.path(".ground_truth.json"))
    report.gt = truth_hands(gt_file)
    report.memos = truth_memos(gt_file)
    script = _read_json(files.path(SCRIPT_SUFFIX))
    events = load_events(files.events) if files.events.exists() else []
    if script is not None:
        # 台本のハンド: 正解は台本（真のアクションの入力が無くても評価できる）。札の確認の手順のメモも読む
        marks = _read_jsonl(files.path(MARKS_SUFFIX))
        began = [e.timestamp for e in events if isinstance(e, AudioEvent) and e.action == "script_hand"]
        st = script_truth(script, marks, report.live, hand_starts=began or None,
                          utc_offset=record_utc_offset(report.live, events))
        report.script = {"kind": script.get("kind"), "hands": len(script.get("hands") or []),
                         "used": st["script_hands"], "redone": st["redone"], "unmatched": st["unmatched"],
                         "steps": script_steps(script, marks)}
        transcripts = _read_jsonl(files.path(".transcripts.jsonl"))
        if script.get("kind") == "voice" and marks and transcripts:
            ordered = script_order_events(script, marks, transcripts)
            hands = replay_session(ordered, script_setup(script),
                                   {"auto_new_hand": False, "auto_winner": True, "rfid_folds": False},
                                   files.session_id)
            report.script["ordered"] = score_script_order(script, marks, hands)
        if st["hands"] and not report.gt["hands"]:
            report.gt = {"hands": st["hands"]}
    if report.setup is None:
        report.note = "確定したハンドの記録が無いので卓の設定が分からず、再生できません"
        return report
    report.flags = replay_flags(config, events, script)
    report.replayed = replay_session(events, report.setup, report.flags, files.session_id)
    report.differences = diff_record(report.live, report.replayed)
    if report.differences and not any(isinstance(e, RFIDEvent) and e.kind == "deal" for e in events):
        report.note = ("配布の信号（schema 0.9）が無い記録です。在否で決めた配布は再生で違うことがあります"
                       "（起動時に卓に残っていた札・配る前の発話の待ち）")
    transcripts = _read_jsonl(files.path(".transcripts.jsonl"))
    if transcripts:
        report.reparsed = replay_session(
            reparse_events(events, transcripts), report.setup, report.flags, files.session_id)
    rescored = _read_jsonl(files.path(".rescored.jsonl"))
    if transcripts and rescored:
        texts = {r["audio_file"]: r["best"] for r in rescored
                 if r.get("audio_file") and r.get("best") and not r.get("error")}
        report.rescored = replay_session(
            reparse_events(events, transcripts, texts), report.setup, report.flags, files.session_id)
        report.listening = listening_summary(transcripts, rescored)
    ear_rows = _read_jsonl(files.path(".ear.jsonl"))
    whisper_rows = _read_jsonl(files.path(".whisper.jsonl"))
    if transcripts and (ear_rows or whisper_rows):
        for key, texts in route_texts(transcripts, ear_rows, whisper_rows).items():
            report.routes[key] = replay_session(
                reparse_events(events, transcripts, texts), report.setup, report.flags, files.session_id)
        if ear_rows:
            report.ear = ear_summary(transcripts, ear_rows)
    if report.gt["hands"]:
        report.truth = {
            "record": evaluate_against_truth(report.gt, report.live),
            "replay": evaluate_against_truth(report.gt, report.replayed),
        }
        if report.reparsed:
            report.truth["reparse"] = evaluate_against_truth(report.gt, report.reparsed)
        if report.rescored:
            report.truth["rescored"] = evaluate_against_truth(report.gt, report.rescored)
        for key, hands in report.routes.items():
            report.truth[key] = evaluate_against_truth(report.gt, hands)
        if transcripts and script is not None:
            # 台本のセッション: ハンドの区切りはライブの記録ではなく台本の画面を押した時刻（ライブはハンドの始まりが
            # 遅れることがある）
            marks = _read_jsonl(files.path(MARKS_SUFFIX))
            report.stop = listening_stop_metrics(
                {"hands": [{"hand_id": int(h["n"]), "actions": h.get("actions") or []}
                           for h in script.get("hands") or []]},
                [], transcripts, windows=script_windows(script, marks))
        elif transcripts:
            report.stop = listening_stop_metrics(report.gt, report.reparsed or report.replayed, transcripts)
    return report


def record_utc_offset(hands: list[dict], events: list) -> Optional[float]:
    """記録（`<sid>.json`）の時刻を書いた PC の時差（秒, 15 分単位）。無ければ None。

    記録の時刻は時差の表記の無い、その PC の時計の時刻（`IntegrationThread._iso`）。同じ発話の入力（`events.jsonl`,
    epoch）と並べて差を取る（店の記録を別の時差の PC で評価すると 9 時間ずれていた, 2026-09-30）。
    """
    spoken: dict[str, list[float]] = {}
    for e in events:
        if isinstance(e, AudioEvent) and e.raw_text:
            spoken.setdefault(e.raw_text, []).append(e.timestamp)
    diffs: Counter = Counter()
    for hand in hands:
        for a in hand.get("actions") or []:
            try:
                naive = datetime.fromisoformat(str(a.get("timestamp")))
            except ValueError:
                continue
            if naive.tzinfo is not None:
                continue
            clock = naive.replace(tzinfo=timezone.utc).timestamp()
            for t in spoken.get(a.get("raw_text") or "", ()):
                diff = round((clock - t) / 900.0) * 900.0
                if abs(clock - t - diff) < 1.0:
                    diffs[diff] += 1
    return diffs.most_common(1)[0][0] if diffs else None


# ――― タイムライン ―――

def _epoch(iso: Optional[str]) -> Optional[float]:
    """このプロセスで作った ISO 時刻（`datetime.fromtimestamp(...).isoformat()`）を epoch に戻す。"""
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return None


def hand_windows(hands: list[dict]) -> dict[int, tuple[float, float]]:
    """再生したハンドの時間帯（始まり = 配布、終わり = 次のハンドの始まり）。"""
    out: dict[int, tuple[float, float]] = {}
    starts = [(_epoch(h.get("started_at")), h) for h in hands]
    for i, (start, h) in enumerate(starts):
        if start is None:
            continue
        nxt = next((s for s, _ in starts[i + 1:] if s is not None), None)
        end = nxt if nxt is not None else (_epoch(h.get("ended_at")) or start) + 30.0
        out[h.get("hand_id")] = (start, end)
    return out


_VARIANT_NAMES = (("noprompt", "なし"), ("short", "短"), ("short_noprompt", "短なし"))


def _ear_note(row: dict) -> str:
    """タイムラインの第 2 の耳: 自由に聞いた文 → いちばん確からしい候補（自由に聞いた文との確からしさの差）。"""
    if row.get("error"):
        return " ／ 耳: 失敗"
    ear = row.get("ear") or {}
    cands = ear.get("candidates") or []
    best = ear_best(row)
    top = f" → {cands[0].get('text')}" if cands else ""
    diff = f"（差 {best.diff:+.1f}{'・読める' if best.agrees else ''}）" if best is not None else ""
    return f" ／ 耳「{ear.get('text') or ''}」{top}{diff}"


def _variant_note(row: dict, live: dict) -> str:
    """タイムラインの Whisper の別のやり方（ライブの書き起こしと違ったものだけ。なし = プロンプトなし・短 = 短い窓）。"""
    if row.get("error"):
        return " ／ W: 失敗"
    heard = (live.get("text") or "").strip()
    parts = [f"{label}「{(row[key].get('text') or '').strip()}」" for key, label in _VARIANT_NAMES
             if isinstance(row.get(key), dict) and (row[key].get("text") or "").strip() != heard]
    return (" ／ W " + " ".join(parts)) if parts else ""


def timeline(files: SessionFiles, window: tuple[float, float], tz: Optional[timezone] = None) -> list[str]:
    """ハンドの時間帯の発話・RFID の札と信号・入力を時刻順に（相対秒 + 時計）。"""
    start, end = window
    rows: list[tuple[float, str]] = []
    heard = set()
    best = {r.get("audio_file"): r.get("best") for r in _read_jsonl(files.path(".rescored.jsonl"))
            if r.get("audio_file") and r.get("best")}
    ears = {r.get("audio_file"): r for r in _read_jsonl(files.path(".ear.jsonl")) if r.get("audio_file")}
    variants = {r.get("audio_file"): r for r in _read_jsonl(files.path(".whisper.jsonl")) if r.get("audio_file")}
    for x in _read_jsonl(files.path(".transcripts.jsonl")):
        t = x.get("utterance_start_ts")
        if t is None or not start - 5.0 <= t < end:
            continue
        heard.add(round(t, 2))
        if x.get("no_speech"):
            continue
        tag = "雑音" if x.get("noise") else ("疑問" if x.get("question") else "声")
        parsed = ", ".join(
            f"{e.get('action')}" + (f" {e['amount']}" if e.get("amount") else "") + (f" 席{e['seat']}" if e.get("seat") else "")
            for e in x.get("events") or []) or "（読まない）"
        lag = (x.get("heard_at") or t) - t
        line = f"{tag} 「{x.get('text')}」 自信 {x.get('confidence') or 0:.2f}・認識 +{lag:.1f}s → {parsed}"
        alt = best.get(x.get("audio_file"))
        if alt and alt.strip() != (x.get("text") or "").strip():
            line += f" ／ 採点: 「{alt}」→ {_actions_text(alt, x)}"
        ear_row = x if x.get("ear") else ears.get(x.get("audio_file"))     # ライブで聞き直した / あとで聞き直した
        if ear_row is not None:
            line += _ear_note(ear_row)
        if x.get("audio_file") in variants:
            line += _variant_note(variants[x["audio_file"]], x)
        rows.append((t, line))
    for ev in load_events(files.events):
        t = ev.timestamp
        if not start - 5.0 <= t < end:
            continue
        if isinstance(ev, RFIDEvent):
            if ev.kind == "card":
                where = f"席{ev.seat}" if ev.role == "seat" else f"ボード {ev.board_index or '?'} 枚目"
                rep = f"（{ev.replaces} を差し替え）" if ev.replaces else ""
                rows.append((t, f"札 {ev.card or '?'} → {where} [{ev.reader_id}]{rep}"))
            else:
                what = _SIGNAL_NAMES.get(ev.kind, ev.kind)
                target = f"席{ev.seat}" if ev.seat is not None else f"ボード {ev.board_index} 枚"
                obs = f"（{ev.observed_at - start:+.1f}s に見えた）" if ev.observed_at is not None else ""
                rows.append((t, f"信号 {what} {target}{obs}"))
        elif isinstance(ev, AudioEvent):
            if ev.utterance_start_ts is not None and round(ev.utterance_start_ts, 2) in heard:
                continue                  # 発話の行に出ている
            rows.append((t, f"入力 {ev.action}" + (f" {ev.amount}" if ev.amount else "")
                         + (f" 席{ev.seat}" if ev.seat else "") + (f" 「{ev.raw_text}」" if ev.raw_text else "")))
    rows.sort(key=lambda r: r[0])
    return [f"  {t - start:+7.1f}s {datetime.fromtimestamp(t, tz).strftime('%H:%M:%S')}  {text}" for t, text in rows]


def compare_rows(gt_hand: Optional[dict], live_hand: Optional[dict], rep_hand: Optional[dict]) -> list[str]:
    """真のアクション（あれば）に記録・再生の行を並べる。✓ = 一致 / ✗ = 違う / — = 無い。"""
    lines = []
    live_actions = (live_hand or {}).get("actions") or []
    rep_actions = (rep_hand or {}).get("actions") or []
    if gt_hand is not None:
        gt_actions = gt_hand.get("actions") or []
        pair_maps = []
        for cap in (live_actions, rep_actions):
            pairs, _, _ = _align_actions(gt_actions, cap)
            pair_maps.append({id(g): c for g, c in pairs})
        lines.append(f"  {'真のアクション':<24}{'記録（店舗）':<30}再生（いまのコード）")
        for g in gt_actions:
            cells = []
            for pm in pair_maps:
                c = pm.get(id(g))
                mark = "—" if c is None else ("✓" if _correct(g, c) else "✗")
                review = " 要" if c is not None and c.get("needs_review") else ""
                cells.append(f"{mark} {_fmt_row(c)}{review}")
            lines.append(f"  {_fmt_row(g):<24}{cells[0]:<30}{cells[1]}")
        for label, cap, pm in (("記録", live_actions, pair_maps[0]), ("再生", rep_actions, pair_maps[1])):
            paired = {id(c) for c in pm.values()}
            extra = [_fmt_row(c) for c in cap if id(c) not in paired]
            if extra:
                lines.append(f"  {label}だけにある行: " + " / ".join(extra))
        return lines
    lines.append(f"  {'記録（店舗）':<34}再生（いまのコード）")
    for i in range(max(len(live_actions), len(rep_actions))):
        a = live_actions[i] if i < len(live_actions) else None
        b = rep_actions[i] if i < len(rep_actions) else None
        mark = " " if a is not None and b is not None and _row(a) == _row(b) else "≠"
        left = _fmt_row(a) + (" 要" if a and a.get("needs_review") else "")
        right = _fmt_row(b) + (" 要" if b and b.get("needs_review") else "")
        lines.append(f"  {mark} {left:<32}{right}")
    return lines


# ――― 回帰テスト用の書き出し ―――

# fixture に残す書き起こしの項目（いまの読み取りで読み直して再生するのに要るものだけ）
_FIXTURE_TRANSCRIPT_KEYS = ("utterance_start_ts", "heard_at", "text", "audio_sec", "confidence", "no_speech", "ear")


def _baseline(truth: dict, hand: Optional[dict]) -> dict:
    m = measure_hand(truth, hand)
    return {"action_correct": m.action_correct, "action_total": m.action_total,
            "winner_match": m.winner_match, "board_match": m.board_match}


def _keep_higher(new: dict, prev: Optional[dict]) -> dict:
    return prev if prev and prev.get("action_correct", 0) > new["action_correct"] else new


def export_fixture(files: SessionFiles, report: SessionReport, out_root: Path) -> Optional[Path]:
    """真のアクションのあるセッションを `out_root/<日付>-<sid 8 桁>/` に書き出す（events.jsonl + expected.json、
    書き起こしがあれば transcripts.jsonl）。

    `baseline` は書き出したときの再生と真のアクションの一致、`reparse_baseline` は書き起こしをそのときの読み取りで
    読み直した再生の一致（読み取りの変更で悪くなったことに気づくため。店舗 2026-09-29:「オーリー」をオールインと
    読むようにしたら、ディーラーの言い直しで 2 ハンドが悪くなったのを、記録の再生だけでは見られなかった）。
    真のアクションが同じで、前に書き出した値より低ければ前の値を残す（悪くなったことを黙って受け入れない）。
    """
    if not report.gt.get("hands") or report.setup is None:
        return None
    date = str((report.live[0].get("started_at") or "")[:10]) or "unknown"
    folder = out_root / f"{date}-{files.session_id[:8]}"
    old = _read_json(folder / "expected.json") if (folder / "expected.json").exists() else None
    if old is not None and "setup" not in old:
        raise SystemExit(f"{folder} は別の形式の fixture です（上書きしません）")
    old_hands = {h["hand_id"]: h for h in (old or {}).get("hands") or []}
    rep_by_id = {h.get("hand_id"): h for h in report.replayed}
    reparsed_by_id = {h.get("hand_id"): h for h in report.reparsed} if report.reparsed else None
    hands = []
    for truth in report.gt["hands"]:
        prev = old_hands.get(truth["hand_id"]) or {}
        if prev.get("truth") != truth:
            prev = {}        # 真のアクションを直した（オーナーの確認など）= 前の baseline とは比べられない
        hand = {"hand_id": truth["hand_id"], "truth": truth,
                "baseline": _keep_higher(_baseline(truth, rep_by_id.get(truth["hand_id"])), prev.get("baseline"))}
        if reparsed_by_id is not None:
            hand["reparse_baseline"] = _keep_higher(
                _baseline(truth, reparsed_by_id.get(truth["hand_id"])), prev.get("reparse_baseline"))
        hands.append(hand)
    folder.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(files.events, folder / "events.jsonl")
    transcripts = _read_jsonl(files.path(".transcripts.jsonl"))
    if transcripts and reparsed_by_id is not None:
        rows = [{k: r[k] for k in _FIXTURE_TRANSCRIPT_KEYS if k in r} for r in transcripts]
        (folder / "transcripts.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    table_state = _read_jsonl(files.path(".table_state.jsonl"))
    if table_state:
        # 席の札の在否の変化だけ（推定器の再生の入力, `integration/world_replay.py`）
        presence = PresenceTimeline.from_table_state(table_state).to_rows()
        (folder / "presence.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in presence), encoding="utf-8")
    expected = {
        "session_id": files.session_id, "setup": {**report.setup, **report.flags},
        "code": code_fingerprint(ROOT), "hands": hands,
    }
    (folder / "expected.json").write_text(json.dumps(expected, ensure_ascii=False, indent=1), encoding="utf-8")
    return folder


def check_fixture(folder: Path) -> list[str]:
    """書き出した fixture を再生し、真のアクションとの一致が baseline より悪くなった点を返す（空 = 問題なし）。

    書き起こし（transcripts.jsonl）があれば、いまの読み取りで読み直した再生も `reparse_baseline` と比べる。
    """
    expected = _read_json(folder / "expected.json") or {}
    setup = expected.get("setup") or {}
    flags = {k: bool(setup.get(k)) for k in ("auto_new_hand", "auto_winner", "rfid_folds")}
    session_id = expected.get("session_id") or folder.name
    runs = [("baseline", "", replay_session(folder / "events.jsonl", setup, flags, session_id))]
    transcripts = _read_jsonl(folder / "transcripts.jsonl") if (folder / "transcripts.jsonl").exists() else []
    if transcripts:
        events = reparse_events(load_events(folder / "events.jsonl"), transcripts)
        runs.append(("reparse_baseline", "（書き起こしの読み直し）",
                     replay_session(events, setup, flags, session_id)))
    problems = []
    for key, label_run, replayed in runs:
        rep_by_id = {h.get("hand_id"): h for h in replayed}
        for hand in expected.get("hands") or []:
            base = hand.get(key)
            if base is None:
                continue
            m = measure_hand(hand["truth"], rep_by_id.get(hand["hand_id"]))
            where = f"{folder.name} ハンド {hand['hand_id']}{label_run}"
            if m.action_correct < base["action_correct"]:
                problems.append(f"{where}: 一致する行が {base['action_correct']} → {m.action_correct}"
                                f"（{m.action_total} 行中）")
            for attr, label in (("winner_match", "勝者"), ("board_match", "ボード")):
                if base.get(attr) and not getattr(m, attr):
                    problems.append(f"{where}: {label}が真のアクションと合わなくなった")
    return problems


# ――― 表示 ―――

def _fmt_pct(value: Optional[float]) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"


_TRUTH_LABELS = (
    ("record", "記録"), ("replay", "再生"), ("reparse", "読み直し"), ("rescored", "採点し直した文"),
)


def session_started(report: SessionReport) -> float:
    """セッションの始まり（いちばん早いハンドの始まり。無ければ 0）。音声の改善を終える目安の「直近」の並び。"""
    times = [_epoch(h.get("started_at")) for h in [*report.live, *report.replayed] if isinstance(h, dict)]
    return min((t for t in times if t is not None), default=0.0)


def print_report(reports: list[SessionReport], show_timeline: bool, only_hand: Optional[int],
                 tz: Optional[timezone], files_by_sid: dict[str, SessionFiles]) -> None:
    labels = _TRUTH_LABELS + _ROUTE_LABELS
    totals: dict[str, Counter] = {key: Counter() for key, _ in labels}
    for r in reports:
        print(f"=== セッション {r.session_id[:8]}（{r.hands} ハンド）")
        if r.memos:                                   # 要望・仕様が書かれる。毎ハンド読む（オーナー, 2026-09-29）
            print(f"  メモ（気づいたこと）{len(r.memos)} 件:")
            for m in r.memos:
                print(f"    ハンド {m['hand_id']}: {m['notes']}")
        if r.script:
            sc = r.script
            if sc["kind"] == "voice":
                print(f"  台本のハンド（声だけ）: 台本 {sc['hands']} ハンドのうち {len(sc['used'])} を評価"
                      f"（やり直し {sc['redone']}" + (f"・記録が見つからない {sc['unmatched']}" if sc["unmatched"] else "")
                      + "）")
            od = sc.get("ordered")
            if od:
                print(f"  台本の画面を押した順に並べ直して再生すると: 行 {od['rows']}/{od['total']}"
                      f"（{od['rows'] / max(od['total'], 1):.0%}）・勝者 {od['winners']}/{od['hands']}"
                      f"・全部正しいハンド {od['exact']}/{od['hands']}")
                for miss in od["missed"]:
                    print(f"    台本のハンド {miss['hand']}（{miss['scenario']}）: 行 {miss['rows']}/{miss['total']}"
                          f"・勝者 {'○' if miss['winner'] else '×'}")
            for step in sc.get("steps") or []:
                mark = {"ok": "✓", "ng": "✗"}.get(step["result"], "・")
                print(f"  札の確認 {mark} {step['title']}" + (f" — メモ: {step['note']}" if step["note"] else ""))
        if r.same_code is not None:
            print("  コード: " + ("記録したときと同じ" if r.same_code
                                else "記録したときと違う（" + "・".join(r.changed_files) + "）"))
        if r.note and r.setup is None:
            print(f"  {r.note}")
            continue
        if r.sources:
            print("  記録された行の出所: " + " / ".join(f"{k} {v}" for k, v in sorted(r.sources.items())))
        if r.differences:
            print(f"  再生すると記録と違うハンド: {len(r.differences)}")
            for d in r.differences:
                print(f"    ハンド {d['hand_id']}: {d['what']}")
            if r.note:
                print(f"    （{r.note}）")
        else:
            print("  再生の結果は記録と同じ")
        if r.reparsed:
            reparse_diff = diff_record(r.replayed, r.reparsed)
            print("  書き起こしをいまの読み取りで読み直すと: "
                  + (f"{len(reparse_diff)} ハンドが変わる" if reparse_diff else "再生と同じ"))
            for d in reparse_diff:
                print(f"    ハンド {d['hand_id']}: {d['what']}")
        if r.listening:
            lis = r.listening
            print(f"  音声の採点: {lis['rescored']} 発話（失敗 {lis['errors']}）・書き起こしと違う文 {lis['changed_text']}"
                  f"・読んだアクションが違う {lis['changed_actions']}")
            for c in lis["changed"]:
                if c["actions_changed"]:
                    margin = "" if c["margin"] is None else f"（確からしさ +{c['margin']}）"
                    print(f"    {c['audio_file']} 「{c['heard']}」{c['before']} → 「{c['best']}」{c['after']}{margin}")
        if r.ear:
            e = r.ear
            print(f"  第 2 の耳: {e['heard']} 発話（失敗 {e['errors']}）・Whisper が読めなかったのに候補を聞いた発話 "
                  f"{e['rescued_loose']}（うち厳しめ {e['rescued_strict']}）")
            for c in e["rescued"]:
                print(f"    {c['audio_file']}  Whisper「{c['whisper'][:20]}」 耳「{c['ear_text']}」→ {c['candidate']}"
                      f"（差 {c['diff']:+.1f}{'・厳しめ' if c['strict'] else ''}）")
        if r.routes and r.reparsed:
            changed = [f"{label} {len(diff_record(r.reparsed, r.routes[key]))}" for key, label in _ROUTE_LABELS
                       if key in r.routes]
            print("  方式ごとに読み直すと変わるハンド（書き起こしの読み直しと比べて）: " + " / ".join(changed))
        if r.truth:
            print(f"  真のアクション: {r.truth['record']['hands']} ハンド")
            for key, label in labels:
                if key not in r.truth:
                    continue
                t = r.truth[key]
                print(f"    {label}: 全部正しいハンド {t['exact_hands']}/{t['hands']}"
                      f"・一致率 {_fmt_pct(t['action_accuracy'])}・ボード {_fmt_pct(t['board_accuracy'])}"
                      f"・勝者 {_fmt_pct(t['winner_accuracy'])} ／ 誤った行 {t['wrong_rows']}・取りこぼし "
                      f"{t['missed_rows']} ／ 要確認 {t['flagged_rows']} 行（うち誤り {t['flagged_wrong']}）"
                      f" = 精度 {_fmt_pct(t['review_precision'])}・再現率 {_fmt_pct(t['review_recall'])}")
                for ph in t["per_hand"]:
                    totals[key]["correct"] += ph["correct"]
                    totals[key]["total"] += ph["total"]
                totals[key]["exact"] += t["exact_hands"]
                totals[key]["hands"] += t["hands"]
                totals[key]["flagged"] += t["flagged_rows"]
                totals[key]["flagged_wrong"] += t["flagged_wrong"]
                totals[key]["wrong"] += t["wrong_rows"]
            rec = r.truth["record"]
            extra = []
            if rec["blind_hands"]:
                extra.append(f"ブラインド {rec['blind_hands']} ハンドの一致率 {_fmt_pct(rec['blind_accuracy'])}"
                             f"（ほか {_fmt_pct(rec['seen_accuracy'])}）")
            if rec["unsure_rows"]:
                extra.append(f"自信なしの行 {rec['unsure_rows']}")
            if rec["entry_sec_avg"] is not None:
                extra.append(f"入力の時間 平均 {rec['entry_sec_avg']:.0f} 秒")
            if extra:
                print("    " + " ／ ".join(extra))
            conf = r.truth["record"]["asr_confidence"]
            if conf["correct"] or conf["wrong"]:
                print(f"    聞き取りの自信（記録の音声の行）: 正しい {conf['correct']} / 誤り {conf['wrong']}")
            st = r.stop
            if st and st["actions"]:
                # 1 セッションの値は参考（判定は最後の合算の行 = `pooled_stop_metrics`）
                print(f"  音声の改善を終える目安（このセッション, 参考）: アクションの発話 {st['read']}/{st['actions']}"
                      f"（{_fmt_pct(st['read_rate'])}）が正しく読めた・決まり文句 {st['canned']}/{st['utterances']}"
                      f"（{_fmt_pct(st['canned_rate'])}）。{st['hands']} ハンド"
                      + ("" if st["hands"] >= STOP_MIN_HANDS else f"（{STOP_MIN_HANDS} ハンド未満なので合算に入れない）"))
        else:
            print("  真のアクション: なし")
        if show_timeline:
            files = files_by_sid[r.session_id]
            windows = hand_windows(r.replayed)
            live_by_id = {h.get("hand_id"): h for h in r.live}
            rep_by_id = {h.get("hand_id"): h for h in r.replayed}
            gt_by_id = {h.get("hand_id"): h for h in r.gt.get("hands") or []}
            for hid in sorted(set(live_by_id) | set(rep_by_id)):
                if only_hand is not None and hid != only_hand:
                    continue
                print(f"  --- ハンド {hid}")
                for m in r.memos:
                    if m["hand_id"] == hid:
                        print(f"    メモ: {m['notes']}")
                if hid in windows:
                    for line in timeline(files, windows[hid], tz):
                        print(line)
                for line in compare_rows(gt_by_id.get(hid), live_by_id.get(hid), rep_by_id.get(hid)):
                    print(line)
    pooled = pooled_stop_metrics([(r.session_id, session_started(r), r.stop) for r in reports if r.stop])
    if pooled is not None:
        print(format_pooled_stop(pooled))
    for key, label in labels:
        c = totals[key]
        if c["total"]:
            print(f"合計（真のアクションのあるハンド）{label}: 全部正しいハンド {c['exact']}/{c['hands']}"
                  f"・一致 {c['correct']}/{c['total']}"
                  f"（{_fmt_pct(c['correct'] / c['total'])}）・要確認の精度 {_fmt_pct(_pct(c['flagged_wrong'], c['flagged']))}"
                  f"・再現率 {_fmt_pct(_pct(c['flagged_wrong'], c['wrong']))}")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="店舗のログをいまのコードで再生して評価する（ADR-0056 追記 1, S0）")
    ap.add_argument("path", type=Path, nargs="+",
                    help="pack_logs の zip（分けた zip は全部並べる）/ 展開したフォルダ / logs")
    ap.add_argument("--session", action="append", help="セッション ID（先頭の数文字でよい。複数回可）")
    ap.add_argument("--timeline", action="store_true", help="ハンドごとのタイムラインと行の比較を出す")
    ap.add_argument("--hand", type=int, help="--timeline で出すハンド")
    ap.add_argument("--utc-offset", type=float, help="時計の表示の時差（例: 9）。省略時はこの PC の時刻")
    ap.add_argument("--export-fixture", type=Path, help="真のアクションのあるセッションを回帰テスト用に書き出す")
    ap.add_argument("--json", action="store_true", help="結果を JSON で出す")
    ap.add_argument("--verbose", action="store_true", help="再生中のエンジンのログも出す")
    args = ap.parse_args(argv)
    if not args.verbose:
        logging.disable(logging.WARNING)

    root, cleanup = open_input(args.path)
    try:
        config = find_config(root)
        manifest = _read_json(root / "manifest.json" if (root / "manifest.json").exists() else None) or {}
        sessions = find_sessions(root, args.session)
        if not sessions:
            print(f"{' '.join(map(str, args.path))} にセッション（*.events.jsonl）がありません", file=sys.stderr)
            return 1
        reports = [evaluate_session(f, config, manifest.get("code_fingerprint")) for f in sessions]
        if args.export_fixture:
            for f, r in zip(sessions, reports):
                folder = export_fixture(f, r, args.export_fixture)
                if folder is not None:
                    print(f"書き出しました: {folder}", file=sys.stderr)
        if args.json:
            print(json.dumps([r.to_json() for r in reports], ensure_ascii=False, indent=1))
        else:
            tz = timezone(timedelta(hours=args.utc_offset)) if args.utc_offset is not None else None
            print_report(reports, args.timeline, args.hand, tz, {f.session_id: f for f in sessions})
    finally:
        if cleanup is not None:
            shutil.rmtree(cleanup, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
