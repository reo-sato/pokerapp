"""tools/param_flips.py — 値を差し替えたときの順位のひっくり返りの試算（台帳 F の F3a）。"""
from __future__ import annotations

from pathlib import Path

import pytest

from tools.param_flips import SETS, reading_overrides, summarize

ROOT = Path(__file__).resolve().parent.parent


def test_summarize_counts_fixed_and_broken():
    sets = [("a", {}, {}), ("b", {}, {})]
    results = [{"key": "x#1", "correct": False, "now": -1.0, "sets": {"a": 0.5, "b": -0.5}},
               {"key": "x#2", "correct": False, "now": 2.0, "sets": {"a": 3.0, "b": 3.0}},   # 探しきれない = 数えない
               {"key": "y#1", "correct": True, "now": 1.0, "sets": {"a": -0.2, "b": 1.0}}]
    lines = summarize(results, sets)
    assert "| a | 1 | 1 | +0 | x#1 | y#1 |" in lines
    assert "| b | 0 | 0 | +0 | — | — |" in lines


def test_reading_overrides_are_restored():
    import integration.estimator as E

    before = dict(E.READING_OVERRIDES)
    with reading_overrides({"ear_base": 5.0}):
        assert E.reading_params()["ear_base"] == 5.0
    assert E.READING_OVERRIDES == before


def test_sets_name_known_values():
    from integration.estimator import PARAMS, reading_params

    reading = reading_params()
    for _name, params, read in SETS:
        assert set(params) <= set(PARAMS) and set(read) <= set(reading)


def test_estimator_does_not_import_the_tool():
    for path in ("integration/estimator.py", "tools/estimate.py"):
        assert "param_flips" not in (ROOT / path).read_text(encoding="utf-8")


@pytest.mark.slow
def test_reading_values_reach_the_rescoring():
    """読みの直しは確からしさを `Edit.logp` に持つので、付け替えないと読みの値の差し替えが効かない（作った最初の版の誤り）。"""
    import tools.param_flips as F

    from tools.bench_hands import scored_hands, v1_inputs
    from integration.estimator import PARAMS

    (_name, sessions), = [x for x in v1_inputs(store=True, script=False) if x[1]]
    inp, presence = next((i, p) for i, p in sessions if i.session_id.startswith("027e4b15"))
    est = F._estimator(inp, presence, PARAMS)
    h = next(h for h in scored_hands(inp, est) if h.key == "027e4b15#1")
    r = F.flip_hand(est, h, [("ear +5", {}, {"ear_base": 5.0})],
                    lambda p: F._estimator(inp, presence, dict(PARAMS, **p)))
    assert r is not None and r["sets"]["ear +5"] != r["now"]
