"""tests/test_forward_ledger.py

前向きの成績表（`tools/forward_ledger.py`, テスト方針 2026-10-08）: 各セッションを初めて見たときの版で、いまの
真のアクションを採点する。
"""
from __future__ import annotations

import json
from pathlib import Path

from tools import forward_ledger as fl

STORE = Path(__file__).resolve().parent / "fixtures" / "store"


def test_every_store_fixture_has_a_first_look_entry():
    first_look = fl.load_first_look()
    folders = {p.parent.name for p in STORE.glob("*/expected.json")}
    assert folders == set(first_look)                    # 新しい開発データには初めて見た版を書く
    assert all(set(v) == {"commit", "batch"} for v in first_look.values())


def test_sessions_are_grouped_by_their_first_look_version():
    groups = fl.by_commit({"a": {"commit": "x"}, "b": {"commit": None}, "c": {"commit": "x"}, "d": {"commit": "y"}})
    assert groups == {"x": ["a", "c"], "y": ["d"]}       # 版の無いセッション（推定器より前）は前向きに数えない


def _store(tmp_path: Path, sid: str, hands: dict[int, dict]) -> Path:
    folder = tmp_path / f"2026-10-09-{sid[:8]}"
    folder.mkdir(parents=True)
    expected = {"session_id": sid, "setup": {"players": [{"seat": s} for s in (1, 2, 3, 4)]},
                "hands": [{"hand_id": h, "truth": t} for h, t in hands.items()]}
    (folder / "expected.json").write_text(json.dumps(expected), encoding="utf-8")
    return folder


def _bench(correct: list[str], better: list[str], review: dict[str, bool], best: list[str]) -> list[dict]:
    return [{"name": fl.SOURCE, "correct": correct, "better": better, "worse": [], "review_of": review,
             "best": {k: k for k in best}}]


def test_rows_score_the_first_look_on_the_hands_counted_now(tmp_path):
    _store(tmp_path, "aaaaaaaa1111", {1: {"winner_seat": 1}, 2: {"winner_seat": 2}, 3: {"winner_seat": 3}})
    _store(tmp_path, "bbbbbbbb2222", {1: {"winner_seat": 4}})
    first_look = {"2026-10-09-aaaaaaaa": {"commit": "old1", "batch": "10/09"},
                  "2026-10-09-bbbbbbbb": {"commit": None, "batch": "09/29"}}
    now = _bench(correct=["aaaaaaaa#1", "aaaaaaaa#2", "bbbbbbbb#1"], better=["aaaaaaaa#2"],
                 review={"aaaaaaaa#3": False}, best=["aaaaaaaa#1", "aaaaaaaa#2", "aaaaaaaa#3", "bbbbbbbb#1"])
    # 初めて見た版: #1 は正しい、#2 は誤り（要確認なし）、#3 は数えなかった（その版の規則で外れた）
    old = _bench(correct=["aaaaaaaa#1"], better=[], review={"aaaaaaaa#2": False}, best=["aaaaaaaa#1", "aaaaaaaa#2"])
    rows = fl.ledger_rows(first_look, now, {"old1": old}, store=tmp_path)
    a, b = rows
    assert (a["hands"], a["first_hands"], a["first_missing"]) == (3, 2, ["aaaaaaaa#3"])
    assert (a["first_est"], a["first_base"], a["first_unflagged"]) == (1, 1, 1)
    assert (a["now_est"], a["now_base"], a["now_unflagged"]) == (2, 1, 1)
    assert a["seats"] == 4 and len(a["gt"]) == 8
    assert "first_est" not in b and b["now_est"] == 1     # 標本内だけのセッション
    table = "\n".join(fl.format_table(rows))
    assert f"| 10/09 | aaaaaaaa | 4 | old1 | 2（初見で数えられない 1） | 1 | 1 | 1 | 2/3 | {a['gt']} |" in table
    assert "**1/2（50%）**" in table
    assert "標本内だけ（推定器 v1 より前に入れたセッション 1 個）: 今の版 推定 1/1（100%）" in table


def test_the_ground_truth_fingerprint_changes_when_the_truth_changes():
    expected = {"hands": [{"hand_id": 1, "truth": {"winner_seat": 5}}, {"hand_id": 2, "truth": {"winner_seat": 6}}]}
    before = fl.gt_fingerprint(expected, [1, 2])
    assert fl.gt_fingerprint(expected, [2, 1]) == before
    expected["hands"][0]["truth"]["winner_seat"] = 6
    assert fl.gt_fingerprint(expected, [1, 2]) != before
    assert fl.gt_fingerprint(expected, [2]) == fl.gt_fingerprint({"hands": expected["hands"][1:]}, [2])


def test_the_old_version_gets_the_current_fixtures(tmp_path):
    store = tmp_path / "store"
    for name in ("x", "y"):
        (store / name).mkdir(parents=True)
        (store / name / "expected.json").write_text(name, encoding="utf-8")
    tree = tmp_path / "tree"
    (tree / "tests" / "fixtures" / "store" / "old").mkdir(parents=True)
    fl.replace_fixtures(tree, ["y"], store=store)
    assert sorted(p.name for p in (tree / "tests" / "fixtures" / "store").iterdir()) == ["y"]
