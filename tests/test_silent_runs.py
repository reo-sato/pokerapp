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
