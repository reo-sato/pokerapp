"""integration/world_replay.py — 生の観測から、聞き取りの遅れの無いライブとしてハンドを組み直す（推定器 v1 の再生器）

ライブの記録（`events.jsonl`）の席の信号（`hand_start` / `leave` / `muck` / `return` / `spoken_fold` / `confirm` /
`showdown` / `street` / `reinterpret`）は、ライブのロガーが**その場の解釈**（ボタン・手番）と**聞き取りの遅れ**の下で
作ったもの（再生を決定的にするために記録している）。解釈を変えて再生すると古い判断が残る（店舗 9d1d8536 ハンド 4:
ずれたボタンで決めた「この『フォールド』は席5」が、ボタンを直した再生でもう 1 つのフォールドになった）。

ここではそれらを捨て、解釈の入っていない観測だけを入力にする（ADR-0056 追記 1 の S3「在否の履歴からの再生」）:

- 札の読み取り（席・ボードの札の RFID。ボードの位置は RFID 側で決めたもの）と配布の信号（`deal`。在否だけで決まる）
- 席の札の在否の履歴（卓状態の履歴 `<sid>.table_state.jsonl` → `PresenceTimeline`）
- 発話（書き起こしをいまの読み取りで読む。**話し終わった時刻に届く** = 聞き取りの遅れが無い）
- 打った操作（記録の時刻）

これをライブと同じ engine に、ライブのループと同じ順で定期の確認（配布・札の離脱・観測の反映・確定の待ち）を
呼びながら時計を進めて流す。発話の途中の観測は、ライブと同じく「認識中の発話」より前のものだけを先に入れる。
推定器（`integration/estimator.py`）はこの入力を編集して（聞こえなかったアクションを足す・余計な語を捨てる・
ボタン・札の離脱を持ち上げとみる …）ハンドの候補を作る。
"""
from __future__ import annotations

import bisect
import copy
import statistics
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from audio.recognizer import is_implausibly_long, is_prompt_echo, is_question, parse_actions
from audio.second_ear import apply_ear
from core.event_queue import make_audio_queue
from core.events import AudioEvent, CameraEvent, RFIDEvent
from core.game_state import PlayerState
from core.poker_engine import create_game_state
from integration.engine import IntegrationThread
from integration.replay import Event, _ReplayClock
from output.json_writer import JsonWriter

# ライブのロガーがその場の解釈・聞き取りの遅れの下で作った席の信号（生の観測から作り直すので入力にしない）
DERIVED_SIGNALS = frozenset({
    "hand_start", "deal_order", "leave", "muck", "return", "spoken_fold", "confirm", "showdown",
    "showdown_end", "street", "reinterpret",
})
# 配布の信号（`deal`）は残す: ライブが席の在否だけで決めたもの（聞き取りの遅れ・解釈に依らない）。卓状態の履歴には
# ボードのリーダーの生の枚数が無く（記録した札からの推定はハンドの切り替わりで狂う）、配布を決め直せないため。
# 配布の信号の無い古い記録（schema 0.9 より前）だけ、席の札の読み取りから決め直す。
TICK_SEC = 0.25          # 定期の確認の間隔（ライブのループは発話を 0.1 秒待つ）
FLUSH_SEC = 30.0         # 最後の入力のあと、確定の待ち（フォールドの確定・片付け）を流す時間
_ORDER: dict[type, int] = {CameraEvent: 0, RFIDEvent: 1, AudioEvent: 2}


# ───────────────────────── 札の在否の履歴 ─────────────────────────


@dataclass(frozen=True)
class SeatObservation:
    t: float                          # この状態になった時刻（epoch）
    present: bool
    absent_since: Optional[float]     # 札が消えた時刻（載っていれば None）
    mucked_at: Optional[float]        # 手札が卓の中央を通過した時刻（ADR-0058）


def _naive_epoch(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()


def _epoch_offset(rows: list[dict]) -> float:
    """卓状態の `updated_at`（店舗 PC の現地時刻, 時差なし）を epoch に直すずれ。`observed_at`（epoch）のある行から。"""
    diffs = [float(r["observed_at"]) - _naive_epoch(r["updated_at"]) for r in rows
             if isinstance(r.get("observed_at"), (int, float))]
    if not diffs:
        return 0.0
    return round(statistics.median(diffs) / 900.0) * 900.0     # 時差は 15 分単位


class PresenceTimeline:
    """席の札の在否（`RFIDThread.presence_snapshot()` の形）を、任意の時刻について答える。

    卓状態の履歴は約 1 秒ごと・変化のあったときの行なので、札が消えた時刻は `away_sec` から戻す。
    札が戻った時刻は行の時刻（最大 1 秒遅れ）。まだ一度も載っていない席は snapshot に出さない（ライブと同じ）。
    """

    def __init__(self, seats: dict[int, list[SeatObservation]], board: Optional[list] = None) -> None:
        self._seats = {s: sorted(obs, key=lambda o: o.t) for s, obs in seats.items()}
        self._times = {s: [o.t for o in obs] for s, obs in self._seats.items()}
        self._reads: dict[int, list[tuple[float, str]]] = {}      # 席 → (読んだ時刻, 札)

    def attach_reads(self, events: Iterable[Event]) -> "PresenceTimeline":
        """席の札の読み取り（RFID の card イベント）から、いま載っている札を答えられるようにする（配布の検出に使う。
        卓状態の履歴の札は engine が記録した手札なので使わない）。"""
        self._reads = {}
        for e in events:
            if isinstance(e, RFIDEvent) and e.kind == "card" and e.role == "seat" and e.seat is not None and e.card:
                self._reads.setdefault(e.seat, []).append((e.timestamp, e.card))
        for reads in self._reads.values():
            reads.sort()
        return self

    def _cards_on(self, seat: int, t: float) -> list[str]:
        """いま席に載っている札: 最後に札が消えてから（読み取りは卓状態より最大 1 秒早い）読んだ札。"""
        o = self._at(seat, t)
        if o is None or not o.present:
            return []
        obs = self._seats[seat]
        i = bisect.bisect_right(self._times[seat], t) - 1
        gone = next((obs[j].t for j in range(i, -1, -1) if not obs[j].present), None)
        since = (gone - 1.0) if gone is not None else float("-inf")
        cards: list[str] = []
        for at, card in self._reads.get(seat, []):
            if since < at <= t and card not in cards:
                cards.append(card)
        return sorted(cards)

    @classmethod
    def from_table_state(cls, rows: Iterable[dict]) -> "PresenceTimeline":
        rows = [r for r in rows if isinstance(r, dict) and isinstance(r.get("updated_at"), str)]
        offset = _epoch_offset(rows)
        seats: dict[int, list[SeatObservation]] = {}
        last: dict[int, tuple] = {}
        muck_since: dict[int, float] = {}
        for r in rows:
            t = _naive_epoch(r["updated_at"]) + offset
            for s in r.get("seats") or []:
                seat = s.get("seat")
                if not isinstance(seat, int):
                    continue
                present = bool(s.get("present"))
                away = s.get("away_sec")
                absent_since = None if present or away is None else t - float(away)
                if present:
                    muck_since.pop(seat, None)
                elif s.get("mucked"):
                    muck_since.setdefault(seat, t)
                mucked_at = muck_since.get(seat) if not present else None
                prev_key = last.get(seat)
                if (prev_key is not None and not present and not prev_key[0] and absent_since is not None
                        and prev_key[1] is not None and abs(absent_since - prev_key[1]) < 1.5):
                    absent_since = prev_key[1]      # 同じ不在（行ごとの away_sec の丸めで揺れる）
                key = (present, None if absent_since is None else round(absent_since, 1), mucked_at)
                if prev_key == key:
                    continue
                last[seat] = key
                # 札が消えたときは消えた時刻に置く（行は最大 1 秒遅れ）。時刻は席ごとに戻さない
                at = absent_since if (absent_since is not None and not (last.get(seat, (True,))[0])) else t
                prev = seats.get(seat)
                if prev and at < prev[-1].t:
                    at = prev[-1].t
                seats.setdefault(seat, []).append(SeatObservation(at, present, absent_since, mucked_at))
        # 一度も札が載らなかった席（卓で使っていない席）は出さない
        return cls({s: obs for s, obs in seats.items() if any(o.present for o in obs)}, [])

    def to_rows(self) -> list[dict]:
        """席の在否の変化だけの行（fixture の `presence.jsonl`。卓状態の履歴は大きいので）。"""
        rows = [{"t": round(o.t, 3), "seat": seat, "present": o.present,
                 "absent_since": None if o.absent_since is None else round(o.absent_since, 3),
                 "mucked_at": None if o.mucked_at is None else round(o.mucked_at, 3)}
                for seat, obs in self._seats.items() for o in obs]
        return sorted(rows, key=lambda r: (r["t"], r["seat"]))

    @classmethod
    def from_rows(cls, rows: Iterable[dict]) -> "PresenceTimeline":
        seats: dict[int, list[SeatObservation]] = {}
        for r in rows:
            seats.setdefault(int(r["seat"]), []).append(SeatObservation(
                float(r["t"]), bool(r["present"]), r.get("absent_since"), r.get("mucked_at")))
        return cls(seats)

    def _at(self, seat: int, t: float) -> Optional[SeatObservation]:
        i = bisect.bisect_right(self._times.get(seat, []), t) - 1
        return self._seats[seat][i] if i >= 0 else None

    def snapshot(self, t: float) -> dict[int, dict]:
        out: dict[int, dict] = {}
        for seat in self._seats:
            o = self._at(seat, t)
            if o is None or (not o.present and o.absent_since is None):
                continue
            cards = self._cards_on(seat, t)
            out[seat] = {"present": o.present, "absent_since": None if o.present else o.absent_since,
                         "uid_count": len(cards) if o.present else 0, "mucked_at": o.mucked_at, "cards": cards,
                         "since": {}}
        return out

    def absent_since(self, seat: int, t: float) -> Optional[float]:
        o = self._at(seat, t)
        return None if o is None or o.present else o.absent_since

    def board_snapshot(self, t: float) -> dict:
        """ボードのリーダーの枚数は卓状態の履歴に無い（0 とする = 配布の検出を止めない）。"""
        return {"absent": {}, "pending": {}, "present_count": 0}

    def departures(self, t0: float, t1: float) -> list[tuple[int, float, Optional[float]]]:
        """[t0, t1) に札が消えた席と時刻と戻った時刻（戻らなければ None）。推定器の「持ち上げ」の選択点に使う。"""
        out = []
        for seat, obs in self._seats.items():
            for i, o in enumerate(obs):
                if o.present or o.absent_since is None or not t0 <= o.absent_since < t1:
                    continue
                if i > 0 and not obs[i - 1].present:
                    continue                      # 同じ不在の続き（中央を通過した など）
                back = next((n.t for n in obs[i + 1:] if n.present), None)
                out.append((seat, o.absent_since, back))
        return sorted(out, key=lambda x: (x[1], x[0]))


# ───────────────────────── 発話 ─────────────────────────


def is_noise(row: dict, text: str) -> bool:
    """声ではない音・雑音への幻聴（ライブと同じ判定）。"""
    return bool(row.get("no_speech")) or not text or is_prompt_echo(text) or is_implausibly_long(
        text, float(row.get("audio_sec") or 0.0))


def read_utterance(row: dict, text: Optional[str] = None) -> list[AudioEvent]:
    """1 つの発話（書き起こしの行）をいまの読み取りで読んだアクション。`text` を渡せばその文で読み直す（第 2 の
    耳の結果は重ねない）。時刻は話し始め（`utterance_start_ts`）。"""
    start = row.get("utterance_start_ts")
    overridden = text is not None
    text = ((text if overridden else row.get("text")) or "").strip()
    events = [] if is_noise(row, text) else parse_actions(
        text, confidence=row.get("confidence"), utterance_start_ts=start)
    if not overridden and row.get("ear"):
        events, _ = apply_ear(events, text, row["ear"], question=is_question(text), utterance_start_ts=start)
    return events


def delivered_at(row: dict) -> float:
    """聞き取りの遅れが無ければ、その発話がアクションとして届く時刻（話し終わり）。"""
    start = float(row["utterance_start_ts"])
    return start + max(0.0, float(row.get("audio_sec") or 0.0))


def world_events(events: list[Event], transcripts: list[dict],
                 texts: Optional[dict[float, Optional[str]]] = None,
                 extra: Optional[list[Event]] = None,
                 drops: Optional[dict[float, set[int]]] = None,
                 cache: Optional[dict] = None) -> list[Event]:
    """世界の時刻の入力: 席の信号を捨て、発話は書き起こしから読み直して話し終わりに置く。

    `texts`: 発話の始まり → 読み直す文（None = 書き起こしのまま、空 = アクションにしない）。
    `drops`: 発話の始まり → 捨てる語の番号（読んだアクションの何番目か。余計な語 = 推定器の選択点）。
    `extra`: 推定器が足す入力（聞こえなかったアクション・ボタン）。
    `cache`: 読んだ結果を覚えておく辞書（推定器は同じ発話を何度も読み直すので）。
    打った操作（書き起こしに無い発話のイベント）は記録の時刻のまま。同じ発話の語は 1 ms ずつずらす
    （記録の時刻 = ミリ秒で語を見分ける）。
    """
    rows = {r["utterance_start_ts"]: r for r in transcripts if r.get("utterance_start_ts") is not None}
    out: list[Event] = []
    for e in events:
        if isinstance(e, RFIDEvent) and e.kind in DERIVED_SIGNALS:
            continue
        if isinstance(e, AudioEvent) and e.utterance_start_ts in rows:
            continue                               # 書き起こしから読み直す
        out.append(e)
    for start, row in rows.items():
        text = (texts or {}).get(start)
        if cache is None:
            parsed = read_utterance(row, text)
        else:
            if (start, text) not in cache:
                cache[(start, text)] = read_utterance(row, text)
            parsed = [copy.copy(ev) for ev in cache[(start, text)]]
        dropped = (drops or {}).get(start) or set()
        parsed = [ev for i, ev in enumerate(parsed) if i not in dropped]
        at = delivered_at(row)
        for i, ev in enumerate(parsed):
            ev.timestamp = at + 0.001 * i          # 同じ発話の中の順を保つ
        out.extend(parsed)
    out.extend(extra or [])
    return sorted(out, key=lambda e: (e.timestamp, _ORDER[type(e)]))


# ───────────────────────── 再生 ─────────────────────────


def replay_world(
    events: list[Event],
    presence: Optional[PresenceTimeline],
    *,
    players: list[PlayerState],
    sb: int,
    bb: int,
    session_id: str,
    auto_new_hand: bool = True,
    auto_winner: bool = True,
    rfid_folds: bool = True,
    button_seat: Optional[int] = None,
    hand_stacks: Optional[dict[int, dict[int, int]]] = None,
    hand_buttons: Optional[dict[int, int]] = None,
    on_action: Optional[Callable[[Any], None]] = None,
    close_open_hand: bool = True,
    tick: float = TICK_SEC,
) -> list[dict]:
    """`world_events` の入力を、ライブのループと同じ順の定期の確認を挟みながら流し、ハンド（記録と同じ形）を返す。

    `hand_stacks` / `hand_buttons`: hand_id → そのハンドの持ち点 / ボタン（推定器はハンドを互いに独立に扱う）。
    `button_seat`: 1 ハンド目のボタンの 1 つ手前の席（`create_game_state` と同じ）。
    """
    gs = create_game_state("pokerkit", players, sb, bb, button_seat=button_seat)
    clock = _ReplayClock()
    if presence is not None:
        # 配布の信号が無い古い記録だけ、席に載っている札から配布を決め直す（信号があれば二重に検出しない）
        recorded = any(isinstance(e, RFIDEvent) and e.kind == "deal" for e in events)
        presence.attach_reads([] if recorded else events)
    speech = sorted({(e.utterance_start_ts, e.timestamp) for e in events
                     if isinstance(e, AudioEvent) and e.utterance_start_ts is not None})
    starts = [s for s, _ in speech]

    def pending_since() -> Optional[float]:
        """話し始めていて、まだ届いていない（認識中の）発話の始まり。"""
        now = clock.now
        hi = bisect.bisect_right(starts, now)
        pending = [s for s, d in speech[max(0, hi - 64):hi] if d > now]
        return min(pending) if pending else None

    def backlog() -> int:
        return 0 if pending_since() is None else 1

    def before_new_hand(hand_id: int) -> None:
        stacks = (hand_stacks or {}).get(hand_id)
        if stacks and hasattr(gs, "set_stacks"):
            gs.set_stacks(stacks)
        button = (hand_buttons or {}).get(hand_id)
        if button is not None and hasattr(gs, "set_button"):
            try:
                gs.set_button(button)
            except ValueError:
                pass

    with tempfile.TemporaryDirectory(prefix="world_replay_") as tmp:
        thread = IntegrationThread(
            audio_queue=make_audio_queue(),
            game_state=gs,
            json_writer=JsonWriter(tmp, session_id),
            clock=clock,
            stop_event=threading.Event(),
            auto_new_hand=auto_new_hand,
            auto_winner=auto_winner,
            rfid_folds=rfid_folds,
            recorded_deals=False,
            button_from_deal=False,
            seat_presence=(lambda: presence.snapshot(clock.now)) if presence is not None else None,
            seat_cards_absent_since=(lambda seat: presence.absent_since(seat, clock.now))
            if presence is not None else None,
            board_presence=(lambda: presence.board_snapshot(clock.now)) if presence is not None else None,
            speech_pending_since=pending_since,
            speech_backlog=backlog,
            before_new_hand=before_new_hand if (hand_stacks or hand_buttons) else None,
            on_action=on_action,
        )

        # ハンドを確定する時点で、まだ手番の人がいたか（ベッティングのラウンドが閉じないまま終わった = 推定器が
        # 罰する。実卓では全員が降りるかショーダウンでしか終わらない）
        open_at_end: dict[int, bool] = {}
        finalize = thread._finalize_hand                # noqa: SLF001

        def finalize_and_note(*args: Any, **kwargs: Any) -> None:
            try:
                open_at_end[int(getattr(gs, "_hand_id", 0))] = bool(
                    gs.is_hand_active() and gs.legal_context().actor_seat is not None)
            except Exception:  # noqa: BLE001 — 印を付けられないだけ
                pass
            finalize(*args, **kwargs)

        thread._finalize_hand = finalize_and_note        # noqa: SLF001

        def periodic() -> None:
            thread._check_deal_presence()     # noqa: SLF001 — ライブの run() と同じ順
            thread._check_table_cleared()     # noqa: SLF001
            thread._poll_departures()         # noqa: SLF001

        def idle() -> None:
            thread._start_dealt_hand_if_ready()   # noqa: SLF001
            thread._apply_idle_observations()     # noqa: SLF001
            thread._check_foldout_timeout()       # noqa: SLF001
            thread._check_showdown_timeout()      # noqa: SLF001
            thread._expire_buffers()              # noqa: SLF001

        def advance(until: float) -> None:
            while clock.now + tick < until:
                clock.now += tick
                periodic()
                idle()

        ordered = sorted(events, key=lambda e: (e.timestamp, _ORDER[type(e)]))
        if ordered:
            clock.now = ordered[0].timestamp
        for ev in ordered:
            advance(ev.timestamp)
            clock.now = max(clock.now, ev.timestamp)
            periodic()
            if isinstance(ev, AudioEvent):
                thread._expire_buffers()          # noqa: SLF001
                thread._handle_audio_event(ev)    # noqa: SLF001
                thread._expire_buffers()          # noqa: SLF001
            elif isinstance(ev, RFIDEvent):
                thread._process_rfid_event(ev)    # noqa: SLF001
            elif isinstance(ev, CameraEvent):
                thread._camera_buffer.append(ev)  # noqa: SLF001
        advance(clock.now + FLUSH_SEC)
        if close_open_hand:
            thread._close_open_hand_at_stop()     # noqa: SLF001
        import json
        path = Path(tmp) / f"{session_id}.json"
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    hands = list(data.get("hands") or [])
    for h in hands:
        if isinstance(h, dict) and h.get("hand_id") in open_at_end:
            h["betting_open_at_end"] = open_at_end[h["hand_id"]]
    return hands
