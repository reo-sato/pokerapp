"""プレー中にハンドごとに推定する（ADR-0072 段階 1, 2026-11-09）。

- 次のハンドが始まったら（次が無ければ確定から 60 秒で）そのハンドを推す。
- 推したときの入力の目印（窓の中の書き起こしの行・events の数・窓が閉じたか）が変われば推し直す。1 ハンド 3 回まで。
- 推定の失敗で見回りを止めない。`tools/estimate_logs.py --hands` は指定したハンドだけ推して書き足す。
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path

import pytest

from core.live_estimator import (
    LiveEstimator,
    Marks,
    _Tail,
    hand_marker,
    live_target,
    next_starts,
    read_marks,
    session_markers,
)

SID = "2026-11-09_200000_abcd"
T0 = 1_800_000_000.0


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t).isoformat(timespec="milliseconds")


def _hand(hid: int, start: float, end: float) -> dict:
    return {"hand_id": hid, "started_at": _iso(start), "ended_at": _iso(end), "actions": []}


class _Logs:
    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.hands: list[dict] = []

    def add_hand(self, hid: int, start: float, end: float) -> None:
        self.hands.append(_hand(hid, start, end))
        (self.folder / f"{SID}.json").write_text(json.dumps({"session_id": SID, "hands": self.hands}), encoding="utf-8")

    def live(self, start: float | None) -> None:
        path = self.folder / f"{SID}.live_hand.json"
        if start is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(json.dumps({"session_id": SID, "hand": {"started_at": _iso(start)}}), encoding="utf-8")

    def said(self, t: float) -> None:
        with (self.folder / f"{SID}.transcripts.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"utterance_start_ts": t, "text": "コール"}) + "\n")

    def event(self, t: float) -> None:
        with (self.folder / f"{SID}.events.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"type": "rfid", "timestamp": t}) + "\n")


class _Runner:
    """偽の推定: 呼ばれたハンドを覚え、いまのファイルから数えた目印を推定のファイルに書く（本物と同じ）。"""

    def __init__(self, fail: bool = False) -> None:
        self.calls: list[int] = []
        self.fail = fail

    def __call__(self, log_dir: Path, session_id: str, hand_id: int, target: str) -> bool:
        self.calls.append(hand_id)
        if self.fail:
            raise RuntimeError("推定が落ちた")
        path = log_dir / f"{session_id}.estimate.json"
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"session_id": session_id, "hands": {}}
        data["hands"][str(hand_id)] = {"hand_id": hand_id, "inputs": session_markers(log_dir, session_id)[hand_id]}
        path.write_text(json.dumps(data), encoding="utf-8")
        return True


def _estimator(tmp_path: Path, runner, now: list[float]) -> LiveEstimator:
    return LiveEstimator(tmp_path, runner=runner, clock=lambda: now[0], active_sec=1e12)


class TestMarker:
    def test_the_window_closes_at_the_next_hand(self):
        hands = [_hand(1, T0, T0 + 50), _hand(2, T0 + 60, T0 + 90)]
        assert next_starts(hands, None) == {1: pytest.approx(T0 + 60), 2: None}
        assert next_starts(hands, {"started_at": _iso(T0 + 100)})[2] == pytest.approx(T0 + 100)
        marks = Marks(rows=[T0 - 0.3, T0 + 10, T0 + 59.9, T0 + 70], events=[T0 - 1.5, T0 + 30, T0 + 61])
        assert hand_marker(T0, T0 + 60, marks) == {"rows": 3, "events": 2, "closed": True}
        # 窓は推定器と同じ幅（行は始まりの 0.5 秒前から）なので、境目の行は両方に入る
        assert hand_marker(T0 + 60, None, marks) == {"rows": 2, "events": 1, "closed": False}

    def test_the_tail_reads_only_added_lines(self, tmp_path):
        path = tmp_path / "a.jsonl"
        tail = _Tail(path, "timestamp")
        assert not tail.update()
        path.write_text('{"timestamp": 2.0}\n{"timestamp": 1.0}\n{"timesta', encoding="utf-8")
        assert tail.update() and tail.times == [1.0, 2.0]
        with path.open("a", encoding="utf-8") as f:
            f.write('mp": 3.0}\nnot json\n\x00\x00\n')               # 書きかけの行の続き・読めない行
        assert tail.update() and tail.times == [1.0, 2.0, 3.0]
        assert not tail.update()

    def test_offplay_rows_count_too(self, tmp_path):
        (tmp_path / f"{SID}.transcripts.jsonl").write_text(json.dumps({"utterance_start_ts": 5.0}) + "\n")
        (tmp_path / f"{SID}.offplay.jsonl").write_text(json.dumps({"utterance_start_ts": 3.0}) + "\n")
        assert read_marks(tmp_path, SID).rows == [3.0, 5.0]


class TestWhen:
    def test_a_hand_is_estimated_when_the_next_hand_starts(self, tmp_path):
        logs = _Logs(tmp_path)
        logs.event(T0)
        logs.add_hand(1, T0, T0 + 50)
        now = [T0 + 51]
        runner = _Runner()
        est = _estimator(tmp_path, runner, now)
        assert est.current_session() == SID
        assert est.step() == 0                                      # 次のハンドがまだ（窓が閉じていない）
        logs.live(T0 + 55)                                          # 次のハンドが配られた
        assert est.step() == 1 and runner.calls == [1]
        assert est.step() == 0                                      # 入力が同じなら推し直さない

    def test_the_last_hand_waits_for_idle(self, tmp_path):
        logs = _Logs(tmp_path)
        logs.event(T0)
        logs.add_hand(1, T0, T0 + 50)
        now = [T0 + 100]
        runner = _Runner()
        est = _estimator(tmp_path, runner, now)
        assert est.due(SID) == []                                   # 確定から 50 秒
        now[0] = T0 + 111
        assert est.due(SID) == [1]

    def test_new_inputs_re_estimate_up_to_three_times(self, tmp_path):
        logs = _Logs(tmp_path)
        logs.event(T0)
        logs.add_hand(1, T0, T0 + 50)
        logs.add_hand(2, T0 + 60, T0 + 120)
        now = [T0 + 130]
        runner = _Runner()
        est = _estimator(tmp_path, runner, now)
        assert est.step() == 1 and runner.calls == [1]              # ハンド 2 は確定から 10 秒
        logs.said(T0 + 20)                                          # 後回しの書き起こしが届いた（ハンド 1 の窓）
        assert est.step() == 1 and runner.calls == [1, 1]
        logs.said(T0 + 21)
        assert est.step() == 1 and runner.calls == [1, 1, 1]
        logs.said(T0 + 22)
        assert est.step() == 0                                      # 3 回まで
        logs.said(T0 + 70)                                          # ハンド 2 の窓は別に数える
        now[0] = T0 + 200
        assert est.step() == 1 and runner.calls[-1] == 2

    def test_a_failing_estimate_does_not_stop_the_loop(self, tmp_path):
        logs = _Logs(tmp_path)
        logs.event(T0)
        logs.add_hand(1, T0, T0 + 50)
        logs.add_hand(2, T0 + 60, T0 + 70)
        runner = _Runner(fail=True)
        est = _estimator(tmp_path, runner, [T0 + 200])
        assert est.step() == 2 and runner.calls == [1, 2]
        est.step()
        est.step()
        assert est.step() == 0 and runner.calls == [1, 2] * 3      # 失敗も 1 回に数える（使い続けない）

    def test_an_old_session_is_left_alone(self, tmp_path):
        logs = _Logs(tmp_path)
        logs.event(T0)
        os.utime(tmp_path / f"{SID}.events.jsonl", (T0, T0))
        est = LiveEstimator(tmp_path, runner=_Runner(), clock=lambda: T0 + 13 * 3600)
        assert est.current_session() is None and est.step() == 0

    def test_run_forever_stops(self, tmp_path):
        stop = threading.Event()
        stop.set()
        LiveEstimator(tmp_path, runner=_Runner()).run_forever(stop, interval=0.01)


def test_the_setting():
    assert live_target({}) == "write"
    assert live_target({"estimator": {"live": True}}) == "write"
    assert live_target({"estimator": {"live": "shadow"}}) == "shadow"
    assert live_target({"estimator": {"live": False}}) is None


def test_the_store_server_starts_it_only_when_on(tmp_path, monkeypatch):
    from api import store_server

    started = []
    monkeypatch.setattr(store_server, "keep_running", lambda name, target, stop: started.append(name))
    stop = threading.Event()
    assert store_server.start_live_estimator({"estimator": {"live": False}}, tmp_path, stop) is None
    est = store_server.start_live_estimator({}, tmp_path, stop)
    assert est is not None and est.target == "write" and started == ["ハンドごとの推定"]


def test_merge_keeps_other_hands():
    from tools.estimate_logs import merge_hands

    old = {"session_id": SID, "content_hash": "old", "hands": {"1": {"a": 1}, "10": {"a": 10}, "2": {"a": 2}}}
    new = {"session_id": SID, "content_hash": "new", "hands": {"2": {"a": 22}}}
    merged = merge_hands(old, new)
    assert list(merged["hands"]) == ["1", "2", "10"] and merged["hands"]["2"] == {"a": 22}
    assert merged["content_hash"] == "new"
    assert merge_hands({"session_id": "other", "hands": {"1": {}}}, new) == new
    assert merge_hands(None, new) == new


@pytest.mark.slow                        # 店舗のハンドを推定する（約 15 秒）
def test_a_hand_estimated_during_play_is_the_same_as_afterwards():
    """プレー中に推した結果（入力は次のハンドが始まって少しまで）は、セッションのあとで推した結果と同じ
    （推定は決定的・ハンドは独立・窓は次のハンドの始まりで閉じる）。"""
    pytest.importorskip("pokerkit")
    from integration.replay import load_events
    from integration.world_replay import PresenceTimeline
    from tools.estimate_logs import estimate_session

    folder = next((Path(__file__).resolve().parent / "fixtures" / "store").glob("*7b897671"))
    expected = json.loads((folder / "expected.json").read_text(encoding="utf-8"))
    setup = expected["setup"]
    flags = {k: bool(setup.get(k)) for k in ("auto_new_hand", "auto_winner", "rfid_folds")}
    sid = expected["session_id"]

    def rows(name):
        return [json.loads(line) for line in (folder / name).read_text(encoding="utf-8").splitlines() if line]

    transcripts, presence_rows = rows("transcripts.jsonl"), rows("presence.jsonl")
    events = load_events(folder / "events.jsonl")
    after = estimate_session(events, transcripts, PresenceTimeline.from_rows(presence_rows), setup, flags, sid, [],
                             only_hands={1, 2})
    assert list(after["hands"]) == ["1", "2"]
    # プレー中: ハンド 3 を配って 5 秒までしか届いていない
    cut = datetime.fromisoformat(after["hands"]["2"]["hand"]["ended_at"]).timestamp()
    nxt = min(e.timestamp for e in events if e.timestamp > cut and getattr(e, "kind", None) == "hand_start") + 5.0
    during = estimate_session([e for e in events if e.timestamp < nxt],
                              [r for r in transcripts if r["utterance_start_ts"] < nxt],
                              PresenceTimeline.from_rows([r for r in presence_rows if r["t"] < nxt]),
                              setup, flags, sid, [], only_hands={2}, inputs={2: {"rows": 1, "events": 2, "closed": True}})
    assert list(during["hands"]) == ["2"] and during["hands"]["2"]["inputs"]["closed"] is True
    a, b = after["hands"]["2"], during["hands"]["2"]
    for key in ("hand", "posterior", "margin", "edits", "notes", "alternatives", "review", "changed"):
        assert a[key] == b[key], key
