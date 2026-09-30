#!/usr/bin/env python3
"""tools/estimate.py — 推定器 v0（ADR-0056 追記 1 の S3, テスト方針 週 1, 2026-09-30）

記録した入力（events.jsonl・書き起こし・第 2 の耳の候補）から、ハンドごとに**いちばん筋の通るアクションの列**と
次点を探す。影で回す（記録も画面も変えない）。開発側が店舗の zip / fixture に使い、ライブ（記録）・いまの読み直し
と比べる。

形（ADR-0056 追記 1 の 1.）: 別の実装を作らず、ライブと同じエンジンを**決定的な再生器**として使う。発話ごとの読みを
**選択点**にする:

- Whisper の書き起こしをいまの読み取りで読んだもの（読めれば既定）
- 第 2 の耳の候補（閉じた語彙: アクション・額。音の確からしさつき）
- 雑談・言い直しとして**捨てる**

選択の組み合わせを山登り（1 つずつ変えて、いちばん良くなる変更を採る）で探す。採点 = 読みの確からしさの和 +
ハンドとしての筋の通り方の罰（エンジンが補ったアクション・反映できなかった発話・額を合法な額に寄せた・言われた
アクションを別のアクションに読み替えた・ボードとストリートの食い違い・勝者を推し量った など）。数値は v0 の
手置き（`PARAMS`、較正は真のアクションが増えてから）。

ハンドは記録の持ち点（`hand_stacks`）から始まるので互いに独立 → 1 回の再生で全部のハンドの候補を同時に採点する。

使い方:

    python tools/estimate.py pokerlogs_20261001_1of2.zip pokerlogs_20261001_2of2.zip   # 店舗の zip（真のアクションと比べる）
    python tools/estimate.py --fixtures                                               # 回帰テストの店舗 fixture
    python tools/estimate.py logs --session d0f055fb --write                          # <sid>.estimate.json を書く
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from audio.recognizer import is_prompt_echo, is_question, parse_actions  # noqa: E402
from audio.second_ear import apply_ear  # noqa: E402
from integration.replay import load_events  # noqa: E402
from tools.eval_store import (  # noqa: E402
    _epoch,
    _is_noise,
    _read_json,
    _read_jsonl,
    find_config,
    find_sessions,
    open_input,
    replay_flags,
    replay_session,
    reparse_events,
    session_setup,
    truth_hands,
)
from tools.measure_capture_accuracy import measure_hand, row_correct  # noqa: E402

logger = logging.getLogger(__name__)

ESTIMATOR_VERSION = "0.1"
ESTIMATE_SUFFIX = ".estimate.json"
FIXTURES = ROOT / "tests" / "fixtures" / "store"

# v0 の手置きの数値（log の尺度。較正は真のアクションが 40 ハンドを超えてから, ADR-0056 追記 1）
PARAMS: dict[str, float] = {
    # 読みの確からしさ
    "fuzzy": -1.0,              # 音の近さで読んだ・額があいまいな Whisper の読み
    "rescue": -0.5,             # Whisper で読めず、第 2 の耳の候補を自由に聞いた文と合うので使った（ライブの規則）
    "ear_base": -2.0,           # 第 2 の耳の候補を（ライブの規則に当てはまらないのに）使う
    "ear_diff_weight": 0.4,     # 候補の確からしさ − 自由に聞いた文の確からしさ（0 以下）にかける重み
    "ear_min_diff": -8.0,       # これより確からしさの低い候補は選択肢にしない（雑談に候補を当てはめない）
    "ear_top": 3,               # 候補の上から何個を選択肢にするか
    "drop_read": -4.0,          # Whisper で読めた発話を雑談・言い直しとして捨てる（長い文 = 雑談が混ざりうる）
    "drop_short": -7.0,         # アクションの言葉だけの短い発話（`short_chars` 文字以下）を捨てる
    "drop_heard": -4.0,         # Whisper が定型の幻聴を書いたが、第 2 の耳は何かを聞いた発話を捨てる（音はあった）
    "short_chars": 15,
    # ハンドとしての筋の通り方
    "implied": -1.5,            # エンジンが補ったチェック・コール（言われていない）
    "silent_fold": -2.0,        # エンジンが補ったフォールド
    "unresolved": -3.0,         # 反映できなかった発話（ハンドの外・ベッティングのあと・勝者の宣言のずれ）
    "snapped": -2.0,            # 聞いた額を合法な額に寄せた・額の無いベット
    "rounded": -0.5,            # 額を BB の単位に丸めた
    "projected": -2.5,          # 言われたアクションを別のアクションに読み替えた（チェック → コール など）
    "estimated_winner": -2.0,   # 勝者を推し量った（次の配布で決めた）
    "missing_street": -5.0,     # ボードは先まで配ったのに、アクションがその前のストリートで全員降りて終わった
    "extra_street": 0.0,        # ボードに無いストリートのアクション（札が無いのは読めていないだけのことがある = 使わない）
    "mucked_stronger": -2.0,    # ショーダウンで一番強い手が見せずに降りた
    # 探し方
    "max_rounds": 4,            # 山登りの回数（1 回 = 1 つの選択点を変える）
    "nbest": 5,                 # 残す候補の数
    "max_points": 10,           # 1 ハンドの選択点の上限（多ければ既定との差が小さい順）
    "after_hand_sec": 20.0,     # ハンドの終わりからこの秒数までの発話をそのハンドの選択点にする
}

# 選択肢にするアクション（ハンドの区切りを変える制御は選択肢にしない = ハンドどうしを独立に保つ）
_BETTING = frozenset({"fold", "check", "call", "bet", "raise", "allin", "check_around", "heads_up", "players_left",
                      "showdown", "end_hand"})
_STREETS = ("preflop", "flop", "turn", "river")
_BOARD_STREET = {3: "flop", 4: "turn", 5: "river"}


def params_hash(params: dict[str, float] = PARAMS) -> str:
    return hashlib.sha1(json.dumps(params, sort_keys=True).encode("utf-8")).hexdigest()[:12]


# ───────────────────────── 選択点 ─────────────────────────


@dataclass(frozen=True)
class Option:
    source: str        # "whisper" | "rescue" | "ear" | "drop"
    text: str          # 読み直す文（drop は空）
    logp: float        # 読みの確からしさ（log）
    keys: tuple        # 読んだアクション（同じ読みをまとめる）

    def label(self) -> str:
        what = "・".join(f"{a}{f' {m}' if m else ''}" for a, m, *_ in self.keys) or "捨てる"
        return f"{what}（{_SOURCE_JA.get(self.source, self.source)}）"


_SOURCE_JA = {"whisper": "Whisper", "rescue": "第 2 の耳・ライブの規則", "ear": "第 2 の耳の候補", "drop": "雑談"}


@dataclass
class ChoicePoint:
    start: float                 # 発話の始まり（utterance_start_ts）= 読み直しの鍵
    at: float                    # 再生で入る時刻（ハンドの割り当てに使う）
    heard: str                   # Whisper の書き起こし
    options: list[Option]        # [0] = 既定（いまの読み直しと同じ）

    def text_for(self, index: int) -> Optional[str]:
        """読み直しに渡す文（既定なら None = 書き起こしのまま）。"""
        return None if index == 0 else self.options[index].text


def _keys(events: list) -> tuple:
    return tuple((e.action, e.amount or 0, e.seat, e.position) for e in events)


def _flag_penalty(events: list, params: dict) -> float:
    flags = {f for e in events for f in e.parse_flags}
    return params["fuzzy"] if flags & {"fuzzy_keyword", "ambiguous_amount", "sentence_after_chatter"} else 0.0


def utterance_options(row: dict, params: dict = PARAMS) -> list[Option]:
    """1 つの発話の読みの選択肢。[0] が既定（`reparse_events` と同じ規則: Whisper で読めればそれ（額の無いベット /
    レイズには第 2 の耳の額）、読めなければ第 2 の耳のライブの規則、どちらも無ければ捨てる）。選択肢が 1 つしか
    無ければ選択点にしない。"""
    start = row.get("utterance_start_ts")
    text = (row.get("text") or "").strip()
    whisper = [] if _is_noise(row, text) else parse_actions(
        text, confidence=row.get("confidence"), utterance_start_ts=start)
    ear = row.get("ear") or None
    # ライブの規則（読めない発話の聞き直し・額の無いベット / レイズの額, `second_ear.apply_ear`）
    live, used = apply_ear(whisper, text, ear, question=is_question(text), utterance_start_ts=start)
    if any(e.action not in _BETTING for e in whisper + live):
        # ハンドの区切り・勝者の宣言などの制御は選ばない（既定のまま = ハンドどうしを独立に保つ）
        return [Option("whisper" if whisper and used is None else "rescue", text, 0.0, _keys(live))]
    options: list[Option] = []
    if used is not None:
        options.append(Option("rescue", used, params["rescue"], _keys(live)))
        if whisper:     # 額を入れる前の Whisper の読み
            options.append(Option("whisper", text, _flag_penalty(whisper, params), _keys(whisper)))
    elif whisper:
        options.append(Option("whisper", text, _flag_penalty(whisper, params), _keys(whisper)))
    else:
        # 定型の幻聴（「ご覧いただきありがとうございます。」）の下で第 2 の耳が何かを聞いた = 何かを言った（店舗
        # 7b897671 ハンド 3: オールインへのコールが幻聴になり、札の離脱でフォールドと記録した）
        heard = bool(ear and (ear.get("text") or "").strip()) and not row.get("no_speech") and is_prompt_echo(text)
        options.append(Option("drop", "", params["drop_heard"] if heard else 0.0, ()))
    if ear and (ear.get("text") or "").strip() and ear.get("logp") is not None:
        cands = sorted((c for c in ear.get("candidates") or [] if c.get("logp") is not None),
                       key=lambda c: -c["logp"])[:int(params["ear_top"])]
        for c in cands:
            diff = min(0.0, float(c["logp"]) - float(ear["logp"]))
            if diff < params["ear_min_diff"]:
                continue
            events = parse_actions(c["text"], utterance_start_ts=start)
            if not events or any(e.action not in _BETTING for e in events):
                continue
            options.append(Option("ear", c["text"], params["ear_base"] + params["ear_diff_weight"] * diff,
                                  _keys(events)))
    if options[0].keys:
        short = len(text) <= params["short_chars"] if options[0].source == "whisper" else False
        options.append(Option("drop", "", params["drop_short"] if short else params["drop_read"], ()))
    # 同じ読みは確からしさの高い方だけ（既定は必ず残す）
    kept: list[Option] = [options[0]]
    for opt in sorted(options[1:], key=lambda o: -o.logp):
        if all(opt.keys != k.keys for k in kept):
            kept.append(opt)
    return kept


def choice_points(events: list, transcripts: list[dict], params: dict = PARAMS) -> list[ChoicePoint]:
    """書き起こしの発話のうち、読みが 2 通り以上あるもの（`reparse_events` と同じ時刻の置き方）。"""
    from core.events import AudioEvent

    live_time: dict[float, float] = {}
    for e in events:
        if isinstance(e, AudioEvent) and e.utterance_start_ts is not None:
            live_time.setdefault(e.utterance_start_ts, e.timestamp)
    points = []
    for row in transcripts:
        start = row.get("utterance_start_ts")
        if start is None:
            continue
        options = utterance_options(row, params)
        if len(options) < 2:
            continue
        at = live_time.get(start, row.get("heard_at") or start)
        points.append(ChoicePoint(start=float(start), at=float(at), heard=(row.get("text") or "").strip(),
                                  options=options))
    return sorted(points, key=lambda p: p.at)


# ───────────────────────── 採点 ─────────────────────────


@dataclass
class Scored:
    score: float
    hand: dict
    penalties: list[str]
    assignment: tuple[int, ...]


def _hand_key(hand: dict) -> tuple:
    rows = tuple((a.get("street"), a.get("seat"), a.get("action"), a.get("amount") or 0)
                 for a in hand.get("actions") or [])
    return rows, hand.get("winner_seat")


def structure_penalties(hand: dict, unresolved: list[dict], params: dict = PARAMS) -> list[tuple[str, float]]:
    """ハンドとしての筋の通らなさ（罰の名前と値）。"""
    out: list[tuple[str, float]] = []
    for a in hand.get("actions") or []:
        src = a.get("actor_source")
        reasons = set(str(a.get("reason") or "").split("+")) - {""}
        if src == "implied":
            out.append(("補ったアクション", params["implied"]))
        if "synth_silent_fold" in reasons:
            out.append(("補ったフォールド", params["silent_fold"]))
        if reasons & {"amount_snapped", "no_amount_heard"}:
            out.append(("額を寄せた", params["snapped"]))
        if "rounded_to_bb" in reasons:
            out.append(("額を丸めた", params["rounded"]))
        if (reasons & {"check_facing_bet", "check_illegal_fold"}
                or any(r.endswith(("_illegal_to_call", "_illegal_to_fold")) for r in reasons)):
            out.append(("読み替えた", params["projected"]))
        if "mucked_stronger_hand" in reasons:
            out.append(("強い手が降りた", params["mucked_stronger"]))
    for u in unresolved:
        out.append((f"反映できない発話（{u.get('reason') or '?'}）", params["unresolved"]))
    if hand.get("winner_source") == "estimated":
        out.append(("勝者を推し量った", params["estimated_winner"]))
    board = [c for c in hand.get("board") or [] if isinstance(c, str) and c]
    dealt = _BOARD_STREET.get(len(board))
    betting = [a.get("street") for a in hand.get("actions") or [] if a.get("street") in _STREETS]
    last = betting[-1] if betting else "preflop"
    if dealt is not None and hand.get("winner_source") == "fold" and _STREETS.index(dealt) > _STREETS.index(last):
        out.append(("ボードより前のストリートで終わった", params["missing_street"]))
    if params["extra_street"]:
        limit = _STREETS.index(dealt) if dealt is not None else 0
        for _ in {s for s in betting if _STREETS.index(s) > limit}:
            out.append(("ボードに無いストリートのアクション", params["extra_street"]))
    return out


# ───────────────────────── 探索 ─────────────────────────


@dataclass
class HandEstimate:
    hand_id: int
    started_at: str
    points: list[ChoicePoint]
    default: Scored
    candidates: list[Scored] = field(default_factory=list)     # 良い順・同じ列は 1 つ

    @property
    def best(self) -> Scored:
        return self.candidates[0] if self.candidates else self.default

    @property
    def margin(self) -> Optional[float]:
        return round(self.candidates[0].score - self.candidates[1].score, 2) if len(self.candidates) > 1 else None

    @property
    def changed(self) -> bool:
        return _hand_key(self.best.hand) != _hand_key(self.default.hand)


class Estimator:
    """1 セッションの推定。`replay(texts, on_action) -> hands` は読み直しの文を渡して再生する関数。"""

    def __init__(self, events: list, transcripts: list[dict], setup: dict, flags: dict, session_id: str,
                 params: dict = PARAMS) -> None:
        self.events = events
        self.transcripts = transcripts
        self.setup = setup
        self.flags = flags
        self.session_id = session_id
        self.params = params
        self.replays = 0

    def _replay(self, texts: dict[float, str]) -> tuple[dict[str, dict], dict[str, list[dict]]]:
        """読み直しの文で再生し、ハンドの始まり → ハンド、ハンドの始まり → 反映できなかった発話 を返す。"""
        unresolved: list[Any] = []

        def on_action(record: Any) -> None:
            if getattr(record, "actor_source", None) == "unresolved":
                unresolved.append(record)

        events = reparse_events(self.events, self.transcripts, texts)
        hands = replay_session(events, self.setup, self.flags, self.session_id, on_action=on_action)
        self.replays += 1
        by_start = {h.get("started_at"): h for h in hands}
        id_to_start = {h.get("hand_id"): h.get("started_at") for h in hands}
        bad: dict[str, list[dict]] = {}
        for r in unresolved:
            start = id_to_start.get(getattr(r, "hand_id", None))
            if start is not None:
                bad.setdefault(start, []).append({"reason": getattr(r, "reason", ""), "action": r.action})
        return by_start, bad

    def _score(self, hand: dict, bad: list[dict], points: list[ChoicePoint], assignment: tuple[int, ...]) -> Scored:
        reading = sum(p.options[i].logp for p, i in zip(points, assignment))
        penalties = structure_penalties(hand, bad, self.params)
        chosen = [f"{p.heard or '（無音）'} → {p.options[i].label()}" for p, i in zip(points, assignment) if i]
        return Scored(round(reading + sum(v for _, v in penalties), 3), hand,
                      [f"{name} {v:+.1f}" for name, v in penalties] + chosen, assignment)

    def run(self) -> list[HandEstimate]:
        base, bad = self._replay({})
        starts = sorted(base, key=lambda s: _epoch(s) or 0.0)
        windows = []
        for i, s in enumerate(starts):
            lo = _epoch(s) or 0.0
            end = _epoch(base[s].get("ended_at"))
            hi = (end + self.params["after_hand_sec"]) if end is not None else lo + 600.0
            if i + 1 < len(starts):
                hi = min(hi, _epoch(starts[i + 1]) or hi)      # 次のハンドの配布まで
            windows.append((s, lo, hi))
        all_points = choice_points(self.events, self.transcripts, self.params)
        per_hand: dict[str, list[ChoicePoint]] = {s: [] for s in starts}
        known = {int(h) for h in (self.setup.get("hand_stacks") or {})}
        for p in all_points:
            for s, lo, hi in windows:
                if known and base[s].get("hand_id") not in known:
                    continue                  # 持ち点の分からないハンド（記録に無い）は選ばない
                # 再生でそのハンドの間に入る発話（最初のハンドより前の発話は選ばない = どのハンドにも入らない）
                if lo <= p.at < hi:
                    per_hand[s].append(p)
                    break
        for s, pts in per_hand.items():
            if len(pts) > self.params["max_points"]:
                # 既定と次点の差が小さい選択点から（迷いの大きいところ）
                pts.sort(key=lambda p: p.options[0].logp - max(o.logp for o in p.options[1:]))
                per_hand[s] = sorted(pts[:int(self.params["max_points"])], key=lambda p: p.at)
        estimates: dict[str, HandEstimate] = {}
        pools: dict[str, dict[tuple, Scored]] = {}
        current: dict[str, tuple[int, ...]] = {}
        for s in starts:
            pts = per_hand[s]
            assignment = tuple(0 for _ in pts)
            scored = self._score(base[s], bad.get(s, []), pts, assignment)
            estimates[s] = HandEstimate(hand_id=base[s].get("hand_id"), started_at=s, points=pts, default=scored)
            pools[s] = {assignment: scored}
            current[s] = assignment
        for _ in range(int(self.params["max_rounds"])):
            neighbors: dict[str, list[tuple[int, ...]]] = {}
            for s in starts:
                cur = current[s]
                opts = []
                for i, p in enumerate(per_hand[s]):
                    for j in range(len(p.options)):
                        if j != cur[i]:
                            cand = cur[:i] + (j,) + cur[i + 1:]
                            if cand not in pools[s]:
                                opts.append(cand)
                neighbors[s] = opts
            width = max((len(v) for v in neighbors.values()), default=0)
            if width == 0:
                break
            for k in range(width):
                texts: dict[float, str] = {}
                used: dict[str, tuple[int, ...]] = {}
                for s in starts:
                    assignment = neighbors[s][k] if k < len(neighbors[s]) else current[s]
                    used[s] = assignment
                    for p, i in zip(per_hand[s], assignment):
                        text = p.text_for(i)
                        if text is not None:
                            texts[p.start] = text
                hands, bad = self._replay(texts)
                for s in starts:
                    if k >= len(neighbors[s]) or s not in hands:
                        continue
                    pools[s][used[s]] = self._score(hands[s], bad.get(s, []), per_hand[s], used[s])
            moved = False
            for s in starts:
                best = max(pools[s].values(), key=lambda x: x.score)
                if best.score > pools[s][current[s]].score + 1e-9:
                    current[s] = best.assignment
                    moved = True
            if not moved:
                break
        out = []
        for s in starts:
            seen: dict[tuple, Scored] = {}
            for sc in sorted(pools[s].values(), key=lambda x: -x.score):
                seen.setdefault(_hand_key(sc.hand), sc)
            est = estimates[s]
            est.candidates = list(seen.values())[:int(self.params["nbest"])]
            out.append(est)
        return out


# ───────────────────────── 入出力 ─────────────────────────


def _action_row(a: dict) -> dict:
    return {k: a.get(k) for k in ("street", "seat", "action", "amount", "actor_source", "needs_review", "reason")}


def estimate_dict(session_id: str, estimates: list[HandEstimate], params: dict = PARAMS) -> dict:
    """`<sid>.estimate.json` の中身（影の推定。記録は変えない）。"""
    hands = []
    for est in estimates:
        hands.append({
            "hand_id": est.hand_id, "started_at": est.started_at, "changed": est.changed, "margin": est.margin,
            "choices": [{"utterance_start_ts": p.start, "heard": p.heard,
                         "options": [asdict(o) for o in p.options]} for p in est.points],
            "best": {"score": est.best.score, "assignment": list(est.best.assignment),
                     "actions": [_action_row(a) for a in est.best.hand.get("actions") or []],
                     "winner_seat": est.best.hand.get("winner_seat"), "board": est.best.hand.get("board"),
                     "notes": est.best.penalties},
            "default_score": est.default.score,
            "nbest": [{"score": c.score, "assignment": list(c.assignment), "notes": c.penalties,
                       "actions": [_action_row(a) for a in c.hand.get("actions") or []],
                       "winner_seat": c.hand.get("winner_seat")} for c in est.candidates],
        })
    return {"tool": "estimator", "estimator_version": ESTIMATOR_VERSION, "params_hash": params_hash(params),
            "tier": "session", "session_id": session_id,
            "created_at": datetime.now().isoformat(timespec="seconds"), "hands": hands}


@dataclass
class TruthResult:
    hand_id: int
    default: tuple[int, int]
    estimate: tuple[int, int]
    in_nbest: bool
    changed: bool


def exact(truth: dict, hand: dict) -> bool:
    m = measure_hand(truth, hand)
    return m.action_correct == m.action_total == len(truth.get("actions") or []) and m.winner_match is not False


def compare_with_truth(estimates: list[HandEstimate], truth: dict) -> list[TruthResult]:
    by_id = {e.hand_id: e for e in estimates}
    out = []
    for t in truth.get("hands") or []:
        est = by_id.get(t.get("hand_id"))
        if est is None:
            continue
        d = measure_hand(t, est.default.hand)
        b = measure_hand(t, est.best.hand)
        out.append(TruthResult(t["hand_id"], (d.action_correct, d.action_total), (b.action_correct, b.action_total),
                               any(exact(t, c.hand) for c in est.candidates), est.changed))
    return out


@dataclass
class SessionInput:
    session_id: str
    events: list
    transcripts: list[dict]
    setup: dict
    flags: dict
    truth: dict
    folder: Optional[Path] = None


def inputs_from_logs(paths: list[Path], only: Optional[list[str]] = None) -> tuple[list[SessionInput], Optional[Path]]:
    root, tmp = open_input(paths)
    config = find_config(root)
    out = []
    for files in find_sessions(root, only):
        record = _read_json(files.path(".json")) or {}
        setup = session_setup(record)
        if setup is None:
            continue
        events = load_events(files.events)
        truth = truth_hands(_read_json(files.path(".ground_truth.json")))
        out.append(SessionInput(files.session_id, events, _read_jsonl(files.path(".transcripts.jsonl")), setup,
                                replay_flags(config, events), truth, files.folder))
    return out, tmp


def inputs_from_fixtures(folder: Path = FIXTURES) -> list[SessionInput]:
    out = []
    for exp in sorted(folder.glob("*/expected.json")):
        data = json.loads(exp.read_text(encoding="utf-8"))
        setup = data.get("setup")
        if not setup:
            continue
        flags = {k: bool(setup.get(k)) for k in ("auto_new_hand", "auto_winner", "rfid_folds")}
        transcripts = _read_jsonl(exp.parent / "transcripts.jsonl") if (exp.parent / "transcripts.jsonl").exists() else []
        truth = {"hands": [dict(h["truth"], hand_id=h["hand_id"]) for h in data.get("hands") or []]}
        out.append(SessionInput(data.get("session_id") or exp.parent.name, load_events(exp.parent / "events.jsonl"),
                                transcripts, setup, flags, truth, exp.parent))
    return out


def estimate_session(inp: SessionInput, params: dict = PARAMS) -> tuple[list[HandEstimate], int]:
    est = Estimator(inp.events, inp.transcripts, inp.setup, inp.flags, inp.session_id, params)
    return est.run(), est.replays


def _fmt(a: dict) -> str:
    amount = f" {a.get('amount')}" if a.get("amount") else ""
    return f"{a.get('street')} 席{a.get('seat')} {a.get('action')}{amount}"


def print_session(inp: SessionInput, estimates: list[HandEstimate], replays: int, verbose: bool) -> list[TruthResult]:
    changed = [e for e in estimates if e.changed]
    print(f"=== {inp.session_id[:8]}: {len(estimates)} ハンド・選択点 {sum(len(e.points) for e in estimates)}"
          f"・変えたハンド {len(changed)}・再生 {replays} 回")
    results = compare_with_truth(estimates, inp.truth) if inp.truth.get("hands") else []
    by_id = {r.hand_id: r for r in results}
    for e in estimates:
        r = by_id.get(e.hand_id)
        if not (e.changed or verbose or (r is not None and r.estimate != r.default)):
            continue
        truth_note = ""
        if r is not None:
            truth_note = (f" ／ 真のアクション: 読み直し {r.default[0]}/{r.default[1]} → 推定 {r.estimate[0]}/{r.estimate[1]}"
                          f"{'・候補に正解あり' if r.in_nbest else ''}")
        print(f"  ハンド {e.hand_id}: 得点 {e.default.score:+.1f} → {e.best.score:+.1f}"
              f"（次点との差 {e.margin if e.margin is not None else '—'}）{truth_note}")
        for note in e.best.penalties:
            print(f"      {note}")
        if e.changed:
            before = [_fmt(a) for a in e.default.hand.get("actions") or []]
            after = [_fmt(a) for a in e.best.hand.get("actions") or []]
            print("      読み直し: " + " / ".join(before))
            print("      推定　　: " + " / ".join(after))
    return results


def print_totals(results: list[TruthResult]) -> None:
    if not results:
        return
    d = sum(r.default[0] for r in results), sum(r.default[1] for r in results)
    b = sum(r.estimate[0] for r in results), sum(r.estimate[1] for r in results)
    worse = [r.hand_id for r in results if r.estimate[0] < r.default[0]]
    better = [r.hand_id for r in results if r.estimate[0] > r.default[0]]
    print(f"合計（真のアクションのあるハンド {len(results)}）: 読み直し {d[0]}/{d[1]}（{d[0] / max(d[1], 1):.0%}）"
          f" → 推定 {b[0]}/{b[1]}（{b[0] / max(b[1], 1):.0%}）・良くなった {len(better)}・悪くなった {len(worse)}"
          f"・正解が候補に入った {sum(r.in_nbest for r in results)}/{len(results)}")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="推定器 v0（影で回す。記録は変えない）")
    ap.add_argument("path", type=Path, nargs="*", help="店舗の zip（分けたものは全部）/ logs フォルダ")
    ap.add_argument("--fixtures", action="store_true", help="回帰テストの店舗 fixture で回す")
    ap.add_argument("--session", action="append", help="セッション ID（先頭の数文字でよい）")
    ap.add_argument("--write", action="store_true", help="<sid>.estimate.json を書く（ログのフォルダ / fixture のフォルダ）")
    ap.add_argument("--json", action="store_true", help="推定を JSON で出す")
    ap.add_argument("--verbose", action="store_true", help="変えなかったハンドも出す")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.ERROR)
    logging.disable(logging.CRITICAL)      # 候補の再生で出るエンジンのログは要らない
    if not args.path and not args.fixtures:
        ap.error("zip / logs フォルダか --fixtures を指定してください")
    tmp = None
    inputs = inputs_from_fixtures() if args.fixtures else []
    if args.path:
        more, tmp = inputs_from_logs(args.path, args.session)
        inputs.extend(more)
    if args.session:
        inputs = [i for i in inputs if any(i.session_id.startswith(s) for s in args.session)]
    all_results: list[TruthResult] = []
    dumped = []
    try:
        for inp in inputs:
            estimates, replays = estimate_session(inp)
            data = estimate_dict(inp.session_id, estimates)
            if args.json:
                dumped.append(data)
            else:
                all_results.extend(print_session(inp, estimates, replays, args.verbose))
            if args.write and inp.folder is not None and tmp is None:
                (inp.folder / f"{inp.session_id}{ESTIMATE_SUFFIX}").write_text(
                    json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    finally:
        if tmp is not None:
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)
    if args.json:
        print(json.dumps(dumped, ensure_ascii=False, indent=1))
    else:
        print_totals(all_results)
    return 0


__all__ = ["ESTIMATOR_VERSION", "PARAMS", "Estimator", "HandEstimate", "Option", "ChoicePoint", "choice_points",
           "utterance_options", "structure_penalties", "estimate_dict", "compare_with_truth", "estimate_session",
           "inputs_from_fixtures", "inputs_from_logs", "row_correct"]

if __name__ == "__main__":
    raise SystemExit(main())
