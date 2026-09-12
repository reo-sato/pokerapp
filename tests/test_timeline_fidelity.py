"""tests/test_timeline_fidelity.py

ADR-0044: アクション履歴を**音声の時系列と突き合わせて再生する**ために必要な時刻の精度。

実時刻が要るのは 2 つだけ（オーナー確認済みの要件）:

1. **fold の時刻** — ディーラーが宣言しない fold は、後続アクションで初めて推定されるため
   従来は「次の人が行動した時刻」が入っていた。RFID のマック観測で実時刻を与える。
2. **ターン / リバーの配布時刻** — ベッティングラウンドの区切りとしてアクションの時刻に対応する。

**不在を fold の判定には使わない**（プレイヤーはカードを持ち上げて見るので不在 ≠ fold）。
判定は従来どおり合法手・actor 推定が行い、マック観測は時刻だけを差し替える。
"""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from core.event_queue import make_audio_queue, make_rfid_queue
from core.events import AudioEvent, RFIDEvent
from core.game_state import PlayerState
from core.poker_engine import PokerkitGameState
from integration.engine import IntegrationThread
from output.json_writer import JsonWriter
from rfid.card_master import CardMaster
from rfid.reader_thread import RFIDThread


def _board_event(index: int, card: str, ts: float, tag: str = "T") -> RFIDEvent:
    return RFIDEvent(
        tag_id=tag, card=card, timestamp=ts, raw_tag_id=tag,
        reader_id="board_0", role="board", seat=None, board_index=index,
    )


class TestBoardTimeline:
    """ボード各枚の配布時刻を HandSummary に残す。"""

    def _thread(self, tmp_path: Path, sid: str, clock):
        gs = PokerkitGameState(
            [PlayerState(seat=i + 1, name=f"P{i + 1}", stack=1000) for i in range(3)], sb=1, bb=2
        )
        hands: list = []
        t = IntegrationThread(
            audio_queue=make_audio_queue(), game_state=gs,
            json_writer=JsonWriter(tmp_path, sid), stop_event=threading.Event(),
            on_hand=hands.append, clock=clock,
        )
        return t, hands

    def test_turn_and_river_deal_times_are_recorded(self, tmp_path: Path):
        pytest.importorskip("pokerkit")
        now = [1000.0]
        t, hands = self._thread(tmp_path, "tl1", lambda: now[0])
        t._handle_audio_event(AudioEvent("new_hand", 0, now[0], "n"))   # noqa: SLF001
        for index, card, ts in [(1, "Qc", 1010.0), (2, "8s", 1010.5), (3, "Jd", 1011.0),
                                (4, "Jh", 1040.0), (5, "9s", 1075.0)]:
            t._process_rfid_event(_board_event(index, card, ts, f"T{index}"))   # noqa: SLF001
        now[0] = 1100.0
        t._handle_audio_event(AudioEvent("winner", 0, now[0], "シート1 ウィナー"))  # noqa: SLF001

        timeline = hands[-1].board_timeline
        assert [e["index"] for e in timeline] == [1, 2, 3, 4, 5]
        assert [e["card"] for e in timeline] == ["Qc", "8s", "Jd", "Jh", "9s"]
        # ターン（4 枚目）とリバー（5 枚目）が配られた実時刻がそのまま残る
        assert timeline[3]["dealt_at"] == IntegrationThread._iso(1040.0)   # noqa: SLF001
        assert timeline[4]["dealt_at"] == IntegrationThread._iso(1075.0)   # noqa: SLF001
        # フロップ 3 枚の最小値がフロップラウンドの開始
        assert timeline[0]["dealt_at"] < timeline[3]["dealt_at"] < timeline[4]["dealt_at"]

    def test_redetection_does_not_move_the_deal_time(self, tmp_path: Path):
        """結合の弱いリーダーは同じ札を何度も再検出する。配布時刻は最初の 1 回で固定。"""
        pytest.importorskip("pokerkit")
        now = [1000.0]
        t, hands = self._thread(tmp_path, "tl2", lambda: now[0])
        t._handle_audio_event(AudioEvent("new_hand", 0, now[0], "n"))   # noqa: SLF001
        t._process_rfid_event(_board_event(1, "Qc", 1010.0, "T1"))      # noqa: SLF001
        first = t._build_board_timeline()[0]["dealt_at"]                # noqa: SLF001
        for ts in (1020.0, 1031.0, 1042.0):                             # 再発火
            t._process_rfid_event(_board_event(1, "Qc", ts, "T1"))      # noqa: SLF001
        assert t._build_board_timeline()[0]["dealt_at"] == first        # noqa: SLF001

    def test_misdeal_correction_takes_a_fresh_deal_time(self, tmp_path: Path):
        pytest.importorskip("pokerkit")
        now = [1000.0]
        t, _ = self._thread(tmp_path, "tl3", lambda: now[0])
        t._handle_audio_event(AudioEvent("new_hand", 0, now[0], "n"))   # noqa: SLF001
        t._process_rfid_event(_board_event(4, "Jh", 1040.0, "T4"))      # noqa: SLF001
        before = t._build_board_timeline()[0]["dealt_at"]               # noqa: SLF001
        t._handle_audio_event(AudioEvent("correct_board", 4, 1050.0, "ボード4 訂正"))  # noqa: SLF001
        t._process_rfid_event(_board_event(4, "2d", 1060.0, "T9"))      # noqa: SLF001
        after = t._build_board_timeline()[0]                            # noqa: SLF001
        assert after["card"] == "2d" and after["dealt_at"] != before

    def test_new_hand_clears_the_timeline(self, tmp_path: Path):
        pytest.importorskip("pokerkit")
        now = [1000.0]
        t, _ = self._thread(tmp_path, "tl4", lambda: now[0])
        t._handle_audio_event(AudioEvent("new_hand", 0, now[0], "n"))   # noqa: SLF001
        t._process_rfid_event(_board_event(1, "Qc", 1010.0, "T1"))      # noqa: SLF001
        t._handle_audio_event(AudioEvent("new_hand", 0, 1200.0, "n"))   # noqa: SLF001
        assert t._build_board_timeline() == []                          # noqa: SLF001


class TestSynthFoldTimestamp:
    """合成 silent-fold に「実際に札が席から離れた時刻」を与える。"""

    def _thread(self, tmp_path: Path, sid: str, clock, absent):
        gs = PokerkitGameState(
            [PlayerState(seat=i + 1, name=f"P{i + 1}", stack=1000) for i in range(3)], sb=10, bb=20
        )
        captured: list = []
        t = IntegrationThread(
            audio_queue=make_audio_queue(), game_state=gs,
            json_writer=JsonWriter(tmp_path, sid), stop_event=threading.Event(),
            on_action=captured.append, clock=clock,
            seat_cards_absent_since=absent,
        )
        return t, captured

    def _run_silent_fold(self, tmp_path: Path, sid: str, absent):
        """3-handed: prior=seat3 のところへ seat1 の明示発話 → seat3 の fold を合成する。"""
        pytest.importorskip("pokerkit")
        now = [1000.0]
        t, cap = self._thread(tmp_path, sid, lambda: now[0], absent)
        t._handle_audio_event(AudioEvent("new_hand", 0, now[0], "n"))     # noqa: SLF001
        now[0] = 1050.0
        t._handle_audio_event(  # noqa: SLF001
            AudioEvent("call", 0, now[0], "シート1 コール", seat=1)
        )
        folds = [r for r in cap if r.action == "fold"]
        assert len(folds) == 1, [r.action for r in cap]
        return folds[0]

    def test_uses_muck_time_when_available(self, tmp_path: Path):
        """札が席から消えた 1020.0 が fold の時刻になる（処理時刻 1050.0 ではなく）。"""
        fold = self._run_silent_fold(tmp_path, "sf1", lambda seat: 1020.0)
        assert fold.timestamp == IntegrationThread._iso(1020.0)   # noqa: SLF001
        assert fold.source["rfid"] is True            # 時刻が物理観測由来であることを示す

    def test_falls_back_to_processing_time_without_observation(self, tmp_path: Path):
        fold = self._run_silent_fold(tmp_path, "sf2", lambda seat: None)
        assert fold.timestamp == IntegrationThread._iso(1050.0)   # 処理時刻 = 従来動作 # noqa: SLF001
        assert fold.source["rfid"] is False

    def test_no_hook_keeps_legacy_behaviour(self, tmp_path: Path):
        fold = self._run_silent_fold(tmp_path, "sf3", None)
        assert fold.timestamp == IntegrationThread._iso(1050.0)   # noqa: SLF001
        assert fold.source["rfid"] is False

    def test_observation_outside_the_hand_is_ignored(self, tmp_path: Path):
        """前ハンドの回収など、ハンド開始より前の観測は使わない。"""
        fold = self._run_silent_fold(tmp_path, "sf4", lambda seat: 900.0)
        assert fold.timestamp == IntegrationThread._iso(1050.0)   # noqa: SLF001
        assert fold.source["rfid"] is False

    def test_hook_failure_does_not_break_recording(self, tmp_path: Path):
        def boom(seat):
            raise RuntimeError("reader gone")

        fold = self._run_silent_fold(tmp_path, "sf5", boom)
        assert fold.timestamp == IntegrationThread._iso(1050.0)   # noqa: SLF001
        assert fold.needs_review is True


# ――― RFID 側の観測 ―――


class _FakeBridge:
    def __init__(self, uids: list[str]) -> None:
        self.uids = uids

    def connect(self) -> bool:
        return True

    def read_uids(self) -> list[str]:
        return list(self.uids)

    def disconnect(self) -> None:
        pass


class TestSeatAbsenceObservation:
    def _thread(self, tmp_path: Path, bridges, clock):
        master = tmp_path / "cards.json"
        master.write_text('{"cards": {}}', encoding="utf-8")
        return RFIDThread(
            rfid_queue=make_rfid_queue(), card_master=CardMaster(str(master)),
            reader_configs=[{"name": "s1", "role": "seat", "seat": 1}],
            stop_event=threading.Event(),
            bridge_factory=lambda name, reader=0: bridges[name],
            clock=clock,
        )

    def _poll(self, t, bridges, cfg):
        t._poll_reader(bridges["s1"], cfg, "reader_0")   # noqa: SLF001

    def test_absence_is_dated_and_cleared_on_return(self, tmp_path: Path):
        now = [500.0]
        bridges = {"s1": _FakeBridge(["A1", "A2"])}
        cfg = {"name": "s1", "role": "seat", "seat": 1}
        t = self._thread(tmp_path, bridges, lambda: now[0])
        self._poll(t, bridges, cfg)
        assert t.seat_cards_absent_since(1) is None       # 載っている

        now[0] = 510.0
        bridges["s1"].uids = []                            # 持ち上げた or マックした
        self._poll(t, bridges, cfg)
        assert t.seat_cards_absent_since(1) == 510.0

        now[0] = 512.0
        bridges["s1"].uids = ["A1", "A2"]                  # 見ただけで戻した
        self._poll(t, bridges, cfg)
        assert t.seat_cards_absent_since(1) is None        # 観測は取り消される

    def test_first_absence_time_is_kept(self, tmp_path: Path):
        """消えたまま戻らない間は、最初に消えた時刻を保つ（後の poll で上書きしない）。"""
        now = [500.0]
        bridges = {"s1": _FakeBridge(["A1"])}
        cfg = {"name": "s1", "role": "seat", "seat": 1}
        t = self._thread(tmp_path, bridges, lambda: now[0])
        self._poll(t, bridges, cfg)
        now[0] = 505.0
        bridges["s1"].uids = []
        self._poll(t, bridges, cfg)
        now[0] = 530.0
        self._poll(t, bridges, cfg)                        # 集合は変わらない = no-op
        assert t.seat_cards_absent_since(1) == 505.0

    def test_new_hand_clears_observations(self, tmp_path: Path):
        """ハンド終了時に全席のカードが回収されるので、観測を次のハンドへ持ち越さない。"""
        now = [500.0]
        bridges = {"s1": _FakeBridge(["A1"])}
        cfg = {"name": "s1", "role": "seat", "seat": 1}
        t = self._thread(tmp_path, bridges, lambda: now[0])
        self._poll(t, bridges, cfg)
        now[0] = 505.0
        bridges["s1"].uids = []
        self._poll(t, bridges, cfg)
        assert t.seat_cards_absent_since(1) == 505.0
        t.reset_for_new_hand()
        assert t.seat_cards_absent_since(1) is None
