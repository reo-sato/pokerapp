"""tests/test_auto_hand.py

ADR-0062: 店舗の音声テスト（2026-09-25）でハンドが一度も始まらなかった（`n` /「ハンド開始」を
言わない運用）ことへの対応。

- 手札が 2 席以上に配られたら新しいハンドを始める。配った札はそのハンドの手札に入る。
- 勝者: ほかが全員フォールドしたら残った人。ベッティングが終わったあとの「フォールド」はショーダウンの
  マック（一番アウトオブポジションから順）で、見せずにマックした人は手札が強くても負け。誰もマック
  しなければ「ハンド終了」か次の配布のときに、RFID の手札とボードで判定する（side pot も）。
- 仮名で書き起こされた数（「ベトナナ」= ベット 7）と「ゴール」= コール。
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional

import pytest

from audio.recognizer import parse_actions
from core.event_queue import make_audio_queue
from core.events import AudioEvent, RFIDEvent
from core.game_state import GameStateManager, PlayerState
from core.hand_log import HandSummary
from integration.engine import IntegrationThread
from output.json_writer import JsonWriter

pytest.importorskip("pokerkit")

from core.poker_engine import PokerkitGameState  # noqa: E402
from core.showdown import award_pots, evaluate_hands  # noqa: E402


class _Table:
    """配布（RFID）と読み上げ（音声）を時刻つきで流す。ボタンは最大の席（1 ハンド目）。"""

    def __init__(self, tmp_path: Path, seats=(2, 5, 8), stacks=None, *, backlog=None,
                 auto_new_hand=True, auto_winner=True, gs=None):
        stacks = stacks or {s: 1000 for s in seats}
        players = [PlayerState(seat=s, name=f"P{s}", stack=stacks[s]) for s in seats]
        self.gs = gs or PokerkitGameState(players, sb=5, bb=10)
        self.now = 0.0
        self.notices: list[str] = []
        self.actions: list = []
        self.hands: list[HandSummary] = []
        self.resets = 0
        self.t = IntegrationThread(
            audio_queue=make_audio_queue(), game_state=self.gs,
            json_writer=JsonWriter(tmp_path, "auto"), on_action=self.actions.append,
            on_hand=self.hands.append, stop_event=threading.Event(), clock=lambda: self.now,
            on_new_hand=self._reset, auto_new_hand=auto_new_hand, auto_winner=auto_winner,
            speech_backlog=backlog, on_notice=self.notices.append,
        )

    def _reset(self) -> None:
        self.resets += 1

    def seat(self, seat: int, card: str, ts: float, replaces: Optional[str] = None) -> None:
        self.now = ts
        self.t._process_rfid_event(RFIDEvent(   # noqa: SLF001
            tag_id=card, card=card, reader_id=f"seat{seat}", role="seat", seat=seat,
            timestamp=ts, raw_tag_id=card, replaces=replaces,
        ))

    def deal(self, hands: dict[int, str], ts: float) -> None:
        """手札を 1 枚ずつ順に配る（{席: "AsAd"}）。"""
        for i in range(2):
            for seat, cards in hands.items():
                if cards[2 * i:2 * i + 2]:
                    self.seat(seat, cards[2 * i:2 * i + 2], ts)
                    ts += 0.4

    def board(self, cards: str, ts: float, first: int = 1) -> None:
        for k in range(len(cards) // 2):
            self.now = ts + 0.3 * k
            card = cards[2 * k:2 * k + 2]
            self.t._process_rfid_event(RFIDEvent(   # noqa: SLF001
                tag_id=card, card=card, reader_id="board", role="board", seat=None,
                timestamp=self.now, raw_tag_id=card, board_index=first + k,
            ))

    def say(self, text: str, ts: float, spoken: Optional[float] = None) -> None:
        self.now = ts
        for event in parse_actions(text, confidence=0.9,
                                   utterance_start_ts=spoken if spoken is not None else ts - 1):
            event.timestamp = ts
            self.t._handle_audio_event(event)   # noqa: SLF001

    def idle(self, ts: float) -> None:
        """run() が音声を待って空振りしたとき（配布の開始を確かめる）。"""
        self.now = ts
        self.t._start_dealt_hand_if_ready()   # noqa: SLF001

    @property
    def applied(self) -> list:
        return [a for a in self.actions if a.actor_source != "unresolved"]


HANDS = {2: "AsAd", 5: "KdKc", 8: "QhQc"}
BOARD = "2c7d9sTh3h"


def _to_river(tb: _Table, start: float = 10.0, board: str = BOARD) -> None:
    """3 人（SB=2, BB=5, BTN=8）: preflop リンプ → flop / turn / river はチェックで回す。"""
    tb.say("コール", start)            # BTN 8
    tb.say("コール", start + 1)        # SB 2
    tb.say("チェック", start + 2)      # BB 5
    tb.board(board[:6], start + 5)
    tb.say("チェック チェック チェック", start + 10)
    tb.board(board[6:8], start + 15, first=4)
    tb.say("チェック チェック チェック", start + 20)
    tb.board(board[8:], start + 25, first=5)
    tb.say("チェック チェック チェック", start + 30)


def _heads_up_to_river(tb: _Table, river: str = "チェック チェック") -> None:
    """席8（BTN）がレイズ、席2（SB）がコール、席5（BB）がフォールド → 2 人でリバーまで。"""
    tb.say("レイズ 30、コール、フォールド", 10)
    tb.board(BOARD, 20)
    tb.say("チェック チェック", 30)    # flop: 2 → 8
    tb.say("チェック チェック", 35)    # turn
    tb.say(river, 40)                  # river


class TestHandStartsWhenCardsAreDealt:
    def test_dealing_starts_a_hand_and_keeps_the_cards(self, tmp_path):
        tb = _Table(tmp_path)
        tb.seat(2, "As", 1.0)
        assert tb.gs.hand_id == 0                       # 1 席だけではまだ始めない
        tb.seat(5, "Kd", 1.4)
        assert tb.gs.hand_id == 1 and tb.resets == 1    # RFID も同じ時点でリセット
        tb.deal({8: "Qh"}, 1.8)
        tb.seat(2, "Ad", 2.2)
        tb.seat(5, "Kc", 2.6)
        tb.seat(8, "Qc", 3.0)
        assert tb.t._hole_cards == {2: ["As", "Ad"], 5: ["Kd", "Kc"], 8: ["Qh", "Qc"]}   # noqa: SLF001
        assert tb.notices[0] == "ハンド 1 開始（手札が配られました / ボタン 席8）"
        assert tb.t._hand_started_epoch == 1.0          # noqa: SLF001 — 最初の札の時刻

    def test_actions_before_dealing_are_not_applied_but_after_are(self, tmp_path):
        tb = _Table(tmp_path)
        tb.say("コール", 0.5)
        assert tb.actions[-1].reason == "no_active_hand"
        tb.deal(HANDS, 1.0)
        tb.say("コール", 10)
        assert [(a.seat, a.action) for a in tb.applied] == [(8, "call")]

    def test_a_stray_card_during_cleanup_does_not_start_a_hand(self, tmp_path):
        tb = _Table(tmp_path)
        tb.seat(5, "Kd", 1.0)                           # 片付けの途中で 1 枚だけ読めた
        tb.seat(2, "As", 30.0)                          # 15 秒の窓を過ぎてから次の札
        assert tb.gs.hand_id == 0
        tb.seat(8, "Qh", 30.5)
        assert tb.gs.hand_id == 1
        assert 5 not in tb.t._hole_cards                # noqa: SLF001 — 古い札は入れない

    def test_cards_on_seats_outside_the_game_do_not_count(self, tmp_path):
        tb = _Table(tmp_path)
        tb.seat(3, "As", 1.0)
        tb.seat(4, "Kd", 1.4)
        assert tb.gs.hand_id == 0

    def test_saying_hand_start_after_the_deal_does_not_start_another_hand(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal(HANDS, 1.0)
        tb.say("ハンド開始", 5.0)
        assert tb.gs.hand_id == 1 and tb.gs.button_seat == 8
        assert "「ハンド開始」は不要です" in tb.notices[-1]

    def test_hand_start_said_before_dealing_takes_the_dealt_cards(self, tmp_path):
        tb = _Table(tmp_path)
        tb.say("ハンド開始", 0.5)
        tb.deal(HANDS, 1.0)
        assert tb.gs.hand_id == 1
        assert tb.t._hole_cards[8] == ["Qh", "Qc"]      # noqa: SLF001

    def test_redealing_before_any_action_stays_in_the_same_hand(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal(HANDS, 1.0)
        tb.seat(2, "Jh", 8.0, replaces="As")            # ミスディールで配り直し（ADR-0058）
        tb.seat(5, "Jd", 8.5, replaces="Kd")
        assert tb.gs.hand_id == 1
        assert tb.t._hole_cards[2] == ["Jh", "Ad"]      # noqa: SLF001

    def test_off_by_default(self, tmp_path):
        tb = _Table(tmp_path, auto_new_hand=False, auto_winner=False)
        tb.deal(HANDS, 1.0)
        assert tb.gs.hand_id == 0


class TestFoldOut:
    def test_everyone_folds_to_the_big_blind(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal(HANDS, 1.0)
        tb.say("フォールド、フォールド", 10)
        (hand,) = tb.hands
        assert hand.winner_seat == 5 and hand.winner_source == "fold"
        assert not hand.review_required
        assert tb.gs.get_stacks() == {2: 995, 5: 1005, 8: 1000}
        assert "ほかは全員フォールド" in tb.notices[-1]

    def test_the_next_deal_starts_the_next_hand_with_the_button_moved(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal(HANDS, 1.0)
        tb.say("フォールド、フォールド", 10)
        tb.deal({2: "7s7d", 5: "8s8d", 8: "9s9d"}, 60.0)
        assert tb.gs.hand_id == 2 and tb.gs.button_seat == 2
        assert tb.t._hole_cards == {2: ["7s", "7d"], 5: ["8s", "8d"], 8: ["9s", "9d"]}   # noqa: SLF001

    def test_a_late_winner_call_for_the_decided_hand_is_accepted(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal(HANDS, 1.0)
        tb.say("フォールド、フォールド", 10)
        tb.say("シート5 ウィナー", 12)
        assert len(tb.hands) == 1
        assert "確定しています" in tb.notices[-1]
        assert tb.actions[-1].actor_source != "unresolved"

    def test_a_conflicting_winner_call_is_shown_not_applied(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal(HANDS, 1.0)
        tb.say("フォールド、フォールド", 10)
        tb.say("シート8 ウィナー", 12)
        assert len(tb.hands) == 1 and tb.hands[0].winner_seat == 5
        assert tb.actions[-1].reason == "winner_after_hand_end"

    def test_a_typed_winner_right_after_the_next_deal_does_not_end_the_new_hand(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal(HANDS, 1.0)
        tb.say("フォールド、フォールド", 10)
        tb.deal({2: "7s7d", 5: "8s8d", 8: "9s9d"}, 60.0)
        tb.say("シート5 ウィナー", 65.0)               # 前のハンドのつもり
        assert len(tb.hands) == 1 and tb.t._hand_open   # noqa: SLF001


class TestShowdown:
    def test_hand_end_decides_the_winner_from_the_cards(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal(HANDS, 1.0)
        _to_river(tb)
        assert tb.hands == []                            # ショーダウンではすぐには決めない
        assert tb.notices[-1].startswith("ショーダウン（席2・席5・席8）")
        tb.say("ハンド終了", 50)
        (hand,) = tb.hands
        assert hand.winner_seat == 2 and hand.winner_source == "cards"
        assert [s["hand"] for s in hand.showdown] == ["One pair"] * 3
        assert hand.to_dict()["showdown"][0] == {
            "seat": 2, "hole_cards": ["As", "Ad"], "hand": "One pair",
            "best": ["As", "Ad", "7d", "9s", "Th"],
        }
        assert tb.gs.get_stacks() == {2: 1020, 5: 990, 8: 990}
        assert "ワンペア" in tb.notices[-1]

    def test_the_next_deal_decides_the_winner_when_nobody_says_anything(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal(HANDS, 1.0)
        _to_river(tb)
        tb.deal({2: "7s7d", 5: "8s8d", 8: "9s9d"}, 90.0)
        assert tb.hands[0].winner_seat == 2 and tb.hands[0].winner_source == "cards"
        assert tb.gs.hand_id == 2

    def test_out_of_position_mucking_a_better_hand_loses(self, tmp_path):
        # 2 人: 席2 がリバーでベット、席8 がコール → 席2（OOP）が見せずにマック。手札は席2 の方が強い。
        tb = _Table(tmp_path)
        tb.deal(HANDS, 1.0)
        _heads_up_to_river(tb, river="ベット 50、コール")
        assert tb.hands == []
        tb.say("フォールド", 45)
        (hand,) = tb.hands
        assert hand.winner_seat == 8 and hand.winner_source == "fold"
        muck = hand.actions[-1]
        assert (muck.seat, muck.action, muck.street) == (2, "fold", "showdown")
        assert "mucked_stronger_hand" in muck.reason and muck.needs_review
        assert hand.review_required

    def test_a_muck_by_the_weaker_hand_needs_no_review(self, tmp_path):
        tb = _Table(tmp_path, stacks={2: 1000, 5: 1000, 8: 1000})
        tb.deal({2: "QhQc", 5: "KdKc", 8: "AsAd"}, 1.0)
        _heads_up_to_river(tb)
        tb.say("フォールド", 45)                        # 席2（OOP, QQ）がマック
        (hand,) = tb.hands
        assert hand.winner_seat == 8 and not hand.review_required

    def test_a_spoken_seat_says_who_mucked(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal(HANDS, 1.0)
        _heads_up_to_river(tb)
        tb.say("シート8 フォールド", 45)
        assert tb.hands[0].winner_seat == 2

    def test_mucks_go_in_order_from_out_of_position_in_a_three_way_pot(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal(HANDS, 1.0)
        _to_river(tb)
        tb.say("フォールド", 50)                        # 席2（SB）
        assert tb.hands == [] and tb.t._showdown_mucks == [2]   # noqa: SLF001
        tb.say("フォールド", 52)                        # 席5（BB）
        (hand,) = tb.hands
        assert hand.winner_seat == 8
        assert "muck_order_assumed" in hand.actions[-2].reason

    def test_missing_cards_ask_for_the_winner(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal({2: "AsAd", 5: "KdKc", 8: "Qh"}, 1.0)   # 席8 の 2 枚目が読めていない
        _to_river(tb)
        tb.say("ハンド終了", 50)
        assert tb.hands == []
        assert "席8 の手札 1/2 枚" in tb.notices[-1] and "w <席>" in tb.notices[-1]
        tb.say("シート8 ウィナー", 55)
        assert tb.hands[0].winner_seat == 8 and tb.hands[0].winner_source is None

    def test_missing_cards_at_the_next_deal_record_an_estimated_winner(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal({2: "AsAd", 5: "KdKc", 8: "Qh"}, 1.0)
        _to_river(tb)
        tb.deal({2: "7s7d", 5: "8s8d", 8: "9s9d"}, 90.0)
        (hand,) = tb.hands
        assert hand.winner_source == "estimated" and hand.review_required
        assert tb.gs.hand_id == 2

    def test_hand_end_during_betting_points_at_the_missing_action(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal(HANDS, 1.0)
        tb.say("コール", 10)
        tb.say("ハンド終了", 12)
        assert tb.hands == []
        assert "まだベッティングの途中です（次は席2 の番）" in tb.notices[-1]

    def test_side_pot_goes_to_the_best_hand_among_those_in_it(self, tmp_path):
        # 席1（50 点）がオールインで AA、席2 が KK、席3 が QQ → main は席1、side は席2
        tb = _Table(tmp_path, seats=(1, 2, 3), stacks={1: 50, 2: 1000, 3: 1000})
        tb.deal({1: "AsAd", 2: "KdKc", 3: "QhQc"}, 1.0)
        tb.say("レイズ 200", 10)                        # BTN 3
        tb.say("コール", 11)                            # SB 1（オールイン）
        tb.say("コール", 12)                            # BB 2
        tb.board(BOARD, 20)
        tb.say("チェック チェック チェック チェック チェック チェック", 30)
        tb.say("ハンド終了", 40)
        (hand,) = tb.hands
        assert hand.pot_awards == [{"seat": 1, "amount": 150}, {"seat": 2, "amount": 300}]
        assert tb.gs.get_stacks() == {1: 150, 2: 1100, 3: 800}
        assert hand.winner_seat == 1

    def test_a_tie_splits_the_pot(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal({2: "AsKd", 5: "AhKc", 8: "2d3s"}, 1.0)
        _to_river(tb, board="QsJdTc4h5h")              # 席2 と席5 が同じストレート
        tb.say("ハンド終了", 50)
        (hand,) = tb.hands
        assert hand.winner_source == "cards" and hand.winner_seat == 2
        assert hand.pot_awards == [{"seat": 2, "amount": 15}, {"seat": 5, "amount": 15}]

    def test_speech_said_before_the_deal_goes_to_the_previous_hand(self, tmp_path):
        # 認識が遅れていて、配ったあとにリバーのマックが届く
        pending = {"n": 1}
        tb = _Table(tmp_path, backlog=lambda: pending["n"])
        tb.deal(HANDS, 1.0)
        _heads_up_to_river(tb)
        tb.deal({2: "7s7d", 5: "8s8d", 8: "9s9d"}, 60.0)
        assert tb.gs.hand_id == 1                        # 前の発話を待っている
        tb.say("フォールド", 63.0, spoken=50.0)          # 配る前に言ったマック
        assert tb.hands[0].winner_seat == 8 and tb.hands[0].winner_source == "fold"
        pending["n"] = 0
        tb.idle(64.0)
        assert tb.gs.hand_id == 2
        assert tb.t._hole_cards[8] == ["9s", "9d"]       # noqa: SLF001

    def test_speech_said_after_the_deal_starts_the_new_hand_first(self, tmp_path):
        tb = _Table(tmp_path, backlog=lambda: 1)
        tb.deal(HANDS, 1.0)
        tb.say("フォールド、フォールド", 10)
        tb.deal({2: "7s7d", 5: "8s8d", 8: "9s9d"}, 60.0)
        tb.say("コール", 70.0, spoken=68.0)
        assert tb.gs.hand_id == 2
        assert tb.applied[-1].seat == 2                  # ボタンが 2 に移った 2 ハンド目の UTG

    def test_an_unfinished_hand_is_closed_when_the_next_hand_is_dealt(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal(HANDS, 1.0)
        tb.say("コール", 10)                            # 以降のアクションが聞き取れなかった
        tb.board(BOARD[:6], 20)
        tb.deal({2: "7s7d", 5: "8s8d", 8: "9s9d"}, 90.0)
        assert len(tb.hands) == 1 and tb.hands[0].winner_source == "estimated"
        assert tb.gs.hand_id == 2
        assert tb.t._hole_cards[2] == ["7s", "7d"]       # noqa: SLF001

    def test_legacy_backend_does_not_decide_winners(self, tmp_path):
        players = [PlayerState(seat=s, name=f"P{s}", stack=1000) for s in (2, 5, 8)]
        tb = _Table(tmp_path, gs=GameStateManager(players, sb=5, bb=10))
        tb.say("ハンド開始", 1)
        tb.say("フォールド フォールド", 10)
        assert tb.hands == []


class TestShowdownHelpers:
    def test_award_pots_splits_odd_chips_from_the_button(self):
        hands = evaluate_hands({2: ["As", "Kd"], 5: ["Ah", "Kc"]}, ["Qs", "Jd", "Tc", "4h", "5h"])
        awards, winners = award_pots(
            [{"amount": 25, "eligible_seats": [2, 5]}], hands, order=[5, 2],
        )
        assert awards == {5: 13, 2: 12} and winners == [[5, 2]]

    def test_award_pots_skips_seats_without_a_hand(self):
        hands = evaluate_hands({2: ["As", "Ad"]}, ["2c", "7d", "9s", "Th", "3h"])
        awards, _ = award_pots([{"amount": 100, "eligible_seats": [2, 5]}], hands, order=[2, 5])
        assert awards == {2: 100}


class TestSpokenNumbers:
    @pytest.mark.parametrize("text, expected", [
        ("ベトナナ", [("bet", 7)]),                    # 店舗の実測
        ("レイズ ゴール", [("raise", 0), ("call", 0)]),  # 同上（「コール」→「ゴール」）
        ("ベット ロク", [("bet", 6)]),
        ("ベット にじゅうさん", [("bet", 23)]),
        ("レイズ サンビャク", [("raise", 300)]),
        ("ベット ロッピャク", [("bet", 600)]),
        ("レイズ センゴヒャク", [("raise", 1500)]),
        ("ベット イチマンニセン", [("bet", 12000)]),
        ("ベット ななです", [("bet", 7)]),
        ("ベット ゴー", [("bet", 5)]),
        ("ベットにします", [("bet", 0)]),               # 「に」は数ではない
        ("ベット ニサン", [("bet", 0)]),                # 数字が 2 つ並ぶ読みは数にしない
        ("ベット イッ", [("bet", 0)]),
        ("ベット6", [("bet", 6)]),
        ("ハンド終了", [("end_hand", 0)]),
    ])
    def test_amounts_are_the_numbers_said(self, text, expected):
        assert [(e.action, e.amount) for e in parse_actions(text)] == expected

    def test_an_ambiguous_man_reading_is_flagged(self):
        (event,) = parse_actions("ベット ヨンマンニ")
        assert event.amount == 42000 and "ambiguous_amount" in event.parse_flags


def test_the_cli_turns_both_on_by_default():
    import main

    kwargs = main._auto_hand_kwargs({}, None)   # noqa: SLF001
    assert kwargs == {"auto_new_hand": True, "auto_winner": True, "speech_backlog": None}
    off = main._auto_hand_kwargs(   # noqa: SLF001
        {"engine": {"auto_new_hand": False, "auto_winner": False}}, None)
    assert off["auto_new_hand"] is False and off["auto_winner"] is False


def test_notices_are_printed(capsys):
    import main

    main._print_notice("ハンド 1 開始")   # noqa: SLF001
    assert "● ハンド 1 開始" in capsys.readouterr().out


def test_end_hand_is_a_control_word():
    event = AudioEvent("end_hand", 0, 1.0, "ハンド終了")
    assert event.action == "end_hand"


def test_replay_can_rebuild_a_session_recorded_with_auto_hands(tmp_path):
    from integration.replay import replay_events

    def card(seat, c, ts, role="seat", index=None):
        return RFIDEvent(tag_id=c, card=c, reader_id=role, role=role, seat=seat, timestamp=ts,
                         raw_tag_id=c, board_index=index)

    def audio(action, ts, amount=0, text=""):
        return AudioEvent(action, amount, ts, text or action, confidence=0.9)

    events = [card(2, "As", 1.0), card(5, "Kd", 1.4), card(8, "Qh", 1.8),
              card(2, "Ad", 2.2), card(5, "Kc", 2.6), card(8, "Qc", 3.0),
              audio("fold", 10.0), audio("fold", 11.0)]
    players = [PlayerState(seat=s, name=f"P{s}", stack=1000) for s in (2, 5, 8)]
    kwargs = dict(backend="pokerkit", players=players, sb=5, bb=10, session_id="rp", out_dir=tmp_path)
    (hand,) = replay_events(events, auto_new_hand=True, auto_winner=True, **kwargs)
    assert hand.winner_seat == 5 and hand.winner_source == "fold"
    assert replay_events(events, **kwargs) == []          # 既定（従来）はハンドを始めない


def test_hands_decided_automatically_match_the_contract(tmp_path):
    jsonschema = pytest.importorskip("jsonschema")
    import json

    root = Path(__file__).resolve().parent.parent / "docs" / "contracts" / "schemas"
    hand_schema = json.loads((root / "hand.schema.json").read_text(encoding="utf-8"))
    action_schema = json.loads((root / "action.schema.json").read_text(encoding="utf-8"))

    side = _Table(tmp_path / "side", seats=(1, 2, 3), stacks={1: 50, 2: 1000, 3: 1000})
    side.deal({1: "AsAd", 2: "KdKc", 3: "QhQc"}, 1.0)
    side.say("レイズ 200、コール、コール", 10)
    side.board(BOARD, 20)
    side.say("チェック チェック チェック チェック チェック チェック", 30)
    side.say("ハンド終了", 40)

    muck = _Table(tmp_path / "muck")
    muck.deal(HANDS, 1.0)
    _heads_up_to_river(muck, river="ベット 50、コール")
    muck.say("フォールド", 45)

    for hand in (side.hands[0], muck.hands[0]):
        data = hand.to_dict()
        jsonschema.validate(data, hand_schema)
        for action in data["actions"]:
            jsonschema.validate(action, action_schema)
    assert side.hands[0].to_dict()["winner_source"] == "cards"
    assert muck.hands[0].to_dict()["actions"][-1]["street"] == "showdown"
