"""tests/test_world_replay.py

推定器 v1 の再生器（`integration/world_replay.py`）: ライブの記録の席の信号（ライブのロガーがその場の解釈と
聞き取りの遅れの下で作ったもの）を捨て、札の読み取り・席の札の在否の履歴・発話（話し終わった時刻）だけから、
ライブと同じ engine でハンドを組み直す。解釈を変えて再生しても古い判断が残らない（店舗 9d1d8536 ハンド 4）。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from core.events import AudioEvent, RFIDEvent
from integration.world_replay import DERIVED_SIGNALS, PresenceTimeline, delivered_at, replay_world, world_events

pytest.importorskip("pokerkit")

STORE = Path(__file__).resolve().parent / "fixtures" / "store"


def _row(t: str, seats: list[tuple[int, bool, float | None, bool]], observed: float | None = None) -> dict:
    return {"updated_at": t, "observed_at": observed,
            "seats": [{"seat": s, "present": p, "away_sec": a, "mucked": m} for s, p, a, m in seats]}


class TestPresenceTimeline:
    def test_absences_and_the_clock_of_the_store_pc(self):
        base = datetime.fromisoformat("2026-09-29T18:12:04").timestamp()
        rows = [
            _row("2026-09-29T18:12:04.000", [(4, True, None, False), (7, False, None, False)], observed=base - 32400),
            _row("2026-09-29T18:12:10.000", [(4, False, 0.9, False), (7, False, None, False)]),
            _row("2026-09-29T18:12:11.000", [(4, False, 2.0, False), (7, False, None, False)]),   # 同じ不在（丸めの揺れ）
            _row("2026-09-29T18:12:12.000", [(4, False, 2.9, True), (7, False, None, False)]),    # 中央を通過
            _row("2026-09-29T18:12:20.000", [(4, True, None, False), (7, False, None, False)]),
        ]
        p = PresenceTimeline.from_table_state(rows)
        t0 = base - 32400                                  # 店舗 PC の現地時刻 → epoch（observed_at から）
        assert p.snapshot(t0 + 1)[4]["present"] is True
        assert 7 not in p.snapshot(t0 + 1)                  # 一度も載っていない席は出さない
        away = p.snapshot(t0 + 7)[4]                        # 18:12:10 の行で 0.9 秒前から離れていた
        assert (away["present"], round(away["absent_since"] - t0, 1)) == (False, 5.1)
        assert round(p.snapshot(t0 + 7.5)[4]["absent_since"] - t0, 1) == 5.1    # 次の行の揺れは同じ不在
        assert p.snapshot(t0 + 9)[4]["mucked_at"] == pytest.approx(t0 + 8)
        assert p.snapshot(t0 + 17)[4]["present"] is True
        assert p.departures(t0, t0 + 30) == [(4, pytest.approx(t0 + 5.1), pytest.approx(t0 + 16))]
        again = PresenceTimeline.from_rows(p.to_rows())
        assert again.snapshot(t0 + 9) == p.snapshot(t0 + 9)


class TestWorldEvents:
    def test_derived_signals_are_dropped_and_speech_is_reread(self):
        signal = RFIDEvent(tag_id="", card="", reader_id="", role="seat", seat=5, timestamp=20.0,
                           raw_tag_id="", kind="spoken_fold", observed_at=18.0)
        deal = RFIDEvent(tag_id="", card="", reader_id="", role="seat", seat=None, timestamp=5.0, raw_tag_id="",
                         kind="deal", observed_at=4.0, cards=("4:As", "4:Ad", "5:Ks", "5:Kd"))
        live = AudioEvent(action="call", amount=0, timestamp=25.0, raw_text="コール", utterance_start_ts=10.0)
        typed = AudioEvent(action="winner", amount=0, timestamp=30.0, raw_text="w 5", seat=5)
        rows = [{"utterance_start_ts": 10.0, "audio_sec": 1.5, "text": "フォールド、コール", "confidence": 0.9}]
        out = world_events([deal, signal, live, typed], rows)
        assert "spoken_fold" in DERIVED_SIGNALS and signal not in out and deal in out and typed in out
        speech = [e for e in out if isinstance(e, AudioEvent) and e.utterance_start_ts == 10.0]
        assert [e.action for e in speech] == ["fold", "call"]
        assert speech[0].timestamp == pytest.approx(delivered_at(rows[0]))    # 話し終わりに届く（遅れなし）
        assert [e.action for e in world_events([live], rows, {10.0: "コール"})] == ["call"]


def _load_store(prefix: str):
    from core.game_state import PlayerState
    from integration.replay import load_events

    folder = next(STORE.glob(f"*{prefix}"))
    expected = json.loads((folder / "expected.json").read_text(encoding="utf-8"))
    setup = expected["setup"]
    presence = PresenceTimeline.from_rows(
        json.loads(line) for line in (folder / "presence.jsonl").read_text(encoding="utf-8").splitlines() if line)
    transcripts = [json.loads(line) for line in (folder / "transcripts.jsonl").read_text(encoding="utf-8").splitlines()
                   if line]
    events = load_events(folder / "events.jsonl")
    players = [PlayerState(seat=p["seat"], name=p["name"], stack=p["stack"]) for p in setup["players"]]
    stacks = {int(h): {int(s): int(v) for s, v in seats.items()} for h, seats in setup["hand_stacks"].items()}
    kwargs = dict(players=players, sb=setup["sb"], bb=setup["bb"], session_id=expected["session_id"],
                  button_seat=setup.get("button_prior"), hand_stacks=stacks)
    return expected, events, transcripts, presence, kwargs


def test_the_button_can_be_changed_without_old_decisions(tmp_path):
    """店舗 9d1d8536 ハンド 4（ディーラーがボタンを動かし忘れた）: ボタンを直し、「チェック、チェック」を 1 つの
    チェックに読むと丸ごと正しくなる（記録の再生ではライブの判断「この『フォールド』は席5」が残り、合わなかった）。"""
    from tools.measure_capture_accuracy import hand_fully_correct

    expected, events, transcripts, presence, kwargs = _load_store("9d1d8536")
    start = next(r["utterance_start_ts"] for r in transcripts if (r.get("text") or "").startswith("チェック、チェック"))
    hands = {h["hand_id"]: h for h in replay_world(world_events(events, transcripts, {start: "チェック"}), presence,
                                                     hand_buttons={4: 5}, **kwargs)}
    truth = next(h["truth"] for h in expected["hands"] if h["hand_id"] == 4)
    assert hands[4]["button_seat"] == 5 and hand_fully_correct(truth, hands[4])


def test_store_sessions_are_rebuilt_from_the_raw_observations():
    """記録の席の信号を使わずに、店舗の真のアクションのある 18 ハンドのうち 12 ハンドが丸ごと正しい（記録の再生と
    同じ数。違いは 027e4b15 ハンド 1 の「フォールド、コール」= 聞き違いの 1 語）。"""
    from tools.measure_capture_accuracy import hand_fully_correct

    correct = 0
    for folder in sorted(STORE.glob("*")):
        if not (folder / "presence.jsonl").exists() or not (folder / "transcripts.jsonl").exists():
            continue
        expected, events, transcripts, presence, kwargs = _load_store(folder.name[-8:])
        flags = {k: bool(expected["setup"].get(k)) for k in ("auto_new_hand", "auto_winner", "rfid_folds")}
        hands = {h["hand_id"]: h for h in replay_world(world_events(events, transcripts), presence, **kwargs, **flags)}
        correct += sum(hand_fully_correct(h["truth"], hands.get(h["hand_id"])) for h in expected["hands"])
    assert correct >= 12
