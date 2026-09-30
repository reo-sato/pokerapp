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


def test_bench_format():
    assert (short_id("2026-09-30_165030_script_voice"), short_id("d0f055fb6fdd441082be18ced7cee437")) == (
        "165030", "d0f055fb")
    r = SourceResult("店舗", hands=18, default_exact=12, estimate_exact=13, in_nbest=13,
                     default_rows=[158, 188], estimate_rows=[159, 188], better=["7b897671#3"])
    text = "\n".join(format_result(r))
    assert "全部正しいハンド 読み直し 12/18（67%） → 推定 13/18（72%）" in text and "7b897671#3" in text
