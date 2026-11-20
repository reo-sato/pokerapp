"""端末の 1 行と iPad の制御を同じ処理にする（ADR-0073 D4, `integration/logger_commands.py`）。

同じ操作なら、端末から打っても iPad から送っても、積まれる AudioEvent（= events.jsonl の行）が同じになること。
名前（お客さんの記録に書く）・読み取り装置の再起動のような queue の外の副作用も、iPad から届くこと。
"""
from __future__ import annotations

import dataclasses
import queue
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.control_queue import ControlCommandLog
from integration.control_consumer import ControlConsumerThread
from integration.logger_commands import LoggerCommands, control_line


class _Integration:
    def __init__(self) -> None:
        self.maps: list[dict] = []

    def set_seat_player_map(self, m: dict) -> None:
        self.maps.append(dict(m))


class _Players:
    def __init__(self) -> None:
        self.made: dict[str, str] = {}

    def find_or_create(self, name: str):
        self.made.setdefault(name, f"pid-{name}")
        return SimpleNamespace(player_id=self.made[name])


class _Rfid:
    def __init__(self) -> None:
        self.restarts = 0

    def request_restart(self) -> None:
        self.restarts += 1


def _commands(**kw) -> LoggerCommands:
    base = dict(session_cfg={"players": [{"seat": s, "name": f"P{s}", "stack": 1000} for s in (1, 2, 3)]},
                game_state=SimpleNamespace(add_seat=lambda *a: None), integration_thread=_Integration(),
                rfid_thread=_Rfid(), session_repo=object(), player_repo=_Players(), clock=lambda: 100.0)
    base.update(kw)
    return LoggerCommands(**base)


def _shape(events) -> list[dict]:
    return [{k: v for k, v in dataclasses.asdict(e).items() if k != "timestamp"} for e in events]


CASES = [
    ("new_hand", {}, "n"),
    ("winner", {"seat": 2}, "w 2"),
    ("winner", {"seats": [1, 3]}, "w 1 3"),
    ("rebuy", {"seat": 1, "amount": 500}, "r 1 500"),
    ("reduce_stack", {"seat": 2, "amount": 200}, "d 2 200"),
    ("correct_board", {"index": 4}, "cb 4"),
    ("correct_seat", {"seat": 3}, "cs 3"),
    ("set_blinds", {"sb": 200, "bb": 400}, "blinds 200 400"),
    ("set_button", {"seat": 3}, "button 3"),
    ("seat_player", {"seat": 2, "name": "太郎"}, "name 2 太郎"),
    ("seat_player", {"seat": 2}, "name 2 -"),
    ("set_ante", {"value": "on"}, "ante on"),
    ("clock", {"op": "set 3 5"}, "clock set 3 5"),
    ("reader_restart", {}, "rr"),
    ("set_dealer", {"name": "佐藤"}, "dealer 佐藤"),
    ("set_dealer", {}, "dealer -"),
]


@pytest.mark.parametrize("ctype,args,line", CASES)
def test_the_control_records_the_same_as_the_terminal(ctype, args, line):
    assert control_line(ctype, args) == line
    terminal, ipad = _commands(), _commands()
    a, b = terminal.run_line(line), ipad.run_line(control_line(ctype, args))
    assert _shape(a.events) == _shape(b.events) and a.ok and b.ok


def test_bad_arguments_are_rejected_the_same_way():
    for ctype, args in [("winner", {}), ("rebuy", {"seat": 1, "amount": 0}), ("correct_board", {"index": 6}),
                        ("set_blinds", {"sb": 400, "bb": 200}), ("set_ante", {"value": "x"}),
                        ("clock", {"op": "rm -rf"}), ("seat_player", {"seat": 10, "name": "a"}),
                        ("set_dealer", {"name": "x" * 41})]:
        with pytest.raises(ValueError):
            control_line(ctype, args)
    assert control_line("sit_out", {"seat": 1}) is None          # 行の形が無い制御は今までの翻訳


def test_side_effects_reach_the_logger():
    c = _commands()
    r = c.run_line("name 2 太郎")
    assert c.player_repo.made == {"太郎": "pid-太郎"} and c.integration_thread.maps[-1] == {2: "pid-太郎"}
    assert [e.action for e in r.events] == ["rename_seat", "sit_in"]
    c.run_line("rr")
    assert c.rfid_thread.restarts == 1
    added = c.run_line("name 5 花子")                              # 卓に無い席は足す
    assert [e.action for e in added.events] == ["sit_in", "rename_seat"] and added.ok
    assert c.run_line("q").stop
    assert not c.run_line("あいうえお").ok
    assert not _commands(rfid_thread=None).run_line("rr").ok


def test_text_is_read_only_as_actions():
    c = _commands()
    assert [e.action for e in c.run_text("フォールド コール").events] == ["fold", "call"]
    assert not c.run_text("n").ok                                 # 打ち込みは「新ハンド」のコマンドとしては読まない


def test_the_consumer_uses_the_shared_commands(tmp_path: Path):
    log = ControlCommandLog(tmp_path / "s.control.jsonl")
    q: queue.Queue = queue.Queue()
    results: list = []
    consumer = ControlConsumerThread(log, q, threading.Event(), start_offset=0, commands=_commands(),
                                     on_result=lambda cmd, res: results.append((cmd.type, res.ok, res.messages)))
    log.append("seat_player", {"seat": 1, "name": "次郎"})
    log.append("text", {"text": "チェック"})
    log.append("rebuy", {"seat": 1, "amount": 0})                  # 使えない引数 → 未適用として知らせる
    log.append("sit_out", {"seat": 3})                             # 行の形が無い制御 → 今までの翻訳
    consumer.poll_once()
    actions = [q.get_nowait().action for _ in range(q.qsize())]
    assert actions == ["rename_seat", "sit_in", "check", "sit_out"]
    assert [(t, ok) for t, ok, _ in results] == [("seat_player", True), ("text", True), ("rebuy", False)]


def test_the_dealer_command():
    r = _commands().run_line("dealer 佐藤 花子")
    assert [(e.action, e.raw_text) for e in r.events] == [("set_dealer", "佐藤 花子")] and r.ok
    assert not _commands().run_line("dealer").ok
