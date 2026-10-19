"""tests/test_bb_ante.py

BB アンティ（BB の人が BB と同額のアンティを出す, CLI の `ante on` / `ante off`, 2026-10-19）: エンジン・記録・再生・
真のアクションの再生・出した額の表示。
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

pytest.importorskip("pokerkit")

from audio.recognizer import parse_actions  # noqa: E402
from core.events import AudioEvent  # noqa: E402
from core.game_state import PlayerState  # noqa: E402
from core.hand_log import hand_street_flow  # noqa: E402
from core.poker_engine import PokerkitGameState  # noqa: E402
from core.truth_hand import materialize_truth  # noqa: E402
from integration.replay import replay_events  # noqa: E402


@pytest.fixture(autouse=True)
def _quiet():
    logging.disable(logging.WARNING)
    yield
    logging.disable(logging.NOTSET)


def _gs(n: int = 3) -> PokerkitGameState:
    return PokerkitGameState([PlayerState(seat=s, name=str(s), stack=10000) for s in range(1, n + 1)], 100, 200)


class TestEngine:
    def test_bb_pays_blind_and_ante(self):
        gs = _gs()
        gs.set_bb_ante(True)
        gs.new_hand()
        bb = next(s for s, p in gs.position_map().items() if p == "BB")
        assert gs.get_stack(bb) == 10000 - 200 - 200 and gs.pot == 500 and gs.bb_ante == 200
        assert gs.legal_context().amount_to_call == 200        # アンティは賭けに入らない

    def test_heads_up(self):
        gs = _gs(2)
        gs.set_bb_ante(True)
        gs.new_hand()
        bb = next(s for s, p in gs.position_map().items() if p == "BB")
        assert gs.get_stack(bb) == 9600 and gs.pot == 500

    def test_off_is_unchanged(self):
        gs = _gs()
        gs.new_hand()
        assert gs.pot == 300 and gs.bb_ante == 0

    def test_mid_hand_switch_waits_and_follows_the_bb(self):
        gs = _gs()
        gs.new_hand()
        gs.set_bb_ante(True)
        assert gs.bb_ante == 0 and gs.bb_ante_next_hand()
        gs.set_blinds(200, 400)
        gs.end_hand(gs.get_current_player())
        gs.new_hand()
        assert gs.bb_ante == 400 and gs.pot == 200 + 400 + 400


def _say(text: str, t: float) -> list[AudioEvent]:
    events = parse_actions(text, confidence=1.0)
    for ev in events:
        ev.timestamp = ev.utterance_start_ts = t
    return events


def _ante(choice: str, t: float) -> AudioEvent:
    return AudioEvent(action="set_bb_ante", amount=0, timestamp=t, raw_text=choice)


def _hand(t: float, say: str = "コール コール チェック") -> list:
    return [AudioEvent(action="new_hand", amount=0, timestamp=t, raw_text=""), *_say(say, t + 1)]


def _run(events: list, tmp_path: Path) -> list[dict]:
    players = [PlayerState(seat=s, name=f"P{s}", stack=10000) for s in (1, 2, 3)]
    hands = replay_events(events, backend="pokerkit", players=players, sb=100, bb=200, session_id="s",
                          out_dir=tmp_path, auto_winner=True, button_seat=2, close_open_hand=True)
    return [h.to_dict() for h in hands]


class TestRecord:
    def test_record_carries_the_ante_and_the_bb_total(self, tmp_path):
        events = [_ante("on", 5.0), *_hand(10.0), AudioEvent(action="winner", amount=0, timestamp=15.0,
                                                               raw_text="シート1 ウィナー", seat=1)]
        (hand,) = _run(events, tmp_path)
        assert hand["blinds"] == {"sb": 100, "bb": 200, "bb_ante": 200}
        assert hand["pot_total"] == 800                        # 200 × 3 + アンティ 200
        bb = int(next(s for s, p in hand["position_map"].items() if p == "BB"))
        flow = dict(zip([(a["seat"], a["action"]) for a in hand["actions"]], hand_street_flow(hand)))
        assert flow[(bb, "check")][1] == 200                   # BB の出した額はアンティを含まない
        result = {p["seat"]: p["result"] for p in hand["players"]}
        assert result[bb] == -400 and sum(result.values()) == 0

    def test_off_again(self, tmp_path):
        events = [_ante("on", 5.0), *_hand(10.0, "フォールド フォールド"), _ante("off", 20.0),
                  *_hand(30.0, "フォールド フォールド")]
        first, second = _run(events, tmp_path)
        assert first["blinds"].get("bb_ante") == 200 and "bb_ante" not in second["blinds"]

    def test_truth_replay_uses_the_recorded_ante(self, tmp_path):
        events = [_ante("on", 5.0), *_hand(10.0, "フォールド フォールド")]
        (hand,) = _run(events, tmp_path)
        order = [a["seat"] for a in hand["actions"]]
        gt = {"actions": [{"seat": s, "action": "fold", "amount": 0} for s in order]}
        truth = materialize_truth(gt, hand)
        assert {p["seat"]: p["result"] for p in truth["players"]} == {
            p["seat"]: p["result"] for p in hand["players"]}
