"""tests/test_compare_versions.py

前の版と今の版を比べる道具（`tools/compare_versions.py`, 監査 3 回目の必須 7）: 両方の版の物差しの結果
（`bench_hands.py --json`）をハンドごとに比べる。
"""
from __future__ import annotations

from tools.compare_versions import compare, per_hand


def _source(correct, better=(), worse=(), review=(), best=None, name="店舗のログ"):
    keys = ["a#1", "a#2", "a#3"]
    return {"name": name, "correct": list(correct), "better": list(better), "worse": list(worse),
            "review_of": {k: k in review for k in keys}, "best": best or {k: f"rec-{k}" for k in keys}}


def test_per_hand_recovers_the_plain_replay_from_better_and_worse():
    hands = per_hand(_source(correct=["a#1", "a#2"], better=["a#2"], worse=["a#3"]))
    assert {k: (v["ok"], v["base_ok"]) for k, v in hands.items()} == {
        "a#1": (True, True), "a#2": (True, False), "a#3": (False, True)}


def test_hands_that_broke_are_listed_with_whether_they_were_flagged():
    before = [_source(correct=["a#1", "a#2"])]
    after = [_source(correct=["a#1", "a#3"], review=["a#2"],
                     best={"a#1": "rec-a#1", "a#2": "other", "a#3": "new"})]
    lines = compare(before, after)
    assert lines[0].startswith("店舗のログ: 推定の全部正しいハンド 2/3 → 2/3（正しくなった 1・正しくなくなった 1、"
                               "うち要確認なし 0）")
    assert "    正しくなった: a#3" in lines and "    正しくなくなった: a#2" in lines
    assert "    1 番の記録が変わった: a#2 a#3" in lines
    assert not any(line.startswith("    要確認なしで正しくなくなった") for line in lines)


def test_a_silent_break_is_called_out():
    lines = compare([_source(correct=["a#1"])], [_source(correct=[])])
    assert "    要確認なしで正しくなくなった: a#1" in lines


def test_hands_the_new_measure_does_not_count_are_listed_apart():
    """今の版の物差しが数えないハンド（真のアクションの不備など）は、前の版にしか無いハンドと分けて出す。"""
    old = _source(correct=["a#1", "a#2", "a#3"])
    new = dict(_source(correct=["a#1", "a#2"]), excluded={"a#3": ["真のアクションの不備"]})
    new["best"] = {k: v for k, v in new["best"].items() if k != "a#3"}
    new["review_of"] = {k: v for k, v in new["review_of"].items() if k != "a#3"}
    lines = compare([old], [new])
    assert lines[0].startswith("店舗のログ: 推定の全部正しいハンド 2/2 → 2/2")
    assert "    片方の版だけ数えないハンド（真のアクションの不備など）: a#3" in lines
    assert not any(line.startswith("    片方の版にしか無いハンド") for line in lines)


def test_a_source_missing_from_the_old_version():
    assert compare([], [_source(correct=[])]) == ["店舗のログ: 前の版に無い出所"]
