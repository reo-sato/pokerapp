"""tests/test_table_state.py

ADR-0056 D5: **RFID だけから導く卓状態**（カード / 有効席 / ストリート）。

実プレイ環境で検証したいのはこの 3 つで、アクション推定には依存しない（推定はダミーでよい）。
要点は **「札が載っている」と「ゲームに残っている」を混同しない**こと（ADR-0056 D4）。
プレイヤーは札を持ち上げて見るので、不在は fold を意味しない。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from core.event_queue import make_audio_queue, make_rfid_queue
from core.events import AudioEvent, RFIDEvent
from core.game_state import GameStateManager, PlayerState
from core.table_state import DEFAULT_FOLD_HINT_SEC, build_table_state, derive_street
from integration.engine import IntegrationThread
from output.json_writer import JsonWriter
from output.table_state_writer import TableStateWriter
from rfid.card_master import CardMaster
from rfid.reader_thread import RFIDThread
from tools import table_monitor


class TestDeriveStreet:
    def test_board_count_maps_to_street(self):
        assert [derive_street(n) for n in range(6)] == [
            "preflop", "preflop", "preflop", "flop", "turn", "river"
        ]

    def test_over_five_is_river(self):
        assert derive_street(7) == "river"


class TestBuildTableState:
    def _state(self, presence, hole=None, board=None, now=1000.0):
        return build_table_state(
            session_id="s", hand_id=1, now=now, updated_at="2026-09-12T00:00:00.000",
            seats=[1, 2, 3], hole_cards=hole or {}, presence=presence,
            board=board or [], board_timeline=[], engine_street="preflop",
        )

    def test_seat_with_cards_is_dealt_in_and_present(self):
        st = self._state({1: {"present": True, "absent_since": None}}, hole={1: ["Ah", "Kd"]})
        seat = st.seats[0]
        assert (seat.dealt_in, seat.present, seat.cards) == (True, True, ["Ah", "Kd"])
        assert seat.away_sec is None and seat.likely_folded is False

    def test_never_dealt_seat_is_not_dealt_in(self):
        st = self._state({1: {"present": False, "absent_since": None}})
        assert st.seats[0].dealt_in is False
        assert st.dealt_in_seats == []

    def test_lifted_cards_are_present_absent_but_not_folded(self):
        """持ち上げて見ているだけ = 離席していても fold らしいとは言わない（しきい値内）。"""
        st = self._state(
            {1: {"present": False, "absent_since": 995.0}}, hole={1: ["Ah", "Kd"]}, now=1000.0
        )
        seat = st.seats[0]
        assert seat.dealt_in is True and seat.present is False
        assert seat.away_sec == pytest.approx(5.0)
        assert seat.likely_folded is False
        assert st.likely_folded_seats == []

    def test_long_absence_is_flagged_as_likely_folded(self):
        st = self._state(
            {1: {"present": False, "absent_since": 1000.0 - DEFAULT_FOLD_HINT_SEC - 1}},
            hole={1: ["Ah", "Kd"]}, now=1000.0,
        )
        assert st.seats[0].likely_folded is True
        assert st.likely_folded_seats == [1]

    def test_detected_without_card_master_entry_still_counts_as_dealt_in(self):
        """カード名が出なくても検出されていれば配布済み（= 未登録タグのサイン）。"""
        st = self._state({2: {"present": True, "absent_since": None}})
        seat = next(s for s in st.seats if s.seat == 2)
        assert seat.dealt_in is True and seat.cards == []

    def test_street_comes_from_board_count(self):
        st = self._state({}, board=["Qc", "8s", "Jd", "Jh"])
        assert st.rfid_street == "turn" and st.engine_street == "preflop"

    def test_summary_lists_are_consistent(self):
        st = self._state(
            {
                1: {"present": True, "absent_since": None},
                2: {"present": False, "absent_since": 900.0},
                3: {"present": False, "absent_since": None},
            },
            hole={1: ["Ah"], 2: ["2c"]}, now=1000.0,
        )
        assert st.present_seats == [1]
        assert st.dealt_in_seats == [1, 2]
        assert st.likely_folded_seats == [2]

    def test_button_and_positions_are_shown(self):
        """ボタンが卓の脇から目視できる（回転していることを実機で確認するため, ISSUE-0032）。"""
        st = build_table_state(
            session_id="s", hand_id=2, now=1000.0, updated_at="t",
            seats=[1, 2, 3], hole_cards={}, presence={}, board=[], board_timeline=[],
            button_seat=1, position_map={2: "SB", 3: "BB", 1: "BTN"},
        )
        assert st.button_seat == 1
        assert [s.position for s in st.seats] == ["BTN", "SB", "BB"]
        assert st.to_dict()["button_seat"] == 1

    def test_button_is_absent_without_a_rules_aware_backend(self):
        st = self._state({})
        assert st.button_seat is None
        assert all(s.position == "" for s in st.seats)


class TestPresenceSnapshot:
    """RFIDThread が卓状態に渡す在否。"""

    class _Bridge:
        def __init__(self, uids):
            self.uids = uids

        def connect(self):
            return True

        def read_uids(self):
            return list(self.uids)

        def disconnect(self):
            pass

    def _thread(self, tmp_path: Path, bridges, clock):
        master = tmp_path / "cards.json"
        master.write_text('{"cards": {}}', encoding="utf-8")
        return RFIDThread(
            rfid_queue=make_rfid_queue(), card_master=CardMaster(str(master)),
            reader_configs=[{"name": "s1", "role": "seat", "seat": 1}],
            stop_event=threading.Event(),
            bridge_factory=lambda name, reader=0: bridges[name], clock=clock,
        )

    def test_present_then_absent(self, tmp_path: Path):
        now = [100.0]
        bridges = {"s1": self._Bridge(["A1", "A2"])}
        cfg = {"name": "s1", "role": "seat", "seat": 1}
        t = self._thread(tmp_path, bridges, lambda: now[0])
        t._poll_reader(bridges["s1"], cfg, "reader_0")        # noqa: SLF001
        assert t.presence_snapshot() == {
            1: {"present": True, "absent_since": None, "uid_count": 2}
        }
        now[0] = 130.0
        bridges["s1"].uids = []
        t._poll_reader(bridges["s1"], cfg, "reader_0")        # noqa: SLF001
        assert t.presence_snapshot() == {
            1: {"present": False, "absent_since": 130.0, "uid_count": 0}
        }


class TestWriterAndPublish:
    def _thread(self, tmp_path: Path, presence):
        gs = GameStateManager(
            [PlayerState(seat=i + 1, name=f"P{i + 1}", stack=100) for i in range(3)], sb=1, bb=2
        )
        now = [500.0]
        writer = TableStateWriter(tmp_path, "sess")
        t = IntegrationThread(
            audio_queue=make_audio_queue(), game_state=gs,
            json_writer=JsonWriter(tmp_path, "sess"), stop_event=threading.Event(),
            clock=lambda: now[0], seat_presence=lambda: presence,
            table_state_writer=writer,
        )
        return t, writer, now

    def _rfid(self, seat: int, card: str, ts: float) -> RFIDEvent:
        return RFIDEvent(
            tag_id=card, card=card, timestamp=ts, raw_tag_id=card,
            reader_id=f"seat_{seat}", role="seat", seat=seat, board_index=None,
        )

    def test_snapshot_is_written_on_card_detection(self, tmp_path: Path):
        presence = {1: {"present": True, "absent_since": None}}
        t, writer, _ = self._thread(tmp_path, presence)
        t._process_rfid_event(self._rfid(1, "Ah", 500.0))     # noqa: SLF001
        state = json.loads(writer.snapshot_path.read_text(encoding="utf-8"))
        assert state["present_seats"] == [1]
        assert next(s for s in state["seats"] if s["seat"] == 1)["cards"] == ["Ah"]

    def test_history_records_observation_time(self, tmp_path: Path):
        presence = {1: {"present": True, "absent_since": None}}
        t, writer, _ = self._thread(tmp_path, presence)
        t._process_rfid_event(self._rfid(1, "Ah", 499.5))     # noqa: SLF001
        lines = writer.history_path.read_text(encoding="utf-8").strip().split("\n")
        assert json.loads(lines[-1])["observed_at"] == 499.5

    def test_history_skips_unchanged_republish(self, tmp_path: Path):
        """定期 publish で経過秒数だけが動くときは履歴に積まない。"""
        presence = {1: {"present": True, "absent_since": None}}
        t, writer, now = self._thread(tmp_path, presence)
        t._process_rfid_event(self._rfid(1, "Ah", 500.0))     # noqa: SLF001
        before = len(writer.history_path.read_text(encoding="utf-8").strip().split("\n"))
        now[0] = 520.0
        t._publish_table_state()                              # noqa: SLF001
        after = len(writer.history_path.read_text(encoding="utf-8").strip().split("\n"))
        assert after == before

    def test_fold_shows_up_without_any_new_event(self, tmp_path: Path):
        """札が外れても RFIDEvent は出ない。定期 publish で有効席が更新されること。"""
        presence = {1: {"present": True, "absent_since": None}}
        t, writer, now = self._thread(tmp_path, presence)
        t._process_rfid_event(self._rfid(1, "Ah", 500.0))     # noqa: SLF001
        presence[1] = {"present": False, "absent_since": 505.0}
        now[0] = 540.0
        t._publish_table_state()                              # noqa: SLF001
        state = json.loads(writer.snapshot_path.read_text(encoding="utf-8"))
        assert state["present_seats"] == [] and state["likely_folded_seats"] == [1]

    def test_no_writer_is_a_noop(self, tmp_path: Path):
        gs = GameStateManager([PlayerState(seat=1, name="P1", stack=100)], sb=1, bb=2)
        t = IntegrationThread(
            audio_queue=make_audio_queue(), game_state=gs,
            json_writer=JsonWriter(tmp_path, "s2"), stop_event=threading.Event(),
        )
        t._process_rfid_event(self._rfid(1, "Ah", 1.0))       # noqa: SLF001
        assert list(tmp_path.glob("*table_state*")) == []

    def test_presence_hook_failure_does_not_break_publish(self, tmp_path: Path):
        def boom():
            raise RuntimeError("reader gone")

        gs = GameStateManager([PlayerState(seat=1, name="P1", stack=100)], sb=1, bb=2)
        writer = TableStateWriter(tmp_path, "s3")
        t = IntegrationThread(
            audio_queue=make_audio_queue(), game_state=gs,
            json_writer=JsonWriter(tmp_path, "s3"), stop_event=threading.Event(),
            seat_presence=boom, table_state_writer=writer,
        )
        t._process_rfid_event(self._rfid(1, "Ah", 1.0))       # noqa: SLF001
        state = json.loads(writer.snapshot_path.read_text(encoding="utf-8"))
        assert state["seats"][0]["cards"] == ["Ah"]           # 在否なしでも publish される

    def test_new_hand_publishes_reset_state(self, tmp_path: Path):
        presence = {1: {"present": True, "absent_since": None}}
        t, writer, _ = self._thread(tmp_path, presence)
        t._process_rfid_event(self._rfid(1, "Ah", 500.0))     # noqa: SLF001
        t._handle_audio_event(AudioEvent("new_hand", 0, 501.0, "n"))   # noqa: SLF001
        state = json.loads(writer.snapshot_path.read_text(encoding="utf-8"))
        assert state["board"] == [] and next(
            s for s in state["seats"] if s["seat"] == 1
        )["cards"] == []


class TestMonitorReader:
    def test_reports_missing_snapshot(self, tmp_path: Path):
        state = table_monitor.read_state(tmp_path, None)
        assert "error" in state

    def test_picks_latest_session_and_reports_age(self, tmp_path: Path):
        (tmp_path / "a.table_state.json").write_text(json.dumps({
            "session_id": "a", "hand_id": 1, "updated_at": "2026-09-12T00:00:00.000",
            "seats": [], "board": [], "rfid_street": "preflop",
        }), encoding="utf-8")
        state = table_monitor.read_state(tmp_path, None)
        assert state["session_id"] == "a"
        assert state["age_sec"] is not None and state["age_sec"] >= 0

    def test_partial_write_is_reported_not_raised(self, tmp_path: Path):
        (tmp_path / "b.table_state.json").write_text("{ broken", encoding="utf-8")
        assert "error" in table_monitor.read_state(tmp_path, None)

    def test_text_rendering_covers_each_seat_state(self, tmp_path: Path):
        state = {
            "session_id": "s", "hand_id": 3, "updated_at": "x", "age_sec": 0.4,
            "rfid_street": "flop", "engine_street": "preflop", "board": ["Qc", "8s", "Jd"],
            "seats": [
                {"seat": 1, "dealt_in": True, "present": True, "cards": ["Ah", "Kd"],
                 "away_sec": None, "likely_folded": False},
                {"seat": 2, "dealt_in": True, "present": False, "cards": ["2c", "3d"],
                 "away_sec": 40.0, "likely_folded": True},
                {"seat": 3, "dealt_in": False, "present": False, "cards": [],
                 "away_sec": None, "likely_folded": False},
            ],
        }
        out = table_monitor._format_text(state)               # noqa: SLF001
        assert "卓上" in out and "fold らしい(40.0s)" in out and "未配布" in out
        assert "flop" in out and "Qc 8s Jd" in out


class TestAnalyzeHistory:
    """ADR-0056 D7 の計測: 反映遅延 / 不在時間 / ストリート遷移を履歴から出す。"""

    def _row(self, t: str, *, hand=1, street="preflop", observed=None, seats=()):
        row = {
            "session_id": "s", "hand_id": hand, "updated_at": t, "rfid_street": street,
            "seats": [
                {"seat": s, "dealt_in": d, "present": p, "cards": [], "away_sec": None,
                 "likely_folded": False}
                for s, d, p in seats
            ],
        }
        if observed is not None:
            row["observed_at"] = observed
        return row

    def test_lag_is_measured_from_observed_at(self):
        from datetime import datetime

        from tools import analyze_table_state as ats

        t = "2026-09-12T10:00:01.500"
        observed = datetime.fromisoformat("2026-09-12T10:00:01.000").timestamp()
        result = ats.analyze([self._row(t, observed=observed)])
        assert result["lags"] == pytest.approx([0.5])

    def test_returned_and_final_absences_are_separated(self):
        """持ち上げて戻した不在と、戻らなかった不在を分けて集計する。"""
        from tools import analyze_table_state as ats

        rows = [
            self._row("2026-09-12T10:00:00.000", seats=[(1, True, True), (2, True, True)]),
            self._row("2026-09-12T10:00:05.000", seats=[(1, True, False), (2, True, False)]),
            self._row("2026-09-12T10:00:09.000", seats=[(1, True, True), (2, True, False)]),
            self._row("2026-09-12T10:01:00.000", seats=[(1, True, True), (2, True, False)]),
        ]
        result = ats.analyze(rows)
        assert result["returned"] == pytest.approx([4.0])    # 席1 は 4 秒で戻った
        assert result["final"] == pytest.approx([55.0])      # 席2 は戻らなかった

    def test_report_warns_when_threshold_is_too_low(self):
        from tools import analyze_table_state as ats

        result = {"rows": 2, "hands": [1], "lags": [], "returned": [30.0], "final": [],
                  "streets": []}
        assert "⚠" in ats.report(result, fold_hint_sec=20.0)
        ok = dict(result, returned=[5.0])
        assert "✓" in ats.report(ok, fold_hint_sec=20.0)

    def test_street_transitions_are_listed_once_each(self):
        from tools import analyze_table_state as ats

        rows = [
            self._row("2026-09-12T10:00:00.000", street="preflop"),
            self._row("2026-09-12T10:00:30.000", street="preflop"),
            self._row("2026-09-12T10:01:00.000", street="flop"),
            self._row("2026-09-12T10:02:00.000", street="turn"),
        ]
        assert [s[2] for s in ats.analyze(rows)["streets"]] == ["preflop", "flop", "turn"]
