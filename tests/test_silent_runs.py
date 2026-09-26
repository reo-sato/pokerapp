"""tests/test_silent_runs.py

無言の連続（オーナー, 2026-09-26）: 連続する席のプレイヤーが同じアクション（コール / チェック / フォールド）を
したとき、ディーラーは 2 回目以降を言わない。

- 「チェック」「コール」のあとのベット / レイズは、次の手番の人のものとは限らない。仮にいちばん近い手番の人に
  置き、候補（そのとき手番を待っていた席）を持つ。RFID でフォールドした席は候補から外れる。
- 仮にベットにした席の札がラウンドの途中で離れたら（ベットのあとに手番は無い）、その席は無言のチェック /
  コールで、ベットはあとの席とみて記録を組み直す。
- ラウンドが終わって候補が 2 席以上残れば、そのベットを要確認にする（全員がコールしたときは音声と札では決め
  られない）。
- 言われなかったチェック / コールが直前に言われたアクションと同じなら無言の連続で、要確認にしない。違えば
  聞き取れなかったとみて要確認。
- 席・ポジションを言ったベット / レイズは、間の人を無言の連続（札が離れていればフォールド）とみる。
"""
from __future__ import annotations

import pytest

from core.events import RFIDEvent
from core.game_state import PlayerState

pytest.importorskip("pokerkit")

from integration.replay import replay_events  # noqa: E402
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
    def test_the_provisional_bettor_leaving_reinterprets_the_run(self, tmp_path):
        # 席4 チェック、席5 チェック（無言）、席6 ベット 600、席4 フォールド、席5 フォールド
        tb = _Table(tmp_path)
        _to_flop(tb)
        tb.say("チェック")
        tb.tick(tb.now + 2.0)
        tb.say("ベット 600")
        tb.tick(tb.now + 2.0)
        assert [a[:2] for a in _flop_actions(tb)] == [(4, "check"), (5, "bet")]   # 仮: 次の手番の席5
        tb.lift(4)
        tb.tick(tb.now + 4.0)
        tb.lift(5)
        tb.tick(tb.now + 16.0)
        assert [a[:3] for a in _flop_actions(tb)] == [
            (4, "check", 0), (5, "check", 0), (6, "bet", 600), (4, "fold", 0), (5, "fold", 0),
        ]
        silent = _flop_actions(tb)[1]
        assert silent[3] is False and "silent_run" in silent[4]
        (hand,) = tb.hands
        assert (hand.winner_seat, hand.winner_source, hand.pot_total) == (6, "fold", 1200)
        assert not hand.review_required
        assert any("レイズは席6 とみて記録を組み直しました" in n for n in tb.notices)

    def test_the_next_seat_really_bet(self, tmp_path):
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

    def test_everyone_calling_leaves_the_bettor_ambiguous(self, tmp_path):
        # 席4 チェック、（席5 か席6 が）ベット 600、コール、（無言のコール）→ ターン
        tb = _Table(tmp_path)
        _to_flop(tb)
        tb.say("チェック")
        tb.tick(tb.now + 2.0)
        tb.say("ベット 600")
        tb.tick(tb.now + 2.0)
        tb.say("コール")
        tb.tick(tb.now + 2.0)
        _board(tb, ["5h"])
        actions = _flop_actions(tb)
        assert [a[:3] for a in actions] == [
            (4, "check", 0), (5, "bet", 600), (6, "call", 600), (4, "call", 600),
        ]
        bet = actions[1]
        assert bet[3] is True and "silent_run_ambiguous(席5・席6)" in bet[4]
        assert actions[3][3] is False and "silent_repeat" in actions[3][4]    # 無言のコール
        assert tb.t._hand_needs_review                                          # noqa: SLF001

    def test_a_fold_after_the_bet_removes_that_seat_from_the_candidates(self, tmp_path):
        # 席4 チェック、席5 ベット 600、席6 フォールド、席4 コール → ターン: 席6 は候補から外れる = 席5 で確定
        tb = _Table(tmp_path)
        _to_flop(tb)
        tb.say("チェック")
        tb.tick(tb.now + 2.0)
        tb.say("ベット 600")
        tb.tick(tb.now + 2.0)
        tb.lift(6)
        tb.tick(tb.now + 4.0)
        tb.say("コール")
        tb.tick(tb.now + 2.0)
        _board(tb, ["5h"])
        actions = _flop_actions(tb)
        assert [a[:3] for a in actions] == [(4, "check", 0), (5, "bet", 600), (6, "fold", 0), (4, "call", 600)]
        assert actions[1][3] is False and not tb.t._hand_needs_review       # noqa: SLF001

    def test_replay_reproduces_the_reinterpretation(self, tmp_path):
        tb = _Table(tmp_path)
        _to_flop(tb)
        tb.say("チェック")
        tb.tick(tb.now + 2.0)
        tb.say("ベット 600")
        tb.tick(tb.now + 2.0)
        tb.lift(4)
        tb.tick(tb.now + 4.0)
        tb.lift(5)
        tb.tick(tb.now + 16.0)
        (live,) = tb.hands
        replayed = replay_events(
            tb.recorder.events, backend="pokerkit",
            players=[PlayerState(seat=s, name=f"P{s}", stack=10000) for s in (4, 5, 6)],
            sb=100, bb=200, session_id="replay", out_dir=tmp_path / "replay",
            auto_new_hand=True, auto_winner=True, rfid_folds=True,
        )
        assert ([(a.street, a.seat, a.action, a.amount) for a in replayed[0].actions]
                == [(a.street, a.seat, a.action, a.amount) for a in live.actions])
        assert replayed[0].winner_seat == 6

    def test_cards_coming_back_undo_the_reinterpretation(self, tmp_path):
        tb = _Table(tmp_path)
        _to_flop(tb)
        tb.say("チェック")
        tb.tick(tb.now + 2.0)
        tb.say("ベット 600")
        tb.tick(tb.now + 2.0)
        tb.lift(5)
        tb.tick(tb.now + 4.0)
        assert [a[:2] for a in _flop_actions(tb)][:3] == [(4, "check"), (5, "check"), (6, "bet")]
        tb.put(5, HOLES[5])                   # 席5 の札が戻った = 覗いていただけ
        tb.tick(tb.now + 1.0)
        assert [a[:2] for a in _flop_actions(tb)][:2] == [(4, "check"), (5, "bet")]
        assert any("取り消して" in n for n in tb.notices)


class TestImpliedRepeats:
    def test_silent_checks_before_the_next_street_are_not_reviewed(self, tmp_path):
        tb = _Table(tmp_path)
        _to_flop(tb)
        tb.say("チェック")
        tb.tick(tb.now + 2.0)
        _board(tb, ["5h"])
        actions = _flop_actions(tb)
        assert [a[:2] for a in actions] == [(4, "check"), (5, "check"), (6, "check")]
        assert all(a[3] is False and "silent_repeat" in a[4] for a in actions[1:])
        assert not tb.t._hand_needs_review                                     # noqa: SLF001

    def test_a_missing_call_after_a_bet_is_reviewed(self, tmp_path):
        tb = _Table(tmp_path)
        _to_flop(tb)
        tb.say("ベット 600")                  # 席4
        tb.tick(tb.now + 2.0)
        _board(tb, ["5h"])                    # 「コール」が 1 つも聞こえないままターン
        actions = _flop_actions(tb)
        assert [a[:2] for a in actions] == [(4, "bet"), (5, "call"), (6, "call")]
        assert all(a[3] is True and "silent_repeat" not in a[4] for a in actions[1:])

    def test_the_first_call_is_announced_the_rest_are_silent(self, tmp_path):
        tb = _Table(tmp_path)
        _to_flop(tb)
        tb.say("ベット 600")
        tb.tick(tb.now + 2.0)
        tb.say("コール")                      # 席5。席6 は無言
        tb.tick(tb.now + 2.0)
        _board(tb, ["5h"])
        actions = _flop_actions(tb)
        assert [a[:2] for a in actions] == [(4, "bet"), (5, "call"), (6, "call")]
        assert actions[2][3] is False and "silent_repeat" in actions[2][4]


class TestSpokenSeat:
    def test_a_spoken_position_bet_walks_over_silent_checks(self, tmp_path):
        tb = _Table(tmp_path)
        _to_flop(tb)
        tb.say("チェック")
        tb.tick(tb.now + 2.0)
        tb.say("BTN ベット 600")              # 席6。席5 は無言のチェック
        actions = _flop_actions(tb)
        assert [a[:3] for a in actions] == [(4, "check", 0), (5, "check", 0), (6, "bet", 600)]
        assert actions[1][3] is False and "silent_run(spoken)" in actions[1][4]
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
        tb.say("チェック")
        tb.tick(tb.now + 2.0)
        _board(tb, cards)


class TestWinnerToss:
    """ベットに全員が降りると、勝った人は素早く札を前に投げる（オーナー, 2026-09-26）。降りた人の札が先に
    離れているので、仮のベットの席の離脱を「無言のチェックだった」と解釈し直さない。"""

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

    def test_the_last_candidate_to_leave_is_the_bettor(self, tmp_path):
        # 席5 は無言のチェック、席6 がベット、席4 と席5 が降り、勝った席6 が札を投げた
        tb = _Table(tmp_path)
        self._bet_then(tb, [(0.0, 4), (1.0, 5), (2.0, 6)])
        (hand,) = tb.hands
        assert [a[:2] for a in _flop_actions(tb)] == [
            (4, "check"), (5, "check"), (6, "bet"), (4, "fold"), (5, "fold"),
        ]
        assert (hand.winner_seat, hand.review_required) == (6, False)

    def test_a_spoken_fold_keeps_the_order_when_the_cards_linger(self, tmp_path):
        # 席6 が「フォールド」と言われたが札は席に残り、席4 も（無言で）降り、勝った席5 が先に札を投げ、
        # 席6 の札はあとで片付いた
        tb = _Table(tmp_path)
        _to_flop(tb)
        tb.say("チェック")
        tb.tick(tb.now + 2.0)
        tb.say("ベット 600")
        tb.tick(tb.now + 2.0)
        tb.say("フォールド")                   # 席6 の番。札はまだ席にある
        spoken = tb.now
        tb.tick(tb.now + 0.5)
        tb.lift(4)                             # 席4 も降りた（同じアクションの 2 回目 = 言わない）
        tb.tick(tb.now + 1.0)
        tb.lift(5)                             # 勝った席5 が札を投げた
        tb.tick(tb.now + 3.0)
        tb.lift(6)                             # 席6 の札が片付いた
        tb.tick(tb.now + 20.0)
        (hand,) = tb.hands
        assert [a[:2] for a in _flop_actions(tb)] == [(4, "check"), (5, "bet"), (6, "fold"), (4, "fold")]
        assert (hand.winner_seat, hand.review_required) == (5, False)
        assert not any("組み直し" in n or "決められません" in n for n in tb.notices)
        fold = next(a for a in hand.actions if a.seat == 6 and a.action == "fold")
        assert fold.timestamp == tb.t._iso(spoken)                          # noqa: SLF001

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
        tb.say("ツーペア")
        (hand,) = tb.hands
        assert hand.winner_source in ("cards", "announced") and hand.review_required
