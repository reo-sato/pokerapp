"""tests/test_store_2026_09_30_script.py

店舗 2026-09-30 の台本のハンド（声だけ, 版 2 = 店の言い方, 6 人・30 ハンド）で見つかったこと:

- ロガーは「始める」を押しても、認識待ちの発話が 0 になるまで台本のハンドを始めなかった。聞き取り（1 発話 2.8 秒）が
  読む速さ（1.3 秒に 1 行）に追いつかず、読み続けている間は 0 にならない → 毎回上限の 60 秒待ってから始め、次のハンドの
  行が前のハンドに入った（記録の一致 8%）。押す前に話し始めた発話だけを待つ（聞き取りが進む限り待つ）。
- 評価: 声だけの台本は RFID なしで再生する / 店の PC の時計（時差の表記なし）を時差込みで読む / 押した順に並べ直して
  再生する（直したロガーの動き = 行 93%・勝者 30/30・全部正しいハンド 27/30）。
- 読み取り: 「チェックラウンド」= チェックアラウンド、「フォールドをホールド」= フォールド 2 つ、「フォールド600」=
  フォールドのあとの 600。
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import main
from audio.recognizer import parse_actions
from core.control_queue import ControlCommand, ControlCommandLog
from core.events import AudioEvent
from integration.control_consumer import ControlConsumerThread

FIXTURE = Path(__file__).parent / "fixtures" / "script" / "2026-09-30-165030"


@pytest.fixture(autouse=True)
def _quiet():
    logging.disable(logging.WARNING)
    yield
    logging.disable(logging.NOTSET)


class TestPendingBefore:
    def test_counts_only_speech_that_began_earlier(self):
        from tests.test_reconstruction_hardening import _make_audio_thread

        t, _ = _make_audio_thread()
        for start in (1.0, 2.0, 3.0):
            t._enqueue_utterance(b"\x00\x00", start)   # noqa: SLF001
        assert t.pending_before(2.5) == 2 and t.pending_before(10.0) == 3 and t.pending_before(0.5) == 0
        t._inferring_start = 0.8                        # noqa: SLF001 — 推論中
        t._capture_start = 4.0                          # noqa: SLF001 — 切り出し中
        assert t.pending_before(2.5) == 3 and t.pending_before(5.0) == 5


class _Speech:
    """話し始めの時刻の並び（聞き取ると消える）。"""

    def __init__(self, starts: list[float]) -> None:
        self.starts = list(starts)
        self.lock = threading.Lock()

    def pending_before(self, ts: float) -> int:
        with self.lock:
            return sum(1 for s in self.starts if s < ts)

    def hear_after(self, delay: float, start: float) -> None:
        def run() -> None:
            with self.lock:
                self.starts.remove(start)
        threading.Timer(delay, run).start()


class TestControlWaitsOnlyForEarlierSpeech:
    def _consumer(self, tmp_path, speech: _Speech, wait: float = 5.0):
        log = ControlCommandLog(tmp_path / "s.control.jsonl")
        q: queue.Queue = queue.Queue()
        consumer = ControlConsumerThread(log, q, threading.Event(), start_offset=0,
                                         backlog=lambda: len(speech.starts), backlog_wait_sec=wait,
                                         pending_before=speech.pending_before)
        return log, q, consumer

    def test_later_speech_does_not_hold_the_hand(self, tmp_path):
        """押す前の 1 発話を聞き取ったら始める。あとから話し始めた次のハンドの行（認識待ち）は待たない。"""
        speech = _Speech([99.0, 101.0, 102.0])
        log, q, consumer = self._consumer(tmp_path, speech)
        log.append("script_hand", {"button": 1, "stacks": {"1": 100, "2": 100}}, ts_clock=lambda: 100.0)
        speech.hear_after(0.2, 99.0)
        began = time.monotonic()
        assert consumer.poll_once() == 1
        assert 0.15 <= time.monotonic() - began < 2.0
        assert q.get_nowait().action == "script_hand" and speech.starts == [101.0, 102.0]

    def test_waits_as_long_as_listening_moves(self, tmp_path):
        """聞き取りが遅れていても、先の発話が減っていくあいだは待つ（上限の秒数で打ち切らない）。"""
        speech = _Speech([97.0, 98.0, 99.0])
        log, q, consumer = self._consumer(tmp_path, speech, wait=0.3)
        log.append("new_hand", ts_clock=lambda: 100.0)
        for i, s in enumerate((97.0, 98.0, 99.0)):
            speech.hear_after(0.2 * (i + 1), s)          # 0.2 秒ごとに 1 つ = 全部で 0.6 秒 > 上限 0.3 秒
        assert consumer.poll_once() == 1 and speech.starts == []

    def test_gives_up_when_listening_stops(self, tmp_path):
        speech = _Speech([99.0])
        log, q, consumer = self._consumer(tmp_path, speech, wait=0.2)
        log.append("new_hand", ts_clock=lambda: 100.0)
        began = time.monotonic()
        assert consumer.poll_once() == 1 and speech.starts == [99.0]
        assert time.monotonic() - began < 1.5

    def test_the_command_keeps_its_time(self, tmp_path):
        log = ControlCommandLog(tmp_path / "c.jsonl")
        cmd = log.append("winner", {"seat": 2}, ts_clock=lambda: 1234.5678)
        (read,), _ = log.read_from(0)
        assert cmd.created_ts == read.created_ts == 1234.568
        old = ControlCommand.from_dict({"command_id": "x", "type": "new_hand", "args": {}, "created_at": ""})
        assert old.created_ts is None and "created_ts" not in old.to_dict()


class TestTypedInputWaitsOnlyForEarlierSpeech:
    def test_speech_after_the_input_is_not_waited_for(self, capsys):
        speech = _Speech([9.0, 11.0])
        speech.hear_after(0.2, 9.0)
        main._wait_for_backlog(SimpleNamespace(pending_before=speech.pending_before), since=10.0)   # noqa: SLF001
        assert speech.starts == [11.0]
        assert "聞き取った発話を先に反映しています… 残り 1 件" in capsys.readouterr().out

    def test_gives_up_when_listening_stops(self, capsys):
        speech = _Speech([9.0])
        main._wait_for_backlog(SimpleNamespace(pending_before=speech.pending_before), timeout_sec=0.2,   # noqa: SLF001
                               since=10.0)
        assert "聞き取りが進みません" in capsys.readouterr().out


class TestReadings:
    @pytest.mark.parametrize("text", ["チェックラウンド", "チェック ラウンド", "チェックアラウンド"])
    def test_check_around(self, text):
        assert [(e.action, e.parse_flags) for e in parse_actions(text)] == [("check", ("check_around",))]

    @pytest.mark.parametrize("text", ["フォールドをホールド", "フォールドをフォールド"])
    def test_two_folds_joined_by_wo(self, text):
        """「を」（片仮名で「ヲ」）は語の中に出ない = 区切り。"""
        assert [e.action for e in parse_actions(text)] == ["fold", "fold"]

    def test_a_word_that_ends_in_hold_is_still_not_a_fold(self):
        assert parse_actions("ホールドデスク") == []

    @pytest.mark.parametrize("text, expected", [
        ("フォールド600", [("fold", 0), ("bet", 600)]),
        ("600フォールド", [("bet", 600), ("fold", 0)]),
        ("チェック800", [("check", 0), ("bet", 800)]),
        ("2千点フォールド", [("bet", 2000), ("fold", 0)]),
        ("コール600", [("call", 600)]),              # コールの額（言い直し）はコールのまま
        ("600点コールです", [("call", 600)]),
    ])
    def test_an_amount_glued_to_fold_or_check_is_the_next_wager(self, text, expected):
        assert [(e.action, e.amount) for e in parse_actions(text)] == expected

    def test_a_seat_before_fold_stays_a_seat(self):
        (fold,) = parse_actions("シート6フォールド")
        assert (fold.action, fold.seat) == ("fold", 6)


class TestEvaluation:
    def test_a_voice_script_is_replayed_without_rfid(self):
        from tools.eval_store import replay_flags

        config = {"rfid": {"enabled": True, "transport": "pcsc"}}
        assert replay_flags(config, [])["rfid_folds"] is True
        assert replay_flags(config, [], {"kind": "voice"})["rfid_folds"] is False
        assert replay_flags(config, [], {"kind": "cards"})["rfid_folds"] is True

    def test_the_store_clock_offset_comes_from_the_record(self):
        from tools.eval_store import record_utc_offset

        jst = timezone(timedelta(hours=9))
        epoch = 1790754677.5
        naive = datetime.fromtimestamp(epoch, jst).replace(tzinfo=None).isoformat(timespec="milliseconds")
        hands = [{"actions": [{"timestamp": naive, "raw_text": "フォールド"}]}]
        events = [AudioEvent(action="fold", amount=0, timestamp=epoch, raw_text="フォールド")]
        assert record_utc_offset(hands, events) == 9 * 3600
        assert record_utc_offset([], events) is None

    def test_script_hands_match_by_order_even_when_the_start_lagged(self):
        from tools.test_script import script_truth

        jst = timezone(timedelta(hours=9))
        script = {"kind": "voice", "hands": [
            {"n": 1, "actions": [{"street": "preflop", "seat": 1, "action": "fold", "amount": 0}], "winner_seat": 2,
             "stacks": {"1": 100, "2": 100}},
            {"n": 2, "actions": [{"street": "preflop", "seat": 2, "action": "fold", "amount": 0}], "winner_seat": 1,
             "stacks": {"1": 100, "2": 100}}]}
        marks = [{"t": 1000.0, "event": "start", "hand": 1}, {"t": 1010.0, "event": "start", "hand": 2}]
        began = [1060.0, 1120.0]                        # 60 秒・110 秒遅れて始まった
        recorded = [{"hand_id": k + 1, "started_at": datetime.fromtimestamp(t, jst).replace(tzinfo=None).isoformat(
            timespec="milliseconds")} for k, t in enumerate(began)]
        st = script_truth(script, marks, recorded, hand_starts=began, utc_offset=9 * 3600)
        assert [(h["hand_id"], h["script_hand"]) for h in st["hands"]] == [(1, 1), (2, 2)] and st["unmatched"] == []
        # 押した時刻から 30 秒以内の窓（前のやり方）では見つからない
        assert script_truth(script, marks, recorded, utc_offset=9 * 3600)["unmatched"] == [1, 2]


class TestStoreScriptSession:
    """台本の画面を押した順に並べ直して再生し、台本と比べる（直したロガーの動き）。baseline より悪くならない。"""

    def test_not_worse_than_the_baseline(self):
        pytest.importorskip("pokerkit")
        from tools.eval_store import replay_session, score_script_order, script_order_events, script_setup

        script = json.loads((FIXTURE / "script.json").read_text(encoding="utf-8"))
        marks = [json.loads(line) for line in (FIXTURE / "script_marks.jsonl").read_text(encoding="utf-8").splitlines()
                 if line.strip()]
        rows = [json.loads(line) for line in (FIXTURE / "transcripts.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()]
        base = json.loads((FIXTURE / "expected.json").read_text(encoding="utf-8"))["baseline"]
        hands = replay_session(script_order_events(script, marks, rows), script_setup(script),
                               {"auto_new_hand": False, "auto_winner": True, "rfid_folds": False}, "fixture")
        score = score_script_order(script, marks, hands)
        assert score["total"] == base["total"]
        assert score["rows"] >= base["rows"] and score["winners"] >= base["winners"] and score["exact"] >= base["exact"], score
