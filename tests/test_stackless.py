"""持ち点を数えないモード（オーナー 2026-10-24。リングでもトーナメントでも）: オールインはオールインとだけ記録し、
額を書かない。持ち点は追わない（`core/poker_engine.PokerkitGameState(stackless=True)` / `core/stackless.py`）。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("pokerkit")

from core.game_state import PlayerState  # noqa: E402
from core.poker_engine import STACKLESS_STACK, create_game_state  # noqa: E402
from core.stackless import allin_seats, mark_allin, strip_hand, unknown_amounts  # noqa: E402
from integration.replay import replay_events  # noqa: E402
from tools.measure_capture_accuracy import row_correct  # noqa: E402
from tools.play_hand_text import utterances_to_events  # noqa: E402

SCHEMAS = Path(__file__).parent.parent / "docs" / "contracts" / "schemas"


def _players(seats=(1, 2, 3), stack=0):
    return [PlayerState(seat=s, name=f"P{s}", stack=stack) for s in seats]


def _replay(tmp_path, lines, *, stackless=True, seats=(1, 2, 3), stack=0):
    replay_events(utterances_to_events(lines), backend="pokerkit", players=_players(seats, stack), sb=100, bb=200,
                  session_id="s", out_dir=tmp_path, stackless=stackless, auto_winner=True, close_open_hand=True)
    return json.loads((tmp_path / "s.json").read_text(encoding="utf-8"))["hands"]


class TestGameState:
    def test_allin_seat_is_skipped_and_the_others_keep_betting(self):
        gs = create_game_state("pokerkit", _players(), 100, 200, stackless=True)
        gs.new_hand()
        shover = gs.get_current_player()
        gs.apply_action(shover, "allin")
        assert shover not in gs.seats_to_act() and gs.allin_seats() == {shover}
        for _ in range(2):
            gs.apply_action(gs.get_current_player(), "call")
        assert gs.street == "flop"
        # 残りの 2 人はフロップで賭けを続けられる（オールインした人には手番が来ない）
        bettor = gs.get_current_player()
        assert bettor != shover
        gs.apply_action(bettor, "bet", 5000)
        caller = gs.get_current_player()
        assert caller not in (shover, bettor)
        gs.apply_action(caller, "call")
        assert gs.street == "turn" and gs.get_current_player() != shover

    def test_heads_up_allin_and_call_runs_the_board_out(self):
        gs = create_game_state("pokerkit", _players((1, 2)), 100, 200, stackless=True)
        gs.new_hand()
        gs.apply_action(gs.get_current_player(), "allin")
        gs.apply_action(gs.get_current_player(), "call")
        assert gs.legal_context().actor_seat is None and gs.is_hand_active()   # ショーダウン待ち

    def test_no_raise_when_every_other_seat_is_allin(self):
        """相手が全員オールインなら、手番の人はコールかフォールドだけ（内部ではオールインを最小のレイズにしているので
        pokerkit はレイズを許していた。店舗 2026-11-05 8b89a609 ハンド 59: 「24,200点です。」を席5 のレイズにしていた）。"""
        gs = create_game_state("pokerkit", _players(), 100, 200, stackless=True)
        gs.new_hand()
        first = gs.get_current_player()
        gs.apply_action(first, "fold")
        gs.apply_action(gs.get_current_player(), "allin")
        ctx = gs.legal_context()
        assert ctx.legal_actions == frozenset({"fold", "call"})
        with pytest.raises(ValueError):
            gs.apply_action(ctx.actor_seat, "raise", 24200)

    def test_an_allin_against_an_allin_is_recorded_as_a_call(self, tmp_path):
        hands = _replay(tmp_path, ["ハンド開始", "フォールド", "オールイン", "オールイン、コール", "シート1 ウィナー"])
        actions = [(a["seat"], a["action"]) for a in hands[0]["actions"]]
        assert actions[-1][1] == "call" and [a for _, a in actions].count("allin") == 1

    def test_a_bet_is_never_cut_by_a_stack_and_no_seat_busts(self):
        gs = create_game_state("pokerkit", _players(stack=0), 100, 200, stackless=True)
        assert gs.playing_seats() == [1, 2, 3]           # 持ち点 0 でも配る
        gs.new_hand()
        seat = gs.get_current_player()
        gs.apply_action(seat, "raise", 5_000_000)
        assert gs.committed(seat) == 5_000_000
        assert gs.stacks_before_next_hand() == {1: STACKLESS_STACK, 2: STACKLESS_STACK, 3: STACKLESS_STACK}


class TestRecord:
    def test_allin_hand_has_no_amounts_stacks_or_pot(self, tmp_path):
        hands = _replay(tmp_path, ["ハンド開始", "オールイン", "コール", "コール", "ベット 5000", "コール",
                                   "チェック", "チェック", "チェック", "チェック", "シート1 ウィナー"])
        hand = hands[0]
        assert hand["stackless"] is True and hand["pot_total"] is None and hand["pots"] == []
        rows = [(a["street"], a["action"], a["amount"]) for a in hand["actions"]]
        assert rows[:3] == [("preflop", "allin", None), ("preflop", "call", None), ("preflop", "call", None)]
        # オールインのない賭けの額はそのまま
        assert rows[3:5] == [("flop", "bet", 5000), ("flop", "call", 5000)]
        assert all(a["stack_after"] is None and a["pot_after"] is None for a in hand["actions"])
        assert all(p["stack_start"] is None and p["stack_end"] is None and p["result"] is None
                   for p in hand["players"])
        assert {p["seat"]: p["allin"] for p in hand["players"]} == {1: "won", 2: "lost", 3: "lost"}

    def test_a_seat_that_folded_before_the_allin_keeps_its_result(self, tmp_path):
        """オールインより前に出しただけで降りた席の収支は分かる（ハンド履歴に「-200」, 2026-11-05）。"""
        hands = _replay(tmp_path, ["ハンド開始", "レイズ 600", "コール", "フォールド", "オールイン", "コール",
                                   "シート1 ウィナー"])
        players = {p["seat"]: p for p in hands[0]["players"]}
        folded = next(s for s, p in players.items() if p.get("allin") is None)
        assert players[folded]["result"] == -200 and "allin" not in players[folded]
        assert sorted(p["allin"] for s, p in players.items() if s != folded) == ["lost", "won"]
        assert all(p["result"] is None for s, p in players.items() if s != folded)

    def test_hand_without_allin_keeps_amounts_pot_and_results(self, tmp_path):
        hands = _replay(tmp_path, ["ハンド開始", "レイズ 600", "コール", "フォールド", "ベット 900", "フォールド"])
        hand = hands[0]
        assert hand["stackless"] is True and hand["pot_total"] == 2300 and hand["winner_seat"] == 1
        assert [a["amount"] for a in hand["actions"] if a["action"] != "fold"] == [600, 500, 900]
        assert [a["pot_after"] for a in hand["actions"]][:2] == [900, 1400]
        assert {p["seat"]: p["result"] for p in hand["players"]} == {1: 800, 2: -200, 3: -600}
        assert all(p["stack_start"] is None for p in hand["players"])

    def test_results_do_not_carry_over_between_hands(self, tmp_path):
        hands = _replay(tmp_path, ["ハンド開始", "オールイン", "コール", "フォールド", "シート3 ウィナー",
                                   "ハンド開始", "レイズ 600", "コール", "フォールド", "ベット 900", "フォールド"])
        # 2 ハンド目（ボタンが 1 つ進む）: 前のハンドのオールインの仮の額を持ち越さない
        assert {p["seat"]: p["result"] for p in hands[1]["players"]} == {1: -600, 2: 800, 3: -200}

    def test_counting_mode_is_unchanged(self, tmp_path):
        hands = _replay(tmp_path, ["ハンド開始", "オールイン", "コール", "フォールド", "シート3 ウィナー"],
                        stackless=False, stack=10000)
        hand = hands[0]
        assert "stackless" not in hand and hand["actions"][0]["amount"] == 10000
        assert hand["pot_total"] == 20200                # 2 人の 10000 + 降りた BB の 200

    def test_record_fits_the_schema(self, tmp_path):
        jsonschema = pytest.importorskip("jsonschema")
        hand_schema = json.loads((SCHEMAS / "hand.schema.json").read_text(encoding="utf-8"))
        action_schema = json.loads((SCHEMAS / "action.schema.json").read_text(encoding="utf-8"))
        for hand in _replay(tmp_path, ["ハンド開始", "オールイン", "コール", "フォールド", "シート3 ウィナー"]):
            jsonschema.validate(hand, hand_schema)
            for a in hand["actions"]:
                jsonschema.validate(a, action_schema)


class TestStrip:
    def test_a_raise_after_the_allin_has_a_known_price(self):
        actions = [{"street": "preflop", "action": "allin"}, {"street": "preflop", "action": "raise"},
                   {"street": "preflop", "action": "call"}, {"street": "flop", "action": "call"}]
        assert [u for u, _ in unknown_amounts(actions)] == [True, False, False, False]
        assert [p for _, p in unknown_amounts(actions)] == [True, True, True, True]

    def test_allin_marks(self):
        hand = {"winner_seat": 2, "actions": [{"street": "preflop", "seat": 1, "action": "raise", "amount": 600},
                                              {"street": "preflop", "seat": 2, "action": "allin"},
                                              {"street": "preflop", "seat": 3, "action": "fold"},
                                              {"street": "preflop", "seat": 1, "action": "call"}],
                "players": [{"seat": s, "result": None} for s in (1, 2, 3)]}
        assert allin_seats(hand) == {1, 2}
        once = mark_allin(hand)
        assert {p["seat"]: p.get("allin") for p in once["players"]} == {1: "lost", 2: "won", 3: None}
        assert mark_allin(once) == once                                  # 冪等
        hand["winner_source"], hand["winner_seat"] = "undetermined", None
        assert {p["seat"]: p.get("allin") for p in mark_allin(hand)["players"]} == {1: "unknown", 2: "unknown",
                                                                                  3: None}
        assert allin_seats({"actions": [{"seat": 1, "action": "raise"}]}) == set()

    def test_truth_style_pot_awards(self):
        data = strip_hand({"actions": [{"street": "preflop", "action": "allin", "amount": 5}],
                           "players": [], "pot_awards": {"1": 10, "2": 10}, "pot_total": 20})
        assert data["pot_awards"] == {"1": None, "2": None} and data["pot_total"] is None


class TestScoring:
    def test_a_call_with_no_amount_matches_by_type(self):
        cap = {"seat": 2, "street": "preflop", "action": "call", "amount": None}
        assert row_correct({"seat": 2, "street": "preflop", "action": "call", "amount": 1200}, cap)
        assert row_correct({"seat": 2, "street": "preflop", "action": "allin", "amount": 800}, cap)
        assert not row_correct({"seat": 2, "street": "preflop", "action": "fold", "amount": 0}, cap)


class TestSetup:
    def test_setup_screen_turns_it_on_and_skips_stacks(self, tmp_path):
        from core.blind_structure import BlindStructureStore
        from tools import session_setup_ui as ui

        raw = {**ui.DEFAULT_SETUP, "seats": [1, 2], "stacks": {"1": "abc"}, "stackless": True}
        cfg = ui.build_session_config(raw, BlindStructureStore(tmp_path / "s.json"))
        assert cfg["table"]["stackless"] is True
        assert [p["stack"] for p in cfg["players"]] == [0, 0]
