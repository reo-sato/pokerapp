"""tests/test_tournament_timer.py — TournamentTimer + loader の単体テスト。"""
from __future__ import annotations

import json

import pytest

from core.tournament_timer import (
    BlindLevel,
    TournamentStructure,
    TournamentTimer,
    load_structure_from_file,
)


class FakeClock:
    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _lv(level: int, sb: int, bb: int, duration: int, *, ante: int = 0) -> BlindLevel:
    return BlindLevel(
        level=level, label=f"Level {level}",
        sb=sb, bb=bb, ante=ante,
        duration_sec=duration, is_break=False,
    )


def _break_lv(duration: int, label: str = "Break") -> BlindLevel:
    return BlindLevel(
        level=None, label=label, sb=None, bb=None,
        ante=0, duration_sec=duration, is_break=True,
    )


def _structure(levels=None, *, late_reg: int = 0) -> TournamentStructure:
    if levels is None:
        levels = [
            _lv(1, 100, 200, 60),
            _lv(2, 200, 400, 60),
            _lv(3, 300, 600, 60),
        ]
    return TournamentStructure(
        name="t", starting_stack=20000, addon_chips=10000,
        late_reg_closes_after_level=late_reg, levels=levels,
    )


class TestStartPauseResume:
    def test_initial_state_not_started(self):
        clk = FakeClock()
        timer = TournamentTimer(_structure(), clock=clk)
        assert not timer.is_running()
        assert not timer.is_paused()
        assert timer.current_level().level == 1
        assert timer.remaining_sec() == 60.0

    def test_start_runs(self):
        clk = FakeClock()
        timer = TournamentTimer(_structure(), clock=clk)
        timer.start()
        assert timer.is_running()
        assert timer.remaining_sec() == pytest.approx(60.0)
        clk.advance(20)
        assert timer.remaining_sec() == pytest.approx(40.0)

    def test_pause_freezes_remaining(self):
        clk = FakeClock()
        timer = TournamentTimer(_structure(), clock=clk)
        timer.start()
        clk.advance(20)
        timer.pause()
        assert timer.is_paused()
        clk.advance(100)  # wall clock 進んでも残り変わらない
        assert timer.remaining_sec() == pytest.approx(40.0)

    def test_resume_continues(self):
        clk = FakeClock()
        timer = TournamentTimer(_structure(), clock=clk)
        timer.start()
        clk.advance(20)
        timer.pause()
        clk.advance(100)
        timer.resume()
        assert timer.is_running()
        clk.advance(10)
        assert timer.remaining_sec() == pytest.approx(30.0)

    def test_start_is_idempotent(self):
        clk = FakeClock()
        timer = TournamentTimer(_structure(), clock=clk)
        timer.start()
        clk.advance(10)
        timer.start()  # should NOT reset
        assert timer.remaining_sec() == pytest.approx(50.0)


class TestLevelAdvance:
    def test_deadline_advances_level_and_fires_callback(self):
        clk = FakeClock()
        calls: list[BlindLevel] = []
        timer = TournamentTimer(_structure(), clock=clk,
                                on_level_changed=calls.append)
        timer.start()
        clk.advance(60)
        timer.tick()
        assert timer.current_level().level == 2
        assert len(calls) == 1
        assert calls[0].level == 2

    def test_tick_idempotent_same_level(self):
        clk = FakeClock()
        calls: list[BlindLevel] = []
        timer = TournamentTimer(_structure(), clock=clk,
                                on_level_changed=calls.append)
        timer.start()
        clk.advance(60)
        timer.tick()
        timer.tick()
        timer.tick()
        assert len(calls) == 1

    def test_final_level_does_not_fire(self):
        clk = FakeClock()
        calls: list[BlindLevel] = []
        timer = TournamentTimer(_structure(), clock=clk,
                                on_level_changed=calls.append)
        timer.start()
        clk.advance(60); timer.tick()  # L1->L2 (fire)
        clk.advance(60); timer.tick()  # L2->L3 (fire)
        clk.advance(120); timer.tick()  # stay at L3
        assert timer.current_level().level == 3
        assert len(calls) == 2
        assert timer.is_finished()

    def test_advance_level_immediate(self):
        clk = FakeClock()
        calls: list[BlindLevel] = []
        timer = TournamentTimer(_structure(), clock=clk,
                                on_level_changed=calls.append)
        timer.start()
        timer.advance_level()
        assert timer.current_level().level == 2
        assert len(calls) == 1

    def test_advance_level_from_unstarted(self):
        clk = FakeClock()
        calls: list[BlindLevel] = []
        timer = TournamentTimer(_structure(), clock=clk,
                                on_level_changed=calls.append)
        timer.advance_level()
        assert timer.is_running()
        assert timer.current_level().level == 2

    def test_advance_level_at_end_is_noop(self):
        clk = FakeClock()
        timer = TournamentTimer(_structure(), clock=clk)
        timer.start()
        timer.advance_level()
        timer.advance_level()
        # now at last
        timer.advance_level()
        assert timer.current_level().level == 3

    def test_multi_level_tick_across_long_gap(self):
        # Simulate a long sleep that crosses 2 deadlines in one tick.
        clk = FakeClock()
        calls: list[BlindLevel] = []
        timer = TournamentTimer(_structure(), clock=clk,
                                on_level_changed=calls.append)
        timer.start()
        clk.advance(150)  # past L1 (60) and L2 (60+60=120)
        timer.tick()
        assert timer.current_level().level == 3
        assert [c.level for c in calls] == [2, 3]


class TestBreakLevels:
    def test_entering_break_does_not_fire_callback(self):
        levels = [_lv(1, 100, 200, 60), _break_lv(30), _lv(2, 200, 400, 60)]
        clk = FakeClock()
        calls: list[BlindLevel] = []
        timer = TournamentTimer(_structure(levels), clock=clk,
                                on_level_changed=calls.append)
        timer.start()
        clk.advance(60); timer.tick()
        assert timer.current_level().is_break
        assert calls == []

    def test_leaving_break_fires_callback(self):
        levels = [_lv(1, 100, 200, 60), _break_lv(30), _lv(2, 200, 400, 60)]
        clk = FakeClock()
        calls: list[BlindLevel] = []
        timer = TournamentTimer(_structure(levels), clock=clk,
                                on_level_changed=calls.append)
        timer.start()
        clk.advance(60); timer.tick()      # → break
        clk.advance(30); timer.tick()      # → L2
        assert timer.current_level().level == 2
        assert len(calls) == 1
        assert calls[0].level == 2

    def test_advance_to_break_does_not_fire(self):
        levels = [_lv(1, 100, 200, 60), _break_lv(30), _lv(2, 200, 400, 60)]
        clk = FakeClock()
        calls: list[BlindLevel] = []
        timer = TournamentTimer(_structure(levels), clock=clk,
                                on_level_changed=calls.append)
        timer.start()
        timer.advance_level()
        assert timer.current_level().is_break
        assert calls == []


class TestDerivedTimeQueries:
    def test_time_to_next_break(self):
        levels = [_lv(1, 100, 200, 60), _lv(2, 200, 400, 60),
                  _break_lv(30), _lv(3, 300, 600, 60)]
        clk = FakeClock()
        timer = TournamentTimer(_structure(levels), clock=clk)
        timer.start()
        # 60 (L1 remaining) + 60 (L2) = 120 until break
        assert timer.time_to_next_break_sec() == pytest.approx(120.0)
        clk.advance(50)
        assert timer.time_to_next_break_sec() == pytest.approx(70.0)
        clk.advance(10); timer.tick()  # → L2
        assert timer.time_to_next_break_sec() == pytest.approx(60.0)

    def test_time_to_next_break_during_break_is_zero(self):
        levels = [_lv(1, 100, 200, 60), _break_lv(30), _lv(2, 200, 400, 60)]
        clk = FakeClock()
        timer = TournamentTimer(_structure(levels), clock=clk)
        timer.start()
        clk.advance(60); timer.tick()
        assert timer.current_level().is_break
        assert timer.time_to_next_break_sec() == 0.0

    def test_time_to_next_break_no_more_returns_none(self):
        clk = FakeClock()
        timer = TournamentTimer(_structure(), clock=clk)  # no breaks
        timer.start()
        assert timer.time_to_next_break_sec() is None

    def test_time_to_late_reg_close(self):
        # late reg closes after L2; structure has 3 levels of 60s each
        clk = FakeClock()
        timer = TournamentTimer(_structure(late_reg=2), clock=clk)
        timer.start()
        # remaining of L1 (60) + L2 (60) = 120
        assert timer.time_to_late_reg_close_sec() == pytest.approx(120.0)

    def test_time_to_late_reg_close_with_break_between(self):
        levels = [_lv(1, 100, 200, 60), _break_lv(30), _lv(2, 200, 400, 60),
                  _lv(3, 300, 600, 60)]
        clk = FakeClock()
        timer = TournamentTimer(_structure(levels, late_reg=2), clock=clk)
        timer.start()
        # L1 remaining (60) + break (30) + L2 (60) = 150
        assert timer.time_to_late_reg_close_sec() == pytest.approx(150.0)

    def test_time_to_late_reg_close_after_target_returns_none(self):
        clk = FakeClock()
        timer = TournamentTimer(_structure(late_reg=1), clock=clk)
        timer.start()
        clk.advance(60); timer.tick()  # past L1
        assert timer.time_to_late_reg_close_sec() is None

    def test_time_to_late_reg_zero_disabled(self):
        clk = FakeClock()
        timer = TournamentTimer(_structure(late_reg=0), clock=clk)
        timer.start()
        assert timer.time_to_late_reg_close_sec() is None


class TestLoadStructureFromFile:
    def test_loads_valid(self, tmp_path):
        path = tmp_path / "structure.json"
        path.write_text(json.dumps({
            "name": "TestT",
            "starting_stack": 20000,
            "addon_chips": 10000,
            "late_reg_closes_after_level": 2,
            "levels": [
                {"level": 1, "sb": 100, "bb": 200, "duration_sec": 600},
                {"label": "Break", "duration_sec": 300, "is_break": True},
                {"level": 2, "sb": 200, "bb": 400, "ante": 50, "duration_sec": 600},
            ],
        }))
        st = load_structure_from_file(path)
        assert st.name == "TestT"
        assert len(st.levels) == 3
        assert st.levels[1].is_break
        assert st.levels[2].ante == 50

    def test_invalid_sb_bb_order(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text(json.dumps({
            "starting_stack": 20000, "addon_chips": 0,
            "late_reg_closes_after_level": 0,
            "levels": [{"level": 1, "sb": 200, "bb": 100, "duration_sec": 60}],
        }))
        with pytest.raises(ValueError, match="sb"):
            load_structure_from_file(path)

    def test_invalid_duration(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text(json.dumps({
            "starting_stack": 20000, "addon_chips": 0,
            "late_reg_closes_after_level": 0,
            "levels": [{"level": 1, "sb": 100, "bb": 200, "duration_sec": 0}],
        }))
        with pytest.raises(ValueError, match="duration_sec"):
            load_structure_from_file(path)

    def test_empty_levels(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text(json.dumps({
            "starting_stack": 20000, "addon_chips": 0,
            "late_reg_closes_after_level": 0, "levels": [],
        }))
        with pytest.raises(ValueError):
            load_structure_from_file(path)
