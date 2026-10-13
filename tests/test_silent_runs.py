"""tests/test_silent_runs.py

言われなかったアクション（オーナー, 2026-09-29）: **コール・チェックは毎回言う**運用にした（それまでの「連続する席の
同じアクションは 2 回目以降を言わない」運用 = 2026-09-26 は廃止。その前提でベットの席を仮に置き、札の離脱で組み直す
処理も外した）。フォールドは言わないことがある（札の離脱 = RFID で決める）。

- 「チェック」「コール」のあとのベット / レイズは、次の手番の人のもの。
- 言われなかったチェック / コールを補うのは、聞き取れなかったとき = 要確認（次のストリートの札・席を言ったベット・
  ショーダウンの前）。
- 席・ポジションを言ったベット / レイズは、間の人を、札が離れていればフォールド、残っていれば聞き取れなかった
  チェック / コール（要確認）とみる。
"""
from __future__ import annotations

import pytest

from core.events import RFIDEvent

pytest.importorskip("pokerkit")

from integration.engine import SHOWDOWN_MUCK_SEC  # noqa: E402
from tests.test_rfid_folds import _Table  # noqa: E402

# ボタン 席6: プリフロップは 6 → 4 → 5、フロップ以降は 4 → 5 → 6 の順。
HOLES = {4: ["6s", "Qs"], 5: ["Jh", "9c"], 6: ["Kd", "Qd"]}


def _board(tb: _Table, cards: list[str]) -> None:
    for index, card in enumerate(cards, start=len(tb.t._board_positions) + 1):   # noqa: SLF001
        ev = RFIDEvent(tag_id=card, card=card, reader_id="b", role="board", seat=None,
                       timestamp=tb.now, raw_tag_id=card, board_index=index)
        tb.recorder.record(ev)
        tb.t._process_rfid_event(ev)         # noqa: SLF001
    tb.tick(tb.now + 1.0)


def _to_flop(tb: _Table) -> None:
    tb.deal(HOLES)
    for text in ("コール", "コール", "チェック"):
        tb.say(text)
        tb.tick(tb.now + 1.0)
    _board(tb, ["Jd", "9d", "3d"])


def _flop_actions(tb: _Table) -> list[tuple]:
    actions = tb.hands[-1].actions if tb.hands else tb.t._current_actions   # noqa: SLF001
    return [(a.seat, a.action, a.amount, a.needs_review, a.reason) for a in actions if a.street == "flop"]


class TestBetAfterCheck:
    def test_the_bet_is_the_next_seats(self, tmp_path):
        # 席4 チェック、席5 ベット 600、席6 フォールド、席4 フォールド
        tb = _Table(tmp_path)
        _to_flop(tb)
        tb.say("チェック")
        tb.tick(tb.now + 2.0)
        tb.say("ベット 600")
        tb.tick(tb.now + 2.0)
        tb.lift(6)
        tb.tick(tb.now + 4.0)
        tb.lift(4)
        tb.tick(tb.now + 16.0)
        assert [a[:3] for a in _flop_actions(tb)] == [
            (4, "check", 0), (5, "bet", 600), (6, "fold", 0), (4, "fold", 0),
        ]
        (hand,) = tb.hands
        assert (hand.winner_seat, hand.pot_total, hand.review_required) == (5, 1200, False)

    def test_everyone_calling_the_bet(self, tmp_path):
        # 席4 チェック、席5 ベット 600、席6 コール、席4 コール → ターン。ベットの席は決まっている（要確認にしない）
        tb = _Table(tmp_path)
        _to_flop(tb)
        for text in ("チェック", "ベット 600", "コール", "コール"):
            tb.say(text)
            tb.tick(tb.now + 2.0)
        _board(tb, ["5h"])
        actions = _flop_actions(tb)
        assert [a[:3] for a in actions] == [
            (4, "check", 0), (5, "bet", 600), (6, "call", 600), (4, "call", 600),
        ]
        assert not any(a[3] for a in actions[1:])
        assert all("silent" not in (a[4] or "") for a in actions)

    def test_the_bettors_cards_leaving_do_not_move_the_bet(self, tmp_path):
        # 以前は「席5 の札がラウンドの途中で離れた = 席5 は無言のチェックで、ベットは席6」と組み直していた
        tb = _Table(tmp_path)
        _to_flop(tb)
        tb.say("チェック")
        tb.tick(tb.now + 2.0)
        tb.say("ベット 600")
        tb.tick(tb.now + 2.0)
        tb.lift(5)
        tb.tick(tb.now + 4.0)
        assert [a[:2] for a in _flop_actions(tb)][:2] == [(4, "check"), (5, "bet")]
        assert not any("組み直し" in n for n in tb.notices)


class TestUnannouncedActions:
    def test_unannounced_checks_before_the_next_street_are_reviewed(self, tmp_path):
        tb = _Table(tmp_path)
        _to_flop(tb)
        tb.say("チェック")
        tb.tick(tb.now + 2.0)
        _board(tb, ["5h"])
        actions = _flop_actions(tb)
        assert [a[:2] for a in actions] == [(4, "check"), (5, "check"), (6, "check")]
        assert all(a[3] is True and a[4] == "implied_before_turn" for a in actions[1:])

    def test_a_missing_call_after_a_bet_is_reviewed(self, tmp_path):
        tb = _Table(tmp_path)
        _to_flop(tb)
        tb.say("ベット 600")                  # 席4
        tb.tick(tb.now + 2.0)
        _board(tb, ["5h"])                    # 「コール」が 1 つも聞こえないままターン
        actions = _flop_actions(tb)
        assert [a[:2] for a in actions] == [(4, "bet"), (5, "call"), (6, "call")]
        assert all(a[3] is True for a in actions[1:])

    def test_a_second_call_that_was_not_heard_is_reviewed(self, tmp_path):
        tb = _Table(tmp_path)
        _to_flop(tb)
        tb.say("ベット 600")
        tb.tick(tb.now + 2.0)
        tb.say("コール")                      # 席5。席6 の「コール」は聞こえなかった
        tb.tick(tb.now + 2.0)
        _board(tb, ["5h"])
        actions = _flop_actions(tb)
        assert [a[:2] for a in actions] == [(4, "bet"), (5, "call"), (6, "call")]
        assert actions[2][3] is True and actions[2][4] == "implied_before_turn"


class TestSpokenSeat:
    def test_a_spoken_position_bet_fills_the_seat_in_between(self, tmp_path):
        tb = _Table(tmp_path)
        _to_flop(tb)
        tb.say("チェック")
        tb.tick(tb.now + 2.0)
        tb.say("BTN ベット 600")              # 席6。席5 の「チェック」は聞こえなかった
        actions = _flop_actions(tb)
        assert [a[:3] for a in actions] == [(4, "check", 0), (5, "check", 0), (6, "bet", 600)]
        assert actions[1][3] is True and actions[1][4] == "implied_before_spoken_seat"
        assert actions[2][3] is False

    def test_a_spoken_position_bet_folds_a_seat_whose_cards_left(self, tmp_path):
        tb = _Table(tmp_path)
        _to_flop(tb)
        tb.say("チェック")
        tb.tick(tb.now + 2.0)
        tb.lift(5)
        tb.tick(tb.now + 4.0)
        tb.say("BTN ベット 600")
        actions = _flop_actions(tb)
        assert [a[:3] for a in actions] == [(4, "check", 0), (5, "fold", 0), (6, "bet", 600)]


def _to_river_with_checks(tb: _Table) -> None:
    _to_flop(tb)
    for cards in (["5h"], ["2c"]):
        for _ in range(3):                    # 3 人とも「チェック」と言う（コール・チェックは毎回言う）
            tb.say("チェック")
            tb.tick(tb.now + 1.0)
        _board(tb, cards)


class TestWinnerToss:
    """ベットに全員が降りると、勝った人は素早く札を前に投げる（オーナー, 2026-09-26）。ベットした人の札の離脱は
    勝った人の札を前に出しただけ（フォールドにしない）。"""

    def _bet_then(self, tb: _Table, lifts: list[tuple[float, int]]) -> None:
        _to_flop(tb)
        tb.say("チェック")
        tb.tick(tb.now + 2.0)
        tb.say("ベット 600")
        tb.tick(tb.now + 2.0)
        start = tb.now
        for at, seat in sorted(lifts):
            tb.tick(start + at)
            tb.lift(seat)
        tb.tick(tb.now + 20.0)

    def test_the_bettor_tossing_after_the_folds_wins_without_review(self, tmp_path):
        tb = _Table(tmp_path)
        self._bet_then(tb, [(0.0, 6), (0.5, 4), (1.0, 5)])
        (hand,) = tb.hands
        assert [a[:2] for a in _flop_actions(tb)] == [(4, "check"), (5, "bet"), (6, "fold"), (4, "fold")]
        assert (hand.winner_seat, hand.review_required) == (5, False)
        assert not any("組み直し" in n or "要確認" in n for n in tb.notices)

    def test_a_folders_cards_collected_late_do_not_matter(self, tmp_path):
        # 席4 は「フォールド」のあと札が席に残り、ディーラーがあとで片付けた（席4 は候補ではない）
        tb = _Table(tmp_path)
        self._bet_then(tb, [(0.0, 6), (1.0, 5), (3.0, 4)])
        (hand,) = tb.hands
        assert [a[:2] for a in _flop_actions(tb)] == [(4, "check"), (5, "bet"), (6, "fold"), (4, "fold")]
        assert (hand.winner_seat, hand.review_required) == (5, False)
        assert not any("組み直し" in n or "決められません" in n for n in tb.notices)

    def test_a_fold_word_with_lingering_cards_belongs_to_the_seat_that_left(self, tmp_path):
        # 手番の席6 の札が席に残ったまま「フォールド」と言われ、すぐあとに席4 の札が離れた = 語は席4 のこと
        # （札を持ったまま口頭で降りることは基本的に無い, オーナー 2026-10-06）。勝った席5 が先に札を投げ、
        # 席6 の札はあとで離れた（席6 のフォールドはその時刻）。記録は同じで、要確認にしない
        tb = _Table(tmp_path)
        _to_flop(tb)
        tb.say("チェック")
        tb.tick(tb.now + 2.0)
        tb.say("ベット 600")
        tb.tick(tb.now + 2.0)
        tb.say("フォールド")                   # 席6 の番。札はまだ席にある
        tb.tick(tb.now + 0.5)
        tb.lift(4)                             # 席4 が降りた
        tb.tick(tb.now + 1.0)
        tb.lift(5)                             # 勝った席5 が札を投げた
        tb.tick(tb.now + 3.0)
        left = tb.now
        tb.lift(6)                             # 席6 の札が離れた
        tb.tick(tb.now + 20.0)
        (hand,) = tb.hands
        assert [a[:2] for a in _flop_actions(tb)] == [(4, "check"), (5, "bet"), (6, "fold"), (4, "fold")]
        assert (hand.winner_seat, hand.review_required) == (5, False)
        assert not any("組み直し" in n or "決められません" in n for n in tb.notices)
        assert any("席6 はフォールドにしません" in n for n in tb.notices)
        fold = next(a for a in hand.actions if a.seat == 6 and a.action == "fold")
        assert fold.timestamp == tb.t._iso(left)                            # noqa: SLF001

    def test_a_river_fold_out_waits_for_a_hand_name_then_confirms(self, tmp_path):
        tb = _Table(tmp_path)
        _to_river_with_checks(tb)
        tb.say("ベット 600")                   # 席4
        tb.tick(tb.now + 2.0)
        tb.lift(5)
        tb.tick(tb.now + 1.0)
        tb.lift(6)
        tb.tick(tb.now + 4.0)
        tb.lift(4)                             # 勝った席4 が札を投げた
        tb.tick(tb.now + 5.0)
        assert tb.hands == []                  # 役名・「ショーダウン」を待つ
        tb.tick(tb.now + 12.0)
        (hand,) = tb.hands
        assert (hand.winner_seat, hand.winner_source, hand.review_required) == (4, "fold", False)

    def test_a_hand_name_after_a_river_fold_out_means_a_showdown(self, tmp_path):
        tb = _Table(tmp_path)
        _to_river_with_checks(tb)
        tb.say("ベット 600")
        tb.tick(tb.now + 2.0)
        tb.lift(5)
        tb.tick(tb.now + 4.0)
        tb.lift(6)                             # 実は「コール」を聞き落として席6 が札を見せた
        tb.tick(tb.now + 1.0)
        tb.lift(4)
        tb.tick(tb.now + 4.0)
        tb.say("クイーンハイ")                 # 残った席4 の手 = ショーダウン（見せた）。もう 1 人の役名・マックを待つ
        assert tb.hands == []
        tb.tick(tb.now + SHOWDOWN_MUCK_SEC + 0.5)
        (hand,) = tb.hands
        assert hand.winner_source in ("cards", "announced") and hand.review_required
