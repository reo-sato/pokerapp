"""tests/test_call_totals.py

コールの額は画面ではトータル（その人がそのストリートで出した合計）で見せる（オーナー, 2026-09-29）。
記録の `amount` は追加額のまま（真のアクション・計測も追加額）。`core/hand_log.py` の `street_flow` /
`shown_amount` と、CLI・GUI が使う `IntegrationThread.street_total`。スマホ・スタッフ画面の
`shared/hand_replay/handReplayModel.ts`（`streetFlow`）と同じ計算（同じ例で確かめる）。
"""
from __future__ import annotations

import copy

import pytest

from core.hand_log import ActionRecord, hand_stack_start, hand_street_flow, shown_amount, street_totals

# 店舗 d0f055fb ハンド 8 の形（ボタン 席4、SB 席5、BB 席6）
HAND = {
    "blinds": {"sb": 100, "bb": 200},
    "position_map": {"4": "BTN", "5": "SB", "6": "BB"},
    "players": [
        {"seat": 4, "stack_start": 10500}, {"seat": 5, "stack_start": 13800}, {"seat": 6, "stack_start": 5700},
    ],
    "actions": [
        {"street": "preflop", "seat": 4, "action": "raise", "amount": 900, "stack_after": 9600},
        {"street": "preflop", "seat": 5, "action": "call", "amount": 800, "stack_after": 12900},   # SB: 100 + 800
        {"street": "preflop", "seat": 6, "action": "allin", "amount": 5700, "stack_after": 0},
        {"street": "preflop", "seat": 4, "action": "call", "amount": 4800, "stack_after": 4800},
        {"street": "preflop", "seat": 5, "action": "allin", "amount": 13800, "stack_after": 0},
        {"street": "preflop", "seat": 4, "action": "call", "amount": 4800, "stack_after": 0},     # 足りないオールインのコール
        {"street": "flop", "seat": 4, "action": "check", "amount": 0},                            # stack_after が無い行
    ],
}


def _totals(hand: dict) -> list:
    return [total for _, total in hand_street_flow(hand)]


class TestStreetFlow:
    def test_a_call_is_what_the_player_put_in_on_that_street(self):
        assert _totals(HAND) == [900, 900, 5700, 5700, 13800, 10500, 0]
        assert street_totals(HAND["actions"], hand_stack_start(HAND))[:6] == [900, 900, 5700, 5700, 13800, 10500]
        shown = [shown_amount(a["action"], a["amount"], t) for a, t in zip(HAND["actions"], _totals(HAND))]
        assert shown == [900, 900, 5700, 5700, 13800, 10500, 0]

    def test_the_next_street_starts_from_zero(self):
        hand = {
            "players": [{"seat": 1, "stack_start": 10000}, {"seat": 2, "stack_start": 3000}],
            "actions": [
                {"street": "flop", "seat": 1, "action": "bet", "amount": 1000, "stack_after": 9000},
                {"street": "flop", "seat": 2, "action": "call", "amount": 1000, "stack_after": 2000},
                {"street": "turn", "seat": 1, "action": "bet", "amount": 5000, "stack_after": 4000},
                {"street": "turn", "seat": 2, "action": "allin", "amount": 2000, "stack_after": 0},
            ],
        }
        totals = _totals(hand)
        assert totals == [1000, 1000, 5000, 2000]
        # 足りないオールインのコール（記録は追加額）もトータル
        assert shown_amount("allin", 2000, totals[3]) == 2000

    def test_shown_amount_falls_back_to_the_record(self):
        assert shown_amount("call", 400, None) == 400          # 持ち点が分からない
        assert shown_amount("call", 500, 300) == 500           # 合わない（訂正で額だけ変わった等）
        assert shown_amount("raise", 1800, 1800) == 1800
        assert shown_amount("bet", 600, None) == 600
        assert shown_amount("check", 0, 200) == 0              # BB のチェック（合計 200）は額を出さない

    def test_corrected_rows_use_what_was_in_before_plus_the_corrected_amount(self):
        """訂正（ADR-0036）は持ち点を変えないので、前に出していた額 + 訂正後の額（TS の streetFlow と同じ例）。"""
        base = copy.deepcopy(HAND)
        base["actions"] = base["actions"][:2] + [
            {"street": "preflop", "seat": 6, "action": "fold", "amount": 0, "stack_after": 5500},
        ]
        assert hand_street_flow(base) == [(0, 900), (100, 900), (200, 200)]

        raised = copy.deepcopy(base)                          # SB のコール → レイズ 2700
        raised["actions"][1].update({"action": "raise", "amount": 2700, "_original": {"action": "call", "amount": 800}})
        assert hand_street_flow(raised)[1] == (100, 2700)

        called = copy.deepcopy(base)                          # BTN のレイズ 900 → コール（追加 200）
        called["actions"][0].update({"action": "call", "amount": 200, "_original": {"action": "raise", "amount": 900}})
        assert hand_street_flow(called)[0] == (0, 200)

        amended = copy.deepcopy(base)                         # SB のコールの額だけ 800 → 700
        amended["actions"][1].update({"amount": 700, "_original": {"amount": 800}})
        assert hand_street_flow(amended)[1] == (100, 800)

        sb_raise = copy.deepcopy(base)                        # SB のレイズをコールに: ブラインドはポジションから
        sb_raise["actions"][1] = {"street": "preflop", "seat": 5, "action": "call", "amount": 800,
                                  "stack_after": 11100, "_original": {"action": "raise", "amount": 2700}}
        assert hand_street_flow(sb_raise)[1] == (100, 900)

    def test_heads_up_the_button_posts_the_small_blind(self):
        hand = {
            "blinds": {"sb": 100, "bb": 200},
            "position_map": {"1": "BTN", "2": "BB"},
            "players": [{"seat": 1, "stack_start": 5000}, {"seat": 2, "stack_start": 5000}],
            "actions": [{"street": "preflop", "seat": 1, "action": "call", "amount": 100,
                         "_original": {"action": "raise", "amount": 600}}],
        }
        assert hand_street_flow(hand) == [(100, 200)]

    def test_unknown_positions_and_bad_rows(self):
        hand = {
            "players": [{"seat": 1, "stack_start": 5000}, {"seat": "x", "stack_start": 1}, {"seat": 2}],
            "actions": [
                {"street": "preflop", "seat": 1, "action": "raise", "amount": 600},    # 持ち点もポジションも無い
                {"street": "preflop", "seat": None, "action": "call", "amount": 600},
            ],
        }
        assert hand_stack_start(hand) == {1: 5000}
        assert hand_street_flow(hand) == [(None, 600), (None, None)]


class TestLiveEngine:
    def test_the_engine_reports_the_street_total_for_its_records(self, tmp_path):
        pytest.importorskip("pokerkit")
        from tests.test_rfid_folds import _Table

        tb = _Table(tmp_path)
        tb.deal()
        tb.say("レイズ 600")                     # 席6（BTN）
        tb.say("コール")                         # 席4（SB）: 追加 500
        tb.say("コール")                         # 席5（BB）: 追加 400
        records = tb.t._current_actions          # noqa: SLF001
        assert [(r.seat, r.action, r.amount) for r in records] == [(6, "raise", 600), (4, "call", 500), (5, "call", 400)]
        assert [tb.t.street_total(r) for r in records] == [600, 600, 600]
        other = ActionRecord(
            hand_id=1, timestamp="", street="preflop", seat=4, player_name="P4", action="call", amount=500,
            pot_after=0, stack_after=0, source={}, needs_review=False, confidence=0.5,
        )
        assert tb.t.street_total(other) is None  # いまのハンドの記録に無い行（未適用など）
