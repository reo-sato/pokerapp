"""integration/replay.py

Phase F1 (#8) — 決定的 replay ハーネス (R4)。

記録済み `events.jsonl` (R1 / `output/event_recorder.py` の `reconstruction_event` envelope) を
読み戻し、live と同じ `IntegrationThread` の per-event 処理に **timestamp 昇順**で通して
`HandSummary` を再構築する。非決定性の源だった wall-clock は `IntegrationThread` に注入する
`clock` で固定するため、同一入力は同一出力になる (round-trip 決定性、DoD #3)。

設計判断 (ADR-0011): threaded `run()` は回さず、timestamp 昇順の同期 driver が
`_handle_audio_event` / `_process_rfid_event` / camera buffer / `_expire_buffers` を再利用する。
理由は `run()` の queue ポーリング / drain 順序が wall-clock スケジューリングに依存し決定的に
扱いにくいため。sidecar 記録 (`_record`) は replay 入力が記録そのものなので呼ばない。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Callable, Optional, Union

from core.event_queue import make_audio_queue
from core.events import AudioEvent, CameraEvent, RFIDEvent
from core.game_state import PlayerState
from core.hand_log import ActionRecord, HandSummary
from core.poker_engine import create_game_state
from integration.engine import IntegrationThread
from output.json_writer import JsonWriter

Event = Union[AudioEvent, RFIDEvent, CameraEvent]

# 同一 timestamp での処理順を live `run()` の drain 順（camera→rfid→audio）に合わせる tie-break。
# これにより同 ts の sensor が audio の corroboration に間に合う（ISSUE-0010 の fidelity）。
_ORDER: dict[type, int] = {CameraEvent: 0, RFIDEvent: 1, AudioEvent: 2}


def _is_signal(ev: Event) -> bool:
    return isinstance(ev, RFIDEvent) and ev.kind != "card"


def _in_replay_order(events: list[Event]) -> list[Event]:
    """live が処理した順に並べる。

    記録（events.jsonl）は live が処理した順に書かれている。時刻では並べ替えない: ボードの札は 2 秒載り続けて
    から確定し、時刻は最初に見えた時刻なので、そのあいだに処理した信号より前の時刻になる（店舗 2026-09-29
    d0f055fb: 時刻で並べ替えるとターンの札がフロップの信号・札の離脱より先に流れ、4 ハンドが記録と違った）。
    ただし発話の処理の中で出した信号（発話と同じ時刻で、その直後に記録したもの）は、live はその発話のアクションを
    入れる前に反映したので、発話の前に流す。engine の信号が無い記録（信号を記録する前の記録・手で書いた
    fixture）は従来どおり時刻順（同じ時刻は camera → rfid → audio）。
    「フォールド」の語を札の離脱と組にした信号は、live では語のあとに出したもの（語の働きはその信号がすべて）。語の前に
    流れても engine が語の直前の組の信号を見て語を流さない（`IntegrationThread._fold_word_already_signalled`）。
    """
    if not any(_is_signal(e) for e in events):
        return sorted(events, key=lambda e: (e.timestamp, _ORDER[type(e)]))
    ordered: list[Event] = []
    i = 0
    while i < len(events):
        ev = events[i]
        j = i + 1
        while j < len(events) and _is_signal(events[j]):
            j += 1                            # この出来事のあとに続けて記録した信号
        signals = events[i + 1:j]
        if isinstance(ev, AudioEvent):
            # 発話と同じ時刻の信号 = その発話の処理の中で出したもの（アクションを入れる前に反映した）
            same = [s for s in signals if s.timestamp == ev.timestamp]
            ordered.extend(same)
            ordered.append(ev)
            ordered.extend(s for s in signals if s.timestamp != ev.timestamp)
        else:
            ordered.append(ev)
            ordered.extend(signals)
        i = j
    return ordered


class _ReplayClock:
    """各イベントの timestamp を「現在時刻」として返す注入用時計。"""

    def __init__(self) -> None:
        self.now: float = 0.0

    def __call__(self) -> float:
        return self.now


def event_from_envelope(d: dict) -> Event:
    """`reconstruction_event` envelope (dict) を Event データクラスに復元する。

    `output/event_recorder.py:event_to_envelope` の逆。
    """
    t = d.get("type")
    if t == "audio":
        return AudioEvent(
            action=d["action"], amount=d["amount"], timestamp=d["timestamp"],
            raw_text=d["raw_text"], seat=d.get("seat"), confidence=d.get("confidence"),
            # additive（ISSUE-0032 / ADR-0047 / ADR-0048）。旧 events.jsonl には無いので get の既定で後方互換。
            position=d.get("position"),
            parse_flags=tuple(d.get("parse_flags") or ()),
            utterance_start_ts=d.get("utterance_start_ts"),
            hand_name=d.get("hand_name"),             # 勝った役名（0.6 additive）
            hit_rank=d.get("hit_rank"),               # 「Nヒット」の N（0.16 additive）
            amount_options=tuple(d.get("amount_options") or ()),   # 音で読んだ額の候補（0.13 additive）
            amount_scores=tuple((int(a), float(s)) for a, s in d.get("amount_scores") or ()),   # 0.14 additive
        )
    if t == "rfid":
        return RFIDEvent(
            tag_id=d["tag_id"], card=d["card"], reader_id=d["reader_id"], role=d["role"],
            seat=d.get("seat"), timestamp=d["timestamp"], raw_tag_id=d.get("raw_tag_id") or "",
            board_index=d.get("board_index"),
            replaces=d.get("replaces"),   # ADR-0058 additive（旧 events.jsonl には無い）
            kind=d.get("kind") or "card",           # 札の離脱・戻り（2026-09-25 additive）
            observed_at=d.get("observed_at"),
            cards=tuple(d.get("cards") or ()),      # 配布の手札（0.9 additive）
            times=tuple(d.get("times") or ()),      # 手札を最初に読んだ時刻（0.11 additive）
        )
    if t == "camera":
        return CameraEvent(seat=d["seat"], timestamp=d["timestamp"])
    raise ValueError(f"unknown reconstruction_event type: {t!r}")


def load_events(path: str | Path) -> list[Event]:
    """events.jsonl (1 行 1 envelope) を Event 列に復元する。空行と JSON として読めない行（電源断のあとの
    書きかけ・NUL の末尾）は飛ばす。形の合わない envelope は今まで通り例外（記録の誤り）。"""
    from core.atomic_io import read_jsonl

    return [event_from_envelope(d) for d in read_jsonl(path)]


def replay_events(
    events: list[Event],
    *,
    backend: str,
    players: list[PlayerState],
    sb: int,
    bb: int,
    session_id: str,
    out_dir: str | Path,
    auto_new_hand: bool = False,
    auto_winner: bool = False,
    rfid_folds: bool = False,
    button_seat: Optional[int] = None,
    close_open_hand: bool = False,
    hand_stacks: Optional[dict[int, dict[int, int]]] = None,
    on_action: Optional[Callable[[ActionRecord], None]] = None,
) -> list[HandSummary]:
    """Event 列を timestamp 昇順で再構築し、確定した HandSummary 群を返す。

    `out_dir` には JsonWriter が `{session_id}.json` を書く (session_id 源でもある)。
    決定性のため clock を注入し、ActionRecord/HandSummary の timestamp を各イベントの
    timestamp に固定する。

    `auto_new_hand` / `auto_winner`（ADR-0062, 既定 False）: 手札の配布でハンドを始め、勝者を自動で決めた
    セッション（店舗の既定）を再生するときに True にする。replay は発話の認識待ちを持たないので、
    配布を検出した時点で新しいハンドを始める。
    `rfid_folds`: フォールドを札の離脱で決めたセッション（記録された leave / return / confirm で再現する）。
    `button_seat`: 1 ハンド目のボタンの **1 つ手前**の席（`create_game_state` と同じ。省略時は最大の席番号）。
    `close_open_hand`: 最後に確定していないハンドを、終了（`q`）と同じ規則で閉じる（記録に終了が無いセッション）。
    `hand_stacks`: hand_id → {席: 持ち点}。そのハンドをこの持ち点から始める（評価用: 記録の `stack_start` を渡すと、
    前のハンドの違いが持ち点を通して次のハンドへ持ち越されない。真のアクションのオールインの額は記録の持ち点から
    決めているので、これが無いと前のハンドを直しただけで後のハンドが違って見える）。pokerkit backend のみ。
    `on_action`: live の画面と同じ通知（反映できなかった発話 = actor_source "unresolved" も来る。推定器が採点に使う）。

    記録に配布の信号（rfid kind `deal` / `hand_start`, schema 0.9）があれば、札の読み取りから配布を決め直さず、
    live が在否で決めた配布と、発話を待って始めた時点に従う（在否を持たない replay で live と同じにするため）。
    """
    gs = create_game_state(backend, players, sb, bb, button_seat=button_seat)
    json_writer = JsonWriter(out_dir, session_id)
    summaries: list[HandSummary] = []
    clock = _ReplayClock()

    def start_from_recorded_stacks(hand_id: int) -> None:
        stacks = (hand_stacks or {}).get(hand_id)
        if stacks and hasattr(gs, "set_stacks"):
            gs.set_stacks(stacks)

    thread = IntegrationThread(
        audio_queue=make_audio_queue(),
        game_state=gs,
        json_writer=json_writer,
        on_hand=summaries.append,
        clock=clock,
        stop_event=threading.Event(),
        auto_new_hand=auto_new_hand,
        auto_winner=auto_winner,
        rfid_folds=rfid_folds,
        recorded_deals=any(isinstance(e, RFIDEvent) and e.kind == "deal" for e in events),
        before_new_hand=start_from_recorded_stacks if hand_stacks else None,
        on_action=on_action,
    )

    for ev in _in_replay_order(events):
        clock.now = max(clock.now, ev.timestamp)   # 記録の順（時刻が前後する札）でも時計は戻さない
        if isinstance(ev, AudioEvent):
            thread._expire_buffers()          # noqa: SLF001 — live run() と同じ前処理
            thread._handle_audio_event(ev)    # noqa: SLF001
            thread._expire_buffers()          # noqa: SLF001
        elif isinstance(ev, RFIDEvent):
            thread._process_rfid_event(ev)    # noqa: SLF001
        elif isinstance(ev, CameraEvent):
            thread._camera_buffer.append(ev)  # noqa: SLF001 — live は drain で buffer 追加
        else:  # pragma: no cover — load_events が型を保証
            raise TypeError(f"unexpected event: {ev!r}")
    if close_open_hand:
        thread._close_open_hand_at_stop()     # noqa: SLF001 — 終了（q）と同じ規則で保存

    return summaries


def replay_fixture(case_dir: str | Path, out_dir: str | Path) -> list[HandSummary]:
    """`<case_dir>/{setup.json, events.jsonl}` を読み replay する。

    setup.json = {"backend", "sb", "bb", "session_id", "players":[{"seat","name","stack"},...]}。
    任意で "auto_new_hand" / "auto_winner"（ADR-0062, 既定 false）/ "rfid_folds"（札の離脱でフォールド）/
    "button_seat"（1 ハンド目のボタンの 1 つ手前の席）。
    """
    case_dir = Path(case_dir)
    setup = json.loads((case_dir / "setup.json").read_text(encoding="utf-8"))
    events = load_events(case_dir / "events.jsonl")
    players = [
        PlayerState(seat=p["seat"], name=p["name"], stack=p["stack"])
        for p in setup["players"]
    ]
    return replay_events(
        events,
        backend=setup.get("backend", "legacy"),
        players=players,
        sb=setup["sb"], bb=setup["bb"],
        session_id=setup["session_id"],
        out_dir=out_dir,
        auto_new_hand=bool(setup.get("auto_new_hand", False)),
        auto_winner=bool(setup.get("auto_winner", False)),
        rfid_folds=bool(setup.get("rfid_folds", False)),
        button_seat=setup.get("button_seat"),
    )
