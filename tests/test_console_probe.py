"""RDP の外でマイクを使えるかを測る道具（`tools/console_probe.py`, ADR-0073 段階 0）。

店舗 PC の Windows でしか本物は測れないので、ここではセッション・マイク・時計を差し替えて、記録の形と、まとめ
（状態の区間・声・開き直し・固まった・子のプロセスが残ったか）を確かめる。
"""
from __future__ import annotations

import json
from pathlib import Path

from tools import console_probe as cp

CONSOLE = {"session": 1, "console": True, "remote": False, "state": "active"}
RDP = {"session": 1, "console": False, "remote": True, "state": "active"}
GONE = {"session": 1, "console": False, "remote": False, "state": "disconnected"}


class Clock:
    def __init__(self) -> None:
        self.t = 1_800_000_000.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, sec: float) -> None:
        self.t += sec


class FakeMic:
    """時刻で振る舞いを変えるマイク: plan(t) が "voice" / "quiet" / "fail" / "closed" を返す。"""

    def __init__(self, clock: Clock, plan) -> None:
        self.clock, self.plan = clock, plan
        self.name, self.error = None, None

    def open(self) -> bool:
        if self.plan(self.clock()) == "closed":
            self.error = "OSError: [Errno -9996] Invalid input device"
            return False
        self.name = "Wireless Mic Rx"
        return True

    def read(self) -> bytes:
        self.clock.sleep(0.1)
        mode = self.plan(self.clock())
        if mode in ("fail", "closed"):
            raise OSError("[Errno -9999] Unanticipated host error")
        sample = 2000 if mode == "voice" else 10
        return int(sample).to_bytes(2, "little", signed=True) * cp.CHUNK

    def close(self) -> None:
        pass


def _run(tmp_path: Path, plan, where, seconds: float = 60.0) -> list[dict]:
    clock = Clock()
    start = clock()
    out = tmp_path / "probe.jsonl"
    cp.run_probe(seconds, out, mic=FakeMic(clock, lambda t: plan(t - start)), sessions=lambda: where(clock() - start),
                 devices=lambda: {"inputs": 3, "wdm_ks_only": False, "chosen": "Wireless Mic Rx"},
                 clock=clock, sleep=clock.sleep)
    return [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]


def test_rows_every_five_seconds_with_session_and_voice(tmp_path):
    rows = _run(tmp_path, lambda t: "voice" if 11 <= t < 19 else "quiet", lambda t: CONSOLE, seconds=30)
    assert rows[0]["kind"] == "start" and rows[-1]["kind"] == "end"
    body = [r for r in rows if r["kind"] == "row"]
    assert len(body) == 6 and all(r["open"] and r["session"] == CONSOLE for r in body)
    assert [bool(r["voiced"]) for r in body] == [False, False, True, True, False, False]
    assert body[2]["rms_max"] == 2000


def test_summary_splits_by_session_and_shows_where_the_mic_stopped(tmp_path):
    """コンソール（声あり）→ RDP（声あり）→ 切断（マイクが切れて開けない）を、区間ごとに 1 行で出す。"""
    where = lambda t: CONSOLE if t < 20 else RDP if t < 40 else GONE        # noqa: E731
    plan = lambda t: "closed" if t >= 41 else "voice"                         # noqa: E731
    lines = cp.summarize(_run(tmp_path, plan, where, seconds=60))
    joined = "\n".join(lines)
    assert "コンソール（RDP なし）: マイクを開けている（Wireless Mic Rx）・声あり" in joined
    assert "RDP でつないでいる: マイクを開けている（Wireless Mic Rx）・声あり" in joined
    assert "切断（RDP を閉じたまま）: マイクを開けない（OSError: [Errno -9996] Invalid input device）" in joined
    assert lines[-1].startswith("終了")


def test_a_broken_read_is_reopened_like_the_logger(tmp_path):
    plan = lambda t: "fail" if 10 <= t < 10.15 else "voice"                   # noqa: E731
    rows = _run(tmp_path, plan, lambda t: CONSOLE, seconds=30)
    body = [r for r in rows if r["kind"] == "row"]
    assert sum(r["read_errors"] for r in body) == 1 and sum(r["reopened"] for r in body) == 1
    assert "開き直し 1" in "\n".join(cp.summarize(rows))


def test_summary_reports_a_stall_a_crash_and_the_child(tmp_path):
    t0 = 1_800_000_000.0
    rows = [{"t": t0, "kind": "start", "session": CONSOLE, "stdin": False, "stderr": False},
            {"t": t0 + 5, "kind": "row", "session": CONSOLE, "open": True, "mic": "M", "rms_max": 50, "voiced": 0},
            {"t": t0 + 10, "kind": "stall", "since_read": 6.0, "session": GONE},
            {"t": t0 + 15, "kind": "stall", "since_read": 11.0, "session": GONE}]
    child = [{"t": t0, "kind": "child_start", "pid": 2, "parent": 1},
             {"t": t0 + 5, "kind": "child", "parent_alive": True},
             {"t": t0 + 20, "kind": "child", "parent_alive": False},
             {"t": t0 + 25, "kind": "child", "parent_alive": False}]
    joined = "\n".join(cp.summarize(rows, child))
    assert "標準入力 なし・標準エラー なし" in joined
    assert "録音の読み取りが返らなかった" in joined and "2 回" in joined
    assert "記録は" in joined and "で止まった" in joined
    assert "本体を止めても子は残る" in joined
    crashed = rows[:2] + [{"t": t0 + 6, "kind": "crash", "error": "Traceback ... ValueError"}]
    assert "落ちた" in "\n".join(cp.summarize(crashed))


def test_report_reads_every_probe_file(tmp_path, capsys):
    d = tmp_path / "console_probe"
    d.mkdir()
    (d / "20261119_190000.jsonl").write_text(json.dumps({"t": 1.0, "kind": "start", "session": CONSOLE}) + "\n",
                                             encoding="utf-8")
    (d / "20261119_190000.child.jsonl").write_text("", encoding="utf-8")
    lines: list[str] = []
    assert cp.report(d, out=lines.append) == 0
    assert lines[0] == "== 20261119_190000.jsonl" and any("コンソール（RDP なし）" in x for x in lines)
    assert cp.report(tmp_path / "none", out=lines.append) == 1


def test_describe_session():
    assert cp.describe_session({"platform": "linux"}).startswith("Windows 以外")
    assert cp.describe_session(CONSOLE) == "コンソール（RDP なし）"
    assert cp.describe_session(RDP) == "RDP でつないでいる"
    assert cp.describe_session(GONE) == "切断（RDP を閉じたまま）"
