"""tests/test_bench_hands.py

目標の数字 = 全部正しいハンドの割合（オーナー 2026-09-30: 発話の読みが 9 割当たっても、ハンドが丸ごと正しい割合は
全然足りない）。`measure_capture_accuracy.hand_fully_correct` と、台本のセッションを推定器の入力にする
`estimate.script_input`、物差し `tools/bench_hands.py` の形。
"""
from __future__ import annotations

import pytest

from tools.bench_hands import SourceResult, format_result, short_id
from tools.measure_capture_accuracy import hand_fully_correct

TRUTH = {
    "hand_id": 1, "winner_seat": 2, "board": ["Kh", "Th", "As"],
    "actions": [{"street": "preflop", "seat": 1, "action": "raise", "amount": 500},
                {"street": "preflop", "seat": 2, "action": "call", "amount": 400}],
}


def _hand(**changes) -> dict:
    hand = {"hand_id": 1, "winner_seat": 2, "board": ["Kh", "Th", "As"],
            "actions": [dict(a) for a in TRUTH["actions"]]}
    hand.update(changes)
    return hand


class TestFullyCorrect:
    def test_exact(self):
        assert hand_fully_correct(TRUTH, _hand())

    @pytest.mark.parametrize("hand", [
        _hand(actions=[{"street": "preflop", "seat": 1, "action": "raise", "amount": 600},
                       {"street": "preflop", "seat": 2, "action": "call", "amount": 400}]),     # 額
        _hand(actions=[{"street": "preflop", "seat": 1, "action": "raise", "amount": 500}]),      # 行が足りない
        _hand(actions=[*TRUTH["actions"], {"street": "flop", "seat": 1, "action": "check", "amount": 0}]),  # 余計な行
        _hand(winner_seat=1),                                                                      # 勝者
        _hand(board=["Kh", "Th", "Ad"]),                                                           # ボード
        None,                                                                                      # ハンドが無い
    ])
    def test_not_exact(self, hand):
        assert not hand_fully_correct(TRUTH, hand)

    def test_no_board_in_the_truth(self):
        """声だけの台本の正解にはボードが無い（札を置かない）。"""
        assert hand_fully_correct(dict(TRUTH, board=[]), _hand(board=[]))

    def test_showdown_mucks_are_not_compared(self):
        """ショーダウンのマックの行は真のアクションの入力で任意（結果は勝者で見る）。"""
        muck = {"street": "showdown", "seat": 1, "action": "fold", "amount": 0}
        assert hand_fully_correct(TRUTH, _hand(actions=[*TRUTH["actions"], muck]))
        assert hand_fully_correct(dict(TRUTH, actions=[*TRUTH["actions"], muck]), _hand())


def test_script_input_uses_the_latest_start_and_speech_start_times():
    from tools.estimate import SCRIPT_FLAGS, script_input

    script = {"table": {"seats": [1, 2, 3], "stacks": {"1": 1000, "2": 1000, "3": 1000}, "sb": 100, "bb": 200,
                        "first_button": 3, "names": {}},
              "hands": [{"n": 1, "button": 3, "stacks": {"1": 1000, "2": 1000, "3": 1000},
                         "actions": [{"street": "preflop", "seat": 1, "action": "fold", "amount": 0}],
                         "winner_seat": 2}]}
    marks = [{"t": 10.0, "event": "start", "hand": 1}, {"t": 20.0, "event": "start", "hand": 1, "redo": True}]
    rows = [{"utterance_start_ts": 21.0, "heard_at": 75.0, "text": "フォールド"}]
    inp = script_input(script, marks, rows, "s")
    assert [e.action for e in inp.events] == ["script_hand", "script_hand"]
    assert inp.truth["hands"][0]["hand_id"] == 2            # やり直したハンドは後の回
    assert inp.transcripts[0]["heard_at"] == 21.0           # 話し始めの時刻に置く（聞き取りの遅れを持ち込まない）
    assert inp.flags == SCRIPT_FLAGS


def test_v1_bench_format_and_interval():
    from tools.bench_hands import V1Result, format_v1, wilson

    lo, hi = wilson(14, 18)
    assert 0.54 < lo < 0.56 and 0.90 < hi < 0.92            # 18 ハンドでは区間が広い（監査の指摘）
    assert wilson(0, 0) == (0.0, 1.0)
    r = V1Result("店舗（実卓）", hands=18, base_exact=12, exact=14, in_candidates=16, flagged=7,
                 better=["9d1d8536#4", "7b897671#3"], failing=["027e4b15#1"], not_found=["027e4b15#1"],
                 flagged_correct=["9d1d8536#4"], edits={"0": 15, "2": 1}, ties=["d0f055fb#3"])
    text = "\n".join(format_v1(r))
    assert "読み直し 12/18（67%） → 推定 14/18（78%, 95% 区間 55%〜91%）" in text
    assert "同点（次点との差 0）: d0f055fb#3" in text
    assert "良くなった 2・悪くなった 0" in text and "誤りに付かない 0・正しいのに付く 1" in text
    assert "正解が候補に無い 1・候補にあるが点で負けた 0" in text


def test_search_check_format():
    from tools.bench_hands import V1Result, format_search_check, wide_params

    assert wide_params({"beam": 4, "expand": 12, "depth": 3}) == {"beam": 8, "expand": 24, "depth": 3}
    a = V1Result("店舗（実卓）", hands=3, exact=1, best={"x#1": "A", "x#2": "B", "x#3": "C"}, correct=["x#1"])
    b = V1Result("店舗（実卓）", hands=3, exact=1, best={"x#1": "A2", "x#2": "B2", "x#3": "C"}, correct=["x#2"])
    text = "\n".join(format_search_check([a], [b]))
    assert "1 番が変わる 2/3（正しくなる 1・正しくなくなる 1）" in text and "x#1 x#2" in text


def test_repeat_check_format():
    """切り替えの条件「同じ入力で同じ結果」: 2 回回して、ハンドごとの 1 番の記録と要確認を比べる。"""
    from tools.bench_hands import V1Result, format_repeat_check

    first = V1Result("店舗（実卓）", hands=2, best={"x#1": "A", "x#2": "B"}, review_of={"x#1": True, "x#2": False})
    same = V1Result("店舗（実卓）", hands=2, best={"x#1": "A", "x#2": "B"}, review_of={"x#1": True, "x#2": False})
    assert "同じ入力で 2 回: 全ハンド同じ" in "\n".join(format_repeat_check([first], [same]))
    other = V1Result("店舗（実卓）", hands=2, best={"x#1": "A", "x#2": "B2"}, review_of={"x#1": False, "x#2": False})
    text = "\n".join(format_repeat_check([first], [other]))
    assert "1 番が違う 1・要確認が違う 1" in text and "x#1 x#2" in text


def test_bench_format():
    assert (short_id("2026-09-30_165030_script_voice"), short_id("d0f055fb6fdd441082be18ced7cee437")) == (
        "165030", "d0f055fb")
    r = SourceResult("店舗", hands=18, default_exact=12, estimate_exact=13, in_nbest=13,
                     default_rows=[158, 188], estimate_rows=[159, 188], better=["7b897671#3"])
    text = "\n".join(format_result(r))
    assert "全部正しいハンド 読み直し 12/18（67%） → 推定 13/18（72%）" in text and "7b897671#3" in text


def test_record_check_shows_new_unflagged_errors_and_changed_ground_truth():
    """テスト方針 2026-10-08: 物差しは前の記録と比べて、新しい要確認なしの誤り（合格を左右する数）と、真のアクションが
    変わったセッション（点の変化がコードでなく真のアクションの直しから来ることがある）を出す。"""
    from tools.bench_hands import V1Result, format_record_check

    record = {"unflagged_errors": {"店舗（実卓）": ["a#1", "b#2"]}, "gt": {"a": "11111111", "b": "22222222"}}
    r = V1Result("店舗（実卓）", unflagged_errors=["b#2", "c#3"])
    lines, worse = format_record_check([r], record, {"a": "11111111", "b": "33333333", "c": "44444444"})
    text = "\n".join(lines)
    assert worse
    assert "要確認なしの誤り 2 → 2（新しく 1: c#3・なくなった 1: a#1）" in text
    assert "変わった b・増えた c" in text
    lines, worse = format_record_check([V1Result("店舗（実卓）", unflagged_errors=["a#1"])], record,
                                       {"a": "11111111", "b": "22222222"})
    assert not worse and lines[-1] == "真のアクション: 前の記録と同じ"


def test_the_bench_record_matches_the_current_ground_truth():
    """開発データの真のアクションを直したら、物差しを回して前の記録も書き直す（`bench_hands.py --update-record`）。
    直しが記録に残らないまま点だけ変わるのを防ぐ。"""
    import json

    from tools.bench_hands import RECORD, store_gt_fingerprints

    record = json.loads(RECORD.read_text(encoding="utf-8"))
    assert record["gt"] == store_gt_fingerprints()
