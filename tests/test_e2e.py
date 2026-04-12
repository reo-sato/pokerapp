"""tests/test_e2e.py

E2E hand scenarios: AudioEvent → IntegrationThread → GameStateManager → ActionRecord.

Turn order follows Phase 1 round-robin (lowest seat first, simple rotation).
folded_seats / all_in_seats are derived from the captured ActionRecord list.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from core.event_queue import make_audio_queue
from core.events import AudioEvent
from core.game_state import GameStateManager, PlayerState
from core.hand_log import ActionRecord
from integration.engine import IntegrationThread
from output.json_writer import JsonWriter


# ――― ヘルパー ―――

def _run_events(
    players: list[PlayerState],
    events: list[AudioEvent],
    tmp_path: Path,
    session_id: str,
    *,
    sleep: float = 0.5,
) -> list[ActionRecord]:
    """IntegrationThread を起動してイベントを流し、キャプチャした ActionRecord を返す。

    GameStateManager は new_hand() を呼ばずに渡す。
    "new_hand" AudioEvent が _start_new_hand() を呼ぶことで手が開始される。
    """
    gs = GameStateManager(players=players, sb=100, bb=200)
    audio_q = make_audio_queue()
    writer = JsonWriter(log_dir=tmp_path, session_id=session_id)
    stop = threading.Event()
    captured: list[ActionRecord] = []

    thread = IntegrationThread(
        audio_queue=audio_q,
        game_state=gs,
        json_writer=writer,
        on_action=captured.append,
        stop_event=stop,
    )

    thread.start()
    time.sleep(0.05)  # スレッド起動待ち

    for ev in events:
        audio_q.put(ev)

    time.sleep(sleep)
    stop.set()
    thread.join(timeout=3.0)

    return captured


# ――― シナリオ1: フォールドなし ―――

def test_full_hand_no_fold(tmp_path: Path) -> None:
    """2人テーブル、フォールドなし。seat1 がベット、seat2 がコール、seat1 勝利。

    ラウンドロビン:
      new_hand → active=[1,2], turn_idx=0
      bet(500) → seat1, advance → seat2
      call(500)→ seat2, advance → seat1
      winner   → finalize_hand(1)
    """
    players = [
        PlayerState(seat=1, name="P1", stack=10000),
        PlayerState(seat=2, name="P2", stack=10000),
    ]
    now = time.time()
    events = [
        AudioEvent("new_hand", 0,   now,        ""),
        AudioEvent("bet",      500, now + 0.01, ""),   # seat1
        AudioEvent("call",     500, now + 0.02, ""),   # seat2
        AudioEvent("winner",   0,   now + 0.03, "シート1 ウィナー"),
    ]

    captured = _run_events(players, events, tmp_path, "e2e_no_fold")

    # bet と call の 2 アクションのみ記録される（new_hand / winner は ActionRecord を生成しない）
    assert len(captured) == 2
    assert all(not r.needs_review for r in captured)
    folded_seats = {r.seat for r in captured if r.action == "fold"}
    assert len(folded_seats) == 0


# ――― シナリオ2: フォールドあり（needs_review=False を維持）―――

def test_with_folds(tmp_path: Path) -> None:
    """6人テーブル、button_seat=1。フォールドあり。

    ラウンドロビン（Phase 1: 単純昇順）による実行順序:
      new_hand → active=[1,2,3,4,5,6], turn_idx=0

      [Preflop]
      bet(600)  → seat1, advance → turn_idx=1 (seat2)
      fold      → seat2, active=[1,3,4,5,6], idx=1, 1>1? No → turn_idx=1 (seat3)
      fold      → seat3, active=[1,4,5,6],   idx=1, 1>1? No → turn_idx=1 (seat4)
      call(600) → seat4, advance → turn_idx=2 (seat5)
      call(600) → seat5, advance → turn_idx=3 (seat6)
      call(600) → seat6, advance → turn_idx=0 (seat1)

      [Flop 相当: ストリートは PREFLOP のまま進行]
      bet(400)  → seat1, advance → turn_idx=1 (seat4)
      call(400) → seat4, advance → turn_idx=2 (seat5)
      fold      → seat5, active=[1,4,6], idx=2, 2>2? No → turn_idx=2 (seat6)
      fold      → seat6, active=[1,4],   idx=2, 2>2? No → turn_idx=2%2=0 (seat1)

      winner    → finalize_hand(4)

    アサート: folded_seats に seat2, seat3, seat5, seat6 が含まれる
    """
    players = [PlayerState(seat=i, name=f"P{i}", stack=10000) for i in range(1, 7)]
    now = time.time()
    events = [
        AudioEvent("new_hand", 0,   now,        ""),
        # Preflop
        AudioEvent("bet",  600, now + 0.01, ""),   # seat1 (UTG 相当)
        AudioEvent("fold", 0,   now + 0.02, ""),   # seat2
        AudioEvent("fold", 0,   now + 0.03, ""),   # seat3
        AudioEvent("call", 600, now + 0.04, ""),   # seat4
        AudioEvent("call", 600, now + 0.05, ""),   # seat5
        AudioEvent("call", 600, now + 0.06, ""),   # seat6
        # Flop 相当
        AudioEvent("bet",  400, now + 0.07, ""),   # seat1
        AudioEvent("call", 400, now + 0.08, ""),   # seat4
        AudioEvent("fold", 0,   now + 0.09, ""),   # seat5
        AudioEvent("fold", 0,   now + 0.10, ""),   # seat6
        # Winner
        AudioEvent("winner", 0, now + 0.11, "シート4 ウィナー"),
    ]

    captured = _run_events(players, events, tmp_path, "e2e_folds", sleep=0.8)

    assert all(not r.needs_review for r in captured)
    folded_seats = {r.seat for r in captured if r.action == "fold"}
    assert 2 in folded_seats
    assert 3 in folded_seats
    assert 5 in folded_seats
    assert 6 in folded_seats


# ――― シナリオ3: オールイン ―――

def test_all_in(tmp_path: Path) -> None:
    """3人テーブル、button_seat=1。seat2 がオールイン。

    ラウンドロビン:
      new_hand → active=[1,2,3], turn_idx=0

      fold         → seat1, active=[2,3], idx=0, 0>0? No → turn_idx=0 (seat2)
      allin(10000) → seat2, advance → turn_idx=1 (seat3)
      call(10000)  → seat3, advance → turn_idx=0 (seat2)

      winner       → finalize_hand(2)

    アサート: all_in_seats に seat2 が含まれる、needs_review=False
    """
    players = [PlayerState(seat=i, name=f"P{i}", stack=10000) for i in range(1, 4)]
    now = time.time()
    events = [
        AudioEvent("new_hand", 0,     now,        ""),
        AudioEvent("fold",     0,     now + 0.01, ""),    # seat1
        AudioEvent("allin",    10000, now + 0.02, ""),    # seat2
        AudioEvent("call",     10000, now + 0.03, ""),    # seat3
        AudioEvent("winner",   0,     now + 0.04, "シート2 ウィナー"),
    ]

    captured = _run_events(players, events, tmp_path, "e2e_allin")

    assert all(not r.needs_review for r in captured)
    all_in_seats = {r.seat for r in captured if r.action == "allin"}
    assert 2 in all_in_seats
