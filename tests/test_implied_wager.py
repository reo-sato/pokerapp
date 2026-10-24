"""tests/test_implied_wager.py

言われなかった賭けを補う推定器の直し（オーナー 2026-10-24: 額の発話は有力な証拠だが、コール・フォールドなどが前の
アクションを示すなら、額をでっち上げて埋めてよい）: 挿入の値・ポット・額を除いた正しさ（速い組）と、開発データの
ハンドで補った賭けが選ばれ要確認が付くこと（遅い組）。
"""
from __future__ import annotations

import logging

import pytest

from core.bet_sizing import pot_before
from integration.estimator import _insert_steps, _insert_value
from tools.measure_capture_accuracy import hand_correct_ignoring_amounts, hand_fully_correct


def test_insert_value():
    assert _insert_value("call") == ("call", 0)
    assert _insert_value("bet:1200") == ("bet", 1200)
    assert _insert_steps("check,bet:800") == [("check", 0), ("bet", 800)]   # 札が離れた人のチェックのあとの賭け
    assert _insert_value("check,bet:800") == ("bet", 800)


def test_pot_before_street():
    rows = [{"street": "preflop", "seat": 1, "action": "raise", "amount": 600},
            {"street": "preflop", "seat": 2, "action": "call", "amount": 400},
            {"street": "flop", "seat": 2, "action": "check", "amount": 0}]
    blinds, pos = {"sb": 100, "bb": 200}, {"1": "SB", "2": "BB", "3": "BTN"}
    assert pot_before(rows, blinds, pos, 0) == 300
    assert pot_before(rows, blinds, pos, 2) == 1200        # SB が 600 まで上げ・BB が 400 足してそろえた（コールの額は上乗せ分）
    assert pot_before(rows, {}, pos, 2) is None


def test_an_unsaid_bet_needs_no_wager_word_around_it():
    """補った賭けの前（同じストリート）か、補った賭けとコールの間に賭けと読んだ語があれば、補う理由が無い。"""
    from types import SimpleNamespace

    from integration.estimator import SessionEstimator, _iso, _wager_before

    t0 = 1_000_000.0
    hand = {"actions": [{"street": "preflop", "action": "raise", "timestamp": _iso(t0)},
                        {"street": "flop", "action": "check", "timestamp": _iso(t0 + 10)},
                        {"street": "flop", "action": "bet", "timestamp": _iso(t0 + 12)},
                        {"street": "flop", "action": "call", "timestamp": _iso(t0 + 20)}]}
    assert _wager_before(hand, t0 + 15)                # 同じストリートに先の賭け
    assert not _wager_before(hand, t0 + 12)            # 補った行そのもの（時刻が同じ）は数えない
    assert not _wager_before(hand, t0 + 5)             # 前のストリートのレイズは数えない

    def tok(at, action):
        return SimpleNamespace(utterance_start_ts=at, action=action)

    follows = SessionEstimator._wager_word_follows
    assert follows([tok(t0 + 1, "bet"), tok(t0 + 2, "call")], t0)
    assert not follows([tok(t0 + 1, "call"), tok(t0 + 2, "bet")], t0)
    assert not follows([tok(t0 + 1, "raise")], t0)


def _hand(*rows, winner=1):
    return {"actions": [{"street": s, "seat": seat, "action": a, "amount": m} for s, seat, a, m in rows],
            "winner_seat": winner}


def test_correct_ignoring_amounts():
    truth = _hand(("flop", 1, "bet", 200), ("flop", 2, "call", 200))
    guessed = _hand(("flop", 1, "bet", 300), ("flop", 2, "call", 300))
    assert not hand_fully_correct(truth, guessed)
    assert hand_correct_ignoring_amounts(truth, guessed)
    assert not hand_correct_ignoring_amounts(truth, _hand(("flop", 1, "check", 0), ("flop", 2, "check", 0)))
    assert not hand_correct_ignoring_amounts(truth, _hand(("flop", 1, "bet", 300), ("flop", 2, "call", 300), winner=2))


@pytest.mark.slow
@pytest.mark.parametrize("key", ["9db1e032#3", "65c412b2#8"])
def test_unsaid_bet_before_a_heard_call_is_filled_and_flagged(key):
    """ベットの声が録れていないのに「コール」が聞こえたハンド: 前にベットを補い（額は推し量る）、流れが真と合い、
    要確認が付く（額は声に無い）。"""
    from integration.estimator import PARAMS, SessionEstimator
    from tools.bench_hands import scored_hands, v1_inputs

    logging.disable(logging.CRITICAL)
    try:
        pairs = [(i, p) for _n, ss in v1_inputs(store=True, script=False) for i, p in ss
                 if i.session_id.startswith(key.split("#")[0])]
        inp, presence = pairs[0]
        est = SessionEstimator(inp.events, inp.transcripts, presence, inp.setup, inp.flags, inp.session_id, PARAMS)
        (h,) = [x for x in scored_hands(inp, est) if x.key == key]
        result = est.estimate_hand(h.window)
    finally:
        logging.disable(logging.NOTSET)
    assert hand_correct_ignoring_amounts(h.truth, result.best.hand)
    assert any(":" in str(e.value) for e in result.best.edits if e.kind == "insert")
    assert any("言われなかった賭けの額を推し量った" in r for r in result.reasons)
