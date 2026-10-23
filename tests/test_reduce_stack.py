"""tests/test_reduce_stack.py

スタックを減らす（CLI の `d <席> <金額>`, 2026-10-19）: ハンドの外ならすぐ減らす・0 より下にはしない・ハンドに入っている
席はそのハンドのあとに減らす（ハンドの記録は変えない）。記録（events.jsonl）からの再生でも同じ。
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

pytest.importorskip("pokerkit")

from audio.recognizer import parse_actions  # noqa: E402
from core.events import AudioEvent  # noqa: E402
from core.game_state import PlayerState  # noqa: E402
from integration.replay import replay_events  # noqa: E402
from main import _normalize_cli_command  # noqa: E402


@pytest.fixture(autouse=True)
def _quiet():
    logging.disable(logging.WARNING)
    yield
    logging.disable(logging.NOTSET)


def _say(text: str, t: float) -> list[AudioEvent]:
    events = parse_actions(text, confidence=1.0)
    for ev in events:
        ev.timestamp = ev.utterance_start_ts = t
    return events


def _hand(t: float) -> list:
    """全員フォールドで終わるハンド（1 ハンド目のボタン 席3: 席3・席1 が降りて BB の席2 の勝ち）。"""
    return [AudioEvent(action="new_hand", amount=0, timestamp=t, raw_text=""),
            *_say("フォールド", t + 1), *_say("フォールド", t + 2)]


def _reduce(seat: int, amount: int, t: float) -> AudioEvent:
    return AudioEvent(action="reduce_stack", amount=amount, timestamp=t,
                      raw_text=f"シート{seat} スタック減 {amount}", seat=seat)


def _run(events: list, tmp_path: Path) -> list[dict]:
    players = [PlayerState(seat=s, name=f"P{s}", stack=10000) for s in (1, 2, 3)]
    hands = replay_events(events, backend="pokerkit", players=players, sb=100, bb=200, session_id="s",
                          out_dir=tmp_path, auto_winner=True, button_seat=2, close_open_hand=True)
    return [h.to_dict() for h in hands]


def _starts(hand: dict) -> dict[int, int]:
    return {p["seat"]: p["stack_start"] for p in hand["players"]}


def test_reduce_between_hands(tmp_path):
    hands = _run([*_hand(10.0), _reduce(1, 3000, 20.0), *_hand(30.0)], tmp_path)
    end = {p["seat"]: p["stack_end"] for p in hands[0]["players"]}
    assert _starts(hands[1])[1] == end[1] - 3000
    assert _starts(hands[1])[2] == end[2]


def test_never_below_zero(tmp_path):
    hands = _run([*_hand(10.0), _reduce(3, 99999, 20.0), *_hand(30.0)], tmp_path)
    assert 3 not in _starts(hands[1])            # スタック 0 の席は配られない


def test_mid_hand_applies_after_the_hand(tmp_path):
    first = _hand(10.0)
    events = [*first[:2], _reduce(2, 1000, 11.5), *first[2:], *_hand(30.0)]
    hands = _run(events, tmp_path)
    one = {p["seat"]: (p["stack_start"], p["stack_end"]) for p in hands[0]["players"]}
    assert one[2] == (10000, 10100)              # そのハンドの記録は変わらない
    assert _starts(hands[1])[2] == 10100 - 1000


def test_glued_command():
    assert _normalize_cli_command("d3 500") == "d 3 500"
    assert _normalize_cli_command("ｄ　３　５００") == "d 3 500"
