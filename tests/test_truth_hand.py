"""tests/test_truth_hand.py

真のアクションを記録と同じ形にする（`core/truth_hand.py`）・お客さんの画面に真のアクションだけを見せる
（`viewer_api.show_truth`）・真のアクションの収支でロガーの持ち点を直す（制御 `truth_stacks`, 2026-10-19）。
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pytest

pytest.importorskip("pokerkit")

from audio.recognizer import parse_actions  # noqa: E402
from core.control_queue import (  # noqa: E402
    ControlCommandLog, command_to_audio_event, parse_truth_stacks, truth_stacks_text,
)
from core.events import AudioEvent  # noqa: E402
from core.game_state import PlayerState  # noqa: E402
from core.truth_hand import load_truth, materialize_truth, truth_view  # noqa: E402
from integration.replay import replay_events  # noqa: E402


@pytest.fixture(autouse=True)
def _quiet():
    logging.disable(logging.WARNING)
    yield
    logging.disable(logging.NOTSET)


BOARD = ["2c", "7d", "9h", "Tc", "3s"]


def _captured(stacks=None, results=None) -> dict:
    stacks = stacks or {1: 10000, 2: 10000, 3: 10000}
    return {
        "session_id": "s", "hand_id": 1, "started_at": "2026-10-19T19:00:00", "blinds": {"sb": 100, "bb": 200},
        "button_seat": 3, "board": BOARD,
        "players": [{"seat": s, "name": f"P{s}", "player_id": f"p{s}", "stack_start": v,
                     "result": (results or {}).get(s, 0)} for s, v in stacks.items()],
        "actions": [],
    }


def _checks_to_river(first: int, second: int) -> list[dict]:
    return [{"seat": s, "action": "check", "amount": 0} for _ in range(3) for s in (first, second)]


def _showdown_gt(**extra) -> dict:
    # ボタン 席3: プリフロップは席3 → 席1（SB）→ 席2（BB）、フロップからは席2 が先
    return {
        "actions": [{"seat": 3, "action": "raise", "amount": 600}, {"seat": 1, "action": "fold", "amount": 0},
                    {"seat": 2, "action": "call", "amount": 0}, *_checks_to_river(2, 3)],
        "players": [{"seat": 2, "hole_cards": ["As", "Ah"], "showed_down": True},
                    {"seat": 3, "hole_cards": ["Ks", "Kh"], "showed_down": True}],
        **extra,
    }


class TestMaterialize:
    def test_a_fold_out(self):
        gt = {"actions": [{"seat": 3, "action": "fold", "amount": 0}, {"seat": 1, "action": "fold", "amount": 0}]}
        hand = materialize_truth(gt, _captured())
        assert hand["truth"] is True and hand["winner_seat"] == 2 and hand["winner_source"] == "fold"
        assert {p["seat"]: p["result"] for p in hand["players"]} == {1: -100, 2: 100, 3: 0}
        assert [(a["seat"], a["action"]) for a in hand["actions"]] == [(3, "fold"), (1, "fold")]
        assert hand["players"][0]["player_id"] == "p1" and hand["actions"][0]["player_name"] == "P3"

    def test_the_cards_decide_the_showdown(self):
        hand = materialize_truth(_showdown_gt(), _captured())
        assert hand["winner_seat"] == 2 and hand["winner_source"] == "cards" and hand["pot_total"] == 1300
        assert {p["seat"]: p["result"] for p in hand["players"]} == {1: -100, 2: 700, 3: -600}
        call = hand["actions"][2]
        assert (call["street"], call["action"], call["amount"], call["pot_after"], call["stack_after"]) == (
            "preflop", "call", 400, 1300, 9400)
        assert [s["seat"] for s in hand["showdown"]] == [2, 3]

    def test_a_muck_loses_even_with_the_stronger_hand(self):
        gt = _showdown_gt()
        gt["actions"].append({"seat": 2, "action": "fold", "amount": 0})     # 見せずにマック
        hand = materialize_truth(gt, _captured())
        assert hand["winner_seat"] == 3 and hand["actions"][-1]["street"] == "showdown"
        assert {p["seat"]: p["result"] for p in hand["players"]}[3] == 700

    def test_side_pots_go_to_each_winner(self):
        gt = {
            "actions": [{"seat": 3, "action": "raise", "amount": 1000}, {"seat": 1, "action": "allin", "amount": 0},
                        {"seat": 2, "action": "call", "amount": 0}, *_checks_to_river(2, 3)],
            "players": [{"seat": 1, "hole_cards": ["As", "Ah"], "showed_down": True},
                        {"seat": 2, "hole_cards": ["Ks", "Kh"], "showed_down": True},
                        {"seat": 3, "hole_cards": ["Qs", "Qh"], "showed_down": True}],
        }
        hand = materialize_truth(gt, _captured({1: 300, 2: 10000, 3: 10000}))
        assert {p["seat"]: p["result"] for p in hand["players"]} == {1: 600, 2: 400, 3: -1000}

    def test_the_entered_winner_is_used_when_the_cards_are_not_known(self):
        gt = _showdown_gt(players=[], winner_seat=3)
        captured = _captured()
        hand = materialize_truth(gt, captured)
        assert hand["winner_seat"] == 3 and hand["winner_source"] == "truth"
        assert materialize_truth(_showdown_gt(players=[]), captured) is None     # 勝者が決まらない

    def test_an_unfinished_hand_is_not_shown(self):
        gt = {"actions": [{"seat": 3, "action": "raise", "amount": 600}]}
        assert materialize_truth(gt, _captured()) is None
        assert materialize_truth({"actions": [{"seat": 1, "action": "fold"}]}, _captured()) is None   # 手番違い


class TestViewer:
    def _write(self, tmp_path: Path, hands: list[dict]) -> Path:
        path = tmp_path / "s.ground_truth.json"
        path.write_text(json.dumps({"session_id": "s", "hands": hands}), encoding="utf-8")
        return path

    def test_only_hands_with_truth_are_shown(self, tmp_path):
        fold_out = {"hand_id": 1, "actions": [{"seat": 3, "action": "fold"}, {"seat": 1, "action": "fold"}]}
        self._write(tmp_path, [fold_out, {"hand_id": 2, "actions": [{"seat": 3, "action": "raise", "amount": 600}]}])
        live = [_captured(), dict(_captured(), hand_id=2), dict(_captured(), hand_id=3)]
        shown = truth_view(live, load_truth(tmp_path, "s"))
        assert [h["hand_id"] for h in shown] == [1] and shown[0]["winner_seat"] == 2

    def test_the_file_is_read_again_when_it_changes(self, tmp_path):
        path = self._write(tmp_path, [])
        assert load_truth(tmp_path, "s") == {}
        self._write(tmp_path, [{"hand_id": 1, "actions": []}])
        st = path.stat()
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000))
        assert list(load_truth(tmp_path, "s")) == [1]

    def test_the_viewer_api_shows_the_truth(self, tmp_path):
        pytest.importorskip("fastapi")
        from fastapi.testclient import TestClient

        from api.server import create_app
        from core.player_repository import PlayerRepository
        from core.session_repository import SessionRepository

        players = PlayerRepository(tmp_path / "players.json")
        session_repo = SessionRepository(tmp_path / "sessions.json", player_repo=players)
        a, b, c = (players.create_player(n).player_id for n in ("A", "B", "C"))
        sid = session_repo.create_session().session_id
        for seat, pid in ((1, a), (2, b), (3, c)):
            session_repo.assign_seat(sid, 1, seat, pid)
            session_repo.assign_seat(sid, 2, seat, pid)
        live = [dict(_captured(), session_id=sid), dict(_captured(), session_id=sid, hand_id=2)]
        (tmp_path / f"{sid}.json").write_text(json.dumps({"session_id": sid, "hands": live}), encoding="utf-8")
        gt = {"session_id": sid, "hands": [
            {"hand_id": 1, "actions": [{"seat": 3, "action": "fold"}, {"seat": 1, "action": "fold"}]}]}
        (tmp_path / f"{sid}.ground_truth.json").write_text(json.dumps(gt), encoding="utf-8")

        def client(show_truth: bool) -> TestClient:
            return TestClient(create_app(players, session_repo, tmp_path, show_truth=show_truth))

        on = client(True)
        hands = on.get(f"/api/players/{a}/sessions/{sid}/hands").json()["hands"]
        assert [h["hand_id"] for h in hands] == [1] and hands[0]["truth"] is True
        assert on.get(f"/api/sessions/{sid}/hands/1").json()["winner_seat"] == 2
        assert on.get(f"/api/sessions/{sid}/hands/2").status_code == 404
        off = client(False)
        assert len(off.get(f"/api/players/{a}/sessions/{sid}/hands").json()["hands"]) == 2


class TestTruthStacksControl:
    def test_text_round_trip(self):
        assert parse_truth_stacks(truth_stacks_text(4, {1: -100, 3: 250})) == (4, {1: -100, 3: 250})
        assert parse_truth_stacks("nonsense") == (None, {})

    def test_control_command_becomes_an_event(self, tmp_path):
        log = ControlCommandLog(tmp_path / "s.control.jsonl")
        cmd = log.append("truth_stacks", {"hand_id": 2, "results": {"1": -100, "2": 100}})
        ev = command_to_audio_event(cmd, lambda: 1.0)
        assert ev.action == "truth_stacks" and parse_truth_stacks(ev.raw_text) == (2, {1: -100, 2: 100})
        assert command_to_audio_event(log.append("truth_stacks", {"hand_id": "x", "results": {}}), lambda: 1.0) is None


def _say(text: str, t: float) -> list[AudioEvent]:
    events = parse_actions(text, confidence=1.0)
    for ev in events:
        ev.timestamp = ev.utterance_start_ts = t
    return events


def _truth(hand_id: int, results: dict[int, int], t: float) -> AudioEvent:
    return AudioEvent(action="truth_stacks", amount=0, timestamp=t, raw_text=truth_stacks_text(hand_id, results))


def _run(events: list, tmp_path: Path) -> list[dict]:
    players = [PlayerState(seat=s, name=f"P{s}", stack=10000) for s in (1, 2, 3)]
    hands = replay_events(events, backend="pokerkit", players=players, sb=100, bb=200, session_id="s",
                          out_dir=tmp_path, auto_winner=True, button_seat=2, close_open_hand=True)   # 1 ハンド目のボタン 席3
    return [h.to_dict() for h in hands]


def _live_hand_1(t: float = 10.0) -> list:
    # 記録: 席3 のレイズ 600、席1・席2 が降りる → 席3 の勝ち（+300）。真のアクションは席3・席1 が降りて席2 の勝ち
    return [AudioEvent(action="new_hand", amount=0, timestamp=t, raw_text=""),
            *_say("レイズ 600", t + 1), *_say("フォールド", t + 2), *_say("フォールド", t + 3)]


TRUTH_1 = {1: -100, 2: 100, 3: 0}


def _hand(t: float) -> list:
    """全員フォールドで終わるハンド（アクションの無いハンドは記録に残らない）。"""
    return [AudioEvent(action="new_hand", amount=0, timestamp=t, raw_text=""),
            *_say("フォールド", t + 1), *_say("フォールド", t + 2)]


def _starts(hand: dict) -> dict[int, int]:
    return {p["seat"]: p["stack_start"] for p in hand["players"]}


class TestEngineStacks:
    def test_the_next_hand_starts_from_the_truth(self, tmp_path):
        hands = _run([*_live_hand_1(), _truth(1, TRUTH_1, 20.0), *_hand(30.0)], tmp_path)
        assert {p["seat"]: p["result"] for p in hands[0]["players"]} == {1: -100, 2: -200, 3: 300}
        assert _starts(hands[1]) == {1: 9900, 2: 10100, 3: 10000}

    def test_truth_saved_before_the_hand_closed_is_applied_when_it_closes(self, tmp_path):
        live = _live_hand_1()
        events = [*live[:2], _truth(1, TRUTH_1, 11.5), *live[2:], *_hand(30.0)]
        assert _starts(_run(events, tmp_path)[1]) == {1: 9900, 2: 10100, 3: 10000}

    def test_saving_again_adds_only_the_difference(self, tmp_path):
        hands = _run([*_live_hand_1(), _truth(1, {1: -100, 2: -200, 3: 300}, 20.0), _truth(1, TRUTH_1, 21.0),
                      _truth(1, TRUTH_1, 22.0), *_hand(30.0)], tmp_path)
        assert _starts(hands[1]) == {1: 9900, 2: 10100, 3: 10000}

    def test_truth_arriving_mid_hand_waits_for_the_hand_to_end(self, tmp_path):
        events = [*_live_hand_1(), AudioEvent(action="new_hand", amount=0, timestamp=30.0, raw_text=""),
                  _truth(1, TRUTH_1, 31.0), *_say("フォールド", 32.0), *_say("フォールド", 33.0), *_hand(40.0)]
        hands = _run(events, tmp_path)
        second = {p["seat"]: (p["stack_start"], p["result"]) for p in hands[1]["players"]}
        assert second[3][0] == 10300                       # 2 ハンド目の記録は直す前の持ち点のまま
        third = _starts(hands[2])
        assert sum(third.values()) == 30000
        assert third == {s: start + result + {1: 0, 2: 300, 3: -300}[s] for s, (start, result) in second.items()}


class TestSendFromTheInputScreen:
    def test_saving_sends_the_results_to_the_logger(self, tmp_path):
        from tools.ground_truth_ui import send_truth_stacks

        gt = {"actions": [{"seat": 3, "action": "fold"}, {"seat": 1, "action": "fold"}]}
        assert send_truth_stacks(tmp_path, "s", 1, gt, _captured()) == {"1": -100, "2": 100, "3": 0}
        (cmd,), _ = ControlCommandLog(tmp_path / "s.control.jsonl").read_from(0)
        assert cmd.type == "truth_stacks" and cmd.args["hand_id"] == 1
        assert send_truth_stacks(tmp_path, "s", 2, {"actions": gt["actions"][:1]}, _captured()) is None


class TestTable:
    """真のアクションで席の有無・始めの持ち点を直す（`with_table`, 店舗 2026-10-19: 記録が席を誤った）。"""

    def test_no_table_fields_keeps_the_record(self):
        from core.truth_hand import with_table
        cap = _captured()
        assert with_table(cap, [{"seat": 1, "hole_cards": ["As", "Ah"]}]) is cap

    def test_absent_seat_and_added_seat(self):
        from core.truth_hand import with_table
        cap = _captured()
        out = with_table(cap, [{"seat": 2, "absent": True}, {"seat": 4, "name": "P4", "stack_start": 5000}])
        assert [(p["seat"], p["stack_start"]) for p in out["players"]] == [(1, 10000), (3, 10000), (4, 5000)]
        assert [p["seat"] for p in cap["players"]] == [1, 2, 3]        # 元は変えない

    def test_materialize_uses_the_table(self):
        # 席2 はいなかった: ボタン 席3 の 2 人 = 席3 が SB で先、席1 が BB
        gt = {"actions": [{"seat": 3, "action": "call", "amount": 0}, {"seat": 1, "action": "check", "amount": 0},
                          *_checks_to_river(1, 3)],
              "players": [{"seat": 2, "absent": True},
                          {"seat": 1, "stack_start": 3000, "hole_cards": ["As", "Ah"], "showed_down": True}],
              "winner_seat": 1}
        hand = materialize_truth(gt, _captured())
        assert hand is not None
        assert sorted(p["seat"] for p in hand["players"]) == [1, 3]
        start = {p["seat"]: p["stack_start"] for p in hand["players"]}
        assert start[1] == 3000
