"""tests/test_test_script.py

台本のハンド（`tools/test_script.py`, テスト方針 週 1）: 台本の生成・台本の行をそのまま読んだときにエンジンが台本の
正解どおりに記録すること・制御 `script_hand`（台本のボタンと持ち点で始める）・画面（真のアクション入力の `/script`）・
台本を真のアクションにした評価（`tools/eval_store.py`）。
"""
from __future__ import annotations

import json
import logging
import threading
import urllib.request
from pathlib import Path

import pytest

from audio.recognizer import parse_actions
from core.control_queue import ControlCommandLog, command_to_audio_event, parse_script_hand, script_hand_text
from core.events import AudioEvent
from core.game_state import PlayerState
from core.poker_engine import PokerkitGameState
from integration.control_consumer import ControlConsumerThread
from integration.replay import replay_events
from output.event_recorder import EventRecorder
from tools import test_script as ts
from tools.ground_truth_ui import make_server, replay_legal
from tools.measure_capture_accuracy import measure_hand


@pytest.fixture(autouse=True)
def _quiet():
    logging.disable(logging.WARNING)
    yield
    logging.disable(logging.NOTSET)


def _events_for(script: dict, start: float = 1_000_000.0, only: list[int] | None = None) -> tuple[list, list[dict]]:
    """台本を完璧に読んだときの入力（ハンドの開始 = 画面の制御 + 各行を読み取りに通した発話）と、画面の操作の記録。"""
    events, marks, t = [], [], start
    for hand in script["hands"]:
        if only is not None and hand["n"] not in only:
            continue
        marks.append({"t": t - 0.2, "event": "start", "hand": hand["n"]})
        stacks = {int(s): v for s, v in hand["stacks"].items()}
        events.append(AudioEvent(action="script_hand", amount=0, timestamp=t,
                                 raw_text=script_hand_text(hand["button"], stacks)))
        t += 1.0
        for line in hand["lines"]:
            for ev in parse_actions(line["say"], confidence=1.0):
                ev.timestamp = ev.utterance_start_ts = t
                events.append(ev)
                t += 0.01
            t += 1.0
        t += 5.0
    return events, marks


def _replay(script: dict, events: list, out_dir: Path, sid: str = "s") -> list[dict]:
    table = ts.session_table(script)
    players = [PlayerState(seat=p["seat"], name=p["name"], stack=p["stack"]) for p in table["players"]]
    hands = replay_events(events, backend="pokerkit", players=players, sb=table["sb"], bb=table["bb"],
                          session_id=sid, out_dir=out_dir, auto_winner=True, button_seat=table["button_seat"],
                          close_open_hand=True)
    return [h.to_dict() for h in hands]


class TestGenerator:
    def test_same_seed_same_script(self):
        a = ts.generate_voice_script(7, hands=6, seats=6)
        b = ts.generate_voice_script(7, hands=6, seats=6)
        assert a["hands"] == b["hands"] and a["table"] == b["table"]
        assert a["hands"] != ts.generate_voice_script(8, hands=6, seats=6)["hands"]

    def test_button_moves_and_stacks_carry_over(self):
        script = ts.generate_voice_script(3, hands=8, seats=6)
        hands = script["hands"]
        assert [h["button"] for h in hands[:3]] == [6, 1, 2]
        for prev, cur in zip(hands, hands[1:]):
            for seat, stack in cur["stacks"].items():
                assert stack == (prev["end_stacks"][seat] or ts.BASE_STACK)
        assert all(h["lines"] or not h["actions"] for h in hands)

    def test_rare_scenarios_appear(self):
        script = ts.generate_voice_script(1, hands=120, seats=6)
        scenarios = {h["scenario"] for h in script["hands"]}
        assert {"allin", "side_pot", "showdown_muck", "chop", "multiway", "heads_up", "normal"} <= scenarios
        says = [ln["say"] for h in script["hands"] for ln in h["lines"]]
        assert "チェックアラウンド" in says and "ヘッズアップ" in says and "オールイン" in says
        assert any("チョップ" in s for s in says) and any("、" in s for s in says)
        assert any(s.startswith(("ボタン ", "スモール ", "ビッグ ", "カットオフ ", "UTG ")) for s in says)

    def test_truth_rows_are_what_the_gt_screen_would_make(self):
        """台本の正解の行は、真のアクション入力の画面（`replay_legal`）と同じ形（コールは追加額・オールインの額）。"""
        script = ts.generate_voice_script(5, hands=25, seats=6)
        for hand in script["hands"]:
            captured = {"players": [{"seat": int(s), "stack_start": v} for s, v in hand["stacks"].items()],
                        "blinds": {"sb": ts.SB, "bb": ts.BB}, "button_seat": hand["button"]}
            rows = [a for a in hand["actions"] if a["street"] != "showdown"]
            got = replay_legal(captured, rows)
            assert got["error"] is None, (hand["n"], got["error"])
            assert [(a["street"], a["seat"], a["action"], a["amount"]) for a in got["actions"]] == \
                   [(a["street"], a["seat"], a["action"], a["amount"]) for a in rows]

    @pytest.mark.parametrize("seats", [4, 6, 9])
    def test_reading_the_script_records_the_truth(self, tmp_path, seats):
        """台本の行を聞き違いなしで読んだら、記録は台本の正解と完全に一致する（台本と読み取り・エンジンの約束ごとの検査）。"""
        script = ts.generate_voice_script(11, hands=25, seats=seats)
        events, _ = _events_for(script)
        hands = _replay(script, events, tmp_path)
        assert len(hands) == len(script["hands"])
        for spec, got in zip(script["hands"], hands):
            gt = {"hand_id": got["hand_id"], "actions": spec["actions"], "winner_seat": spec["winner_seat"], "board": []}
            acc = measure_hand(gt, got)
            assert acc.action_correct == acc.action_total and acc.winner_match, (spec["n"], spec["scenario"])
            assert got["button_seat"] == spec["button"]

    def test_hand_start_uses_the_script_table_even_after_a_misread_hand(self, tmp_path):
        """聞き違いで前のハンドの持ち点・ボタンがずれても、次の台本のハンドは台本の持ち点・ボタンで始まる。"""
        script = ts.generate_voice_script(2, hands=3, seats=6)
        events, _ = _events_for(script)
        # ハンド 1 の最初の発話を「オールイン」に聞き違えた
        first = next(i for i, e in enumerate(events) if e.action != "script_hand")
        t = events[first].timestamp
        events[first] = AudioEvent(action="allin", amount=0, timestamp=t, raw_text="オールイン", confidence=1.0,
                                   utterance_start_ts=t)
        hands = _replay(script, events, tmp_path)
        second = next(h for h in hands if h["hand_id"] == 2)
        assert second["button_seat"] == script["hands"][1]["button"]
        assert {p["seat"]: p["stack_start"] for p in second["players"]} == \
               {int(s): v for s, v in script["hands"][1]["stacks"].items()}


class TestScriptHandControl:
    def test_text_round_trip(self):
        assert parse_script_hand(script_hand_text(6, {1: 20000, 2: 400})) == (6, {1: 20000, 2: 400})
        assert parse_script_hand("nonsense") == (None, {})

    def test_control_command_becomes_an_event(self, tmp_path):
        log = ControlCommandLog(tmp_path / "s.control.jsonl")
        cmd = log.append("script_hand", {"button": 3, "stacks": {"1": 5000, "3": 7000}})
        ev = command_to_audio_event(cmd, lambda: 12.0)
        assert ev.action == "script_hand" and parse_script_hand(ev.raw_text) == (3, {1: 5000, 3: 7000})
        bad = log.append("script_hand", {"button": "x", "stacks": {}})
        assert command_to_audio_event(bad, lambda: 12.0) is None

    def test_consumer_waits_for_speech_in_progress(self, tmp_path):
        """画面の「次のハンドを始める」は、聞き取り中の発話（前のハンドの最後のアクション）を追い越さない。"""
        import queue

        log = ControlCommandLog(tmp_path / "s.control.jsonl")
        q: queue.Queue = queue.Queue()
        pending = {"n": 1}
        consumer = ControlConsumerThread(log, q, threading.Event(), start_offset=0,
                                         backlog=lambda: pending["n"], backlog_wait_sec=5.0)
        log.append("script_hand", {"button": 1, "stacks": {"1": 100, "2": 100}})
        timer = threading.Timer(0.3, lambda: pending.update(n=0))
        timer.start()
        import time

        started = time.monotonic()
        assert consumer.poll_once() == 1
        assert time.monotonic() - started >= 0.25 and q.get_nowait().action == "script_hand"

    def test_forced_stacks_win_over_the_open_hand_result(self):
        gs = PokerkitGameState([PlayerState(seat=s, name=str(s), stack=1000) for s in (1, 2, 3)], 10, 20)
        gs.new_hand()
        gs.force_next_stacks({1: 500, 2: 700, 3: 900})
        gs.end_hand(gs.get_current_player())              # 前のハンドの結果（持ち点が動く）
        gs.new_hand()
        start = {s: gs._hand_start_stacks[gs._seat_to_idx[s]] for s in (1, 2, 3)}   # noqa: SLF001
        assert start == {1: 500, 2: 700, 3: 900}
        with pytest.raises(ValueError):
            gs.force_next_stacks({7: 100})

    def test_forced_stacks_are_kept_when_new_hand_fails(self):
        gs = PokerkitGameState([PlayerState(seat=s, name=str(s), stack=1000) for s in (1, 2)], 10, 20)
        gs.force_next_stacks({1: 0, 2: 0})
        with pytest.raises(ValueError):
            gs.new_hand()
        assert gs.get_stacks() == {1: 1000, 2: 1000}


def _session_dir(tmp_path: Path, script: dict, sid: str = "2026-10-01_190000_script_voice") -> tuple[Path, str]:
    logs = tmp_path / "logs"
    logs.mkdir()
    ts.write_session_script(logs, sid, script)
    return logs, sid


class TestPage:
    def _serve(self, logs: Path):
        server = make_server(logs, "127.0.0.1", 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server, f"http://127.0.0.1:{server.server_address[1]}"

    def _req(self, base, method, path, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(base + path, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if "json" in r.headers.get("Content-Type", "") else raw.decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_no_script_session(self, tmp_path):
        (tmp_path / "logs").mkdir()
        app = ts.ScriptApp(tmp_path / "logs")
        assert app.state() == {"session": None}
        assert app.act("start", {"hand": 1})[0] == 409

    def test_voice_start_sends_the_hand_to_the_logger(self, tmp_path):
        script = ts.generate_voice_script(4, hands=3, seats=6)
        logs, sid = _session_dir(tmp_path, script)
        server, base = self._serve(logs)
        try:
            status, page = self._req(base, "GET", "/script")
            assert status == 200 and "台本のハンド" in page
            status, st = self._req(base, "GET", "/api/script/state")
            assert status == 200 and st["session"] == sid and st["kind"] == "voice" and st["current"] is None
            assert len(st["hands"]) == 3 and st["recorded_hands"] == 0
            status, st = self._req(base, "POST", "/api/script/start", {"hand": 2})
            assert status == 200 and st["current"] == 2 and st["attempts"] == {"2": 1}
            status, st = self._req(base, "POST", "/api/script/start", {"hand": 2, "redo": True})
            assert st["attempts"] == {"2": 2}
            assert self._req(base, "POST", "/api/script/start", {"hand": 9})[0] == 400
            assert self._req(base, "POST", "/api/script/line", {"hand": 2, "line": 1})[0] == 200
            assert self._req(base, "POST", "/api/script/end", {})[1]["ended"]
        finally:
            server.shutdown()
            server.server_close()
        commands, _ = ControlCommandLog(logs / f"{sid}.control.jsonl").read_from(0)
        assert [c.type for c in commands] == ["script_hand", "script_hand"]
        spec = script["hands"][1]
        assert commands[0].args == {"button": spec["button"], "stacks": {s: v for s, v in spec["stacks"].items()}}
        marks = ts.read_marks(logs / f"{sid}{ts.MARKS_SUFFIX}")
        assert [m["event"] for m in marks] == ["start", "start", "line", "end"] and marks[1]["redo"]

    def test_cards_steps(self, tmp_path):
        logs, sid = _session_dir(tmp_path, ts.cards_script(), "2026-10-01_190000_script_cards")
        app = ts.ScriptApp(logs)
        st = app.state()
        assert st["kind"] == "cards" and len(st["steps"]) == len(ts.CARDS_STEPS) and st["steps_done"] == {}
        assert app.act("start", {"hand": 1})[0] == 400
        assert app.act("step", {"step": "nope", "result": "ok"})[0] == 400
        app.act("step", {"step": "deal_order", "result": "start"})
        st = app.act("step", {"step": "deal_order", "result": "ng", "note": "ボタンが 4 のまま"})[1]
        assert st["steps_done"] == {"deal_order": "ng"}
        steps = ts.script_steps(ts.read_json(logs / f"{sid}{ts.SCRIPT_SUFFIX}"),
                                ts.read_marks(logs / f"{sid}{ts.MARKS_SUFFIX}"))
        assert steps == [{"id": "deal_order", "title": ts.CARDS_STEPS[0]["title"], "result": "ng",
                          "note": "ボタンが 4 のまま", "t": steps[0]["t"]}]

    def test_session_table_matches_the_cli_setup(self):
        table = ts.session_table(ts.cards_script())
        assert [p["seat"] for p in table["players"]] == [4, 5, 6] and table["button_seat"] == 5
        assert all(not p["named"] for p in table["players"]) and (table["sb"], table["bb"]) == (100, 200)


class TestEvaluation:
    def test_script_is_the_truth_and_redone_hands_are_left_out(self, tmp_path):
        from tools.eval_store import SessionFiles, evaluate_session

        script = ts.generate_voice_script(9, hands=4, seats=6)
        logs, sid = _session_dir(tmp_path, script)
        events, marks = _events_for(script)
        # ハンド 2 を途中までやってやり直した（同じハンドをもう一度始めた）
        i = next(k for k, e in enumerate(events) if e.action == "script_hand" and k > 0)
        t2 = events[i].timestamp
        tried = [AudioEvent(action="script_hand", amount=0, timestamp=t2 - 4.0, raw_text=events[i].raw_text)]
        for ev in parse_actions(script["hands"][1]["lines"][0]["say"], confidence=1.0):
            ev.timestamp = ev.utterance_start_ts = t2 - 3.0
            tried.append(ev)
        events[i:i] = tried
        marks.append({"t": t2 - 4.2, "event": "start", "hand": 2})
        marks.sort(key=lambda m: m["t"])
        rec = EventRecorder(logs / f"{sid}.events.jsonl")
        for e in events:
            rec.record(e)
        for m in marks:
            ts.append_mark(logs / f"{sid}{ts.MARKS_SUFFIX}", m)
        _replay(script, events, logs, sid)
        report = evaluate_session(SessionFiles(sid, logs), {}, None)
        assert report.script["kind"] == "voice" and report.script["redone"] == 1
        assert report.script["used"] == [1, 2, 3, 4] and report.script["unmatched"] == []
        assert report.truth["record"]["action_accuracy"] == 1.0
        assert report.truth["record"]["winner_accuracy"] == 1.0
