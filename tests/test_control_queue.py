"""tests/test_control_queue.py

hand logger 遠隔制御の control-command queue（ADR-0039）の単体テスト。
append-only I/O・byte offset 読み・command_id 重複排除・AudioEvent 翻訳・consumer の
末尾シーク（過去コマンドを再実行しない）を検証する。録音スレッドは使わない（pure）。
"""
from __future__ import annotations

import queue
import threading
from pathlib import Path

from core.control_queue import (
    ControlCommandLog,
    command_to_audio_event,
)
from integration.control_consumer import ControlConsumerThread


def test_append_and_read_from_offset(tmp_path: Path):
    log = ControlCommandLog(tmp_path / "s.control.jsonl")
    assert log.end_offset() == 0
    c1 = log.append("new_hand")
    c2 = log.append("winner", {"seat": 3})
    cmds, off = log.read_from(0)
    assert [c.type for c in cmds] == ["new_hand", "winner"]
    assert cmds[1].args == {"seat": 3}
    assert off == log.end_offset()
    # 同じ offset から再読みは空（new_offset 以降に新規が無い）。
    cmds2, off2 = log.read_from(off)
    assert cmds2 == [] and off2 == off
    # append 後は新規分だけ返る。
    log.append("rebuy", {"seat": 1, "amount": 5000})
    cmds3, _ = log.read_from(off)
    assert [c.type for c in cmds3] == ["rebuy"]
    assert {c1.command_id, c2.command_id}  # ids ユニーク
    assert c1.command_id != c2.command_id


def test_read_from_ignores_partial_trailing_line(tmp_path: Path):
    p = tmp_path / "s.control.jsonl"
    log = ControlCommandLog(p)
    log.append("new_hand")
    # 末尾に未完の行（改行なし）を足す。
    with p.open("a", encoding="utf-8") as f:
        f.write('{"command_id":"x","type":"winner"')  # 不完全
    cmds, off = log.read_from(0)
    assert [c.type for c in cmds] == ["new_hand"]  # 完全な行のみ
    assert off < log.end_offset()  # 未完行は持ち越し


def test_command_to_audio_event_mapping():
    ControlCommandLog(Path("/dev/null"))  # append しないので path 未使用
    clk = lambda: 123.0  # noqa: E731
    nh = log_command("new_hand", {})
    ev = command_to_audio_event(nh, clk)
    assert ev.action == "new_hand" and ev.timestamp == 123.0

    win = log_command("winner", {"seat": 5})
    ev = command_to_audio_event(win, clk)
    assert ev.action == "winner" and ev.seat == 5 and "シート5" in ev.raw_text

    rb = log_command("rebuy", {"seat": 2, "amount": 3000})
    ev = command_to_audio_event(rb, clk)
    assert ev.action == "rebuy" and ev.seat == 2 and ev.amount == 3000

    # 不正 args / unknown は None。
    assert command_to_audio_event(log_command("winner", {}), clk) is None
    assert command_to_audio_event(log_command("rebuy", {"seat": 1, "amount": 0}), clk) is None
    assert command_to_audio_event(log_command("nope", {}), clk) is None


def log_command(type: str, args: dict):
    from core.control_queue import ControlCommand
    return ControlCommand(command_id="id-" + type, type=type, args=args, created_at="")


def test_consumer_starts_at_end_and_dedupes(tmp_path: Path):
    log = ControlCommandLog(tmp_path / "s.control.jsonl")
    # consumer 起動“前”のコマンドは再生しない（末尾シーク）。
    log.append("new_hand")
    aq: queue.Queue = queue.Queue()
    consumer = ControlConsumerThread(
        control_log=log, audio_queue=aq, stop_event=threading.Event(),
        clock=lambda: 0.0,
    )
    assert consumer.poll_once() == 0  # 過去分は消費しない
    assert aq.empty()

    # 起動後の新規コマンドだけ audio_queue に流れる。
    log.append("winner", {"seat": 1})
    log.append("rebuy", {"seat": 2, "amount": 1000})
    assert consumer.poll_once() == 2
    assert aq.get_nowait().action == "winner"
    assert aq.get_nowait().action == "rebuy"
    # 再 poll は重複なし。
    assert consumer.poll_once() == 0
