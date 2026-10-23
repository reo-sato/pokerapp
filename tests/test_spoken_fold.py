"""tests/test_spoken_fold.py

「フォールド」と言われたのに札が席に残っていた手番の席（店舗 2026-09-27, セッション c2cd4a53）。

店舗の通しテストで、BTN のレイズに SB が降り BB がコールした「フォールド、コール」が、SB の札が
席に残ったままだったため「コール」が SB に付き、以降のハンドが 3 人のまま記録された。
「フォールド」は手番の人にすぐには付けないが、同じ人の番のまま次のアクションが聞こえたときは、その人の
フォールドにする（記録した `spoken_fold` 信号で replay も同じ）。

札を持ったまま口頭で降りることは基本的に無い（オーナー 2026-10-06）。「フォールド」と言われても手番の人の札が
席に残ったまま次のストリートの札が置かれたら、その人は降りていない（チェック / コール, 要確認）。手番の人の札が
残ったまま別の席の札が離れたら、その語は離れた席のこと（手番の人は降ろさない）。
"""
from __future__ import annotations

import pytest

from core.game_state import PlayerState

pytest.importorskip("pokerkit")

from integration.replay import replay_events  # noqa: E402
from tests.test_rfid_folds import _Table  # noqa: E402
from tests.test_silent_runs import _board  # noqa: E402

STORE_HOLES = {4: ["3h", "Td"], 5: ["8c", "As"], 6: ["Jd", "Jh"]}


def _acts(tb: _Table) -> list[tuple]:
    return [(a.street, a.seat, a.action, a.amount) for a in tb.t._current_actions]   # noqa: SLF001


def _raise_then(tb: _Table) -> None:
    tb.deal(STORE_HOLES)
    tb.say("レイズ 600")                        # BTN（席6）が最初に動く
    tb.tick(tb.now + 2.0)


class TestFoldWordThenAction:
    def test_fold_then_call_in_one_breath_folds_the_actor(self, tmp_path):
        tb = _Table(tmp_path)
        _raise_then(tb)
        tb.say("フォールド、コール")                 # 席4 が降り、席5 がコール（札はどちらも席にある）
        tb.tick(tb.now + 1.0)
        assert _acts(tb) == [
            ("preflop", 6, "raise", 600), ("preflop", 4, "fold", 0), ("preflop", 5, "call", 400),
        ]
        fold = tb.t._current_actions[1]                                          # noqa: SLF001
        assert fold.actor_source == "spoken_fold" and fold.source["audio"] and not fold.needs_review
        assert fold.raw_text == "フォールド" and fold.reason == "spoken_fold_before_next_action"
        assert tb.t._spoken_folds == {}                                          # noqa: SLF001
        assert tb.gs.get_active_seats() == [5, 6]
        assert not any("組み直し" in n for n in tb.notices)

    def test_fold_word_with_cards_kept_is_not_a_fold_at_the_next_street(self, tmp_path):
        """「フォールド」のあと札が席に残ったまま次のストリートの札 = 降りていない（店舗 2026-10-06 1f838667
        ハンド 15・20: 「フォール」= コールの聞き違いで、ヘッズアップではハンドがそこで終わっていた。いまは語尾の「ド」が
        無い「フォール」はコールの聞き違いとして読む = tests/test_rfid_folds.py の TestGarbledCall）。"""
        tb = _Table(tmp_path)
        _raise_then(tb)
        tb.say("フォールド")
        tb.tick(tb.now + 6.0)
        assert _acts(tb) == [("preflop", 6, "raise", 600)]      # 札が残っているうちは入れない
        _board(tb, ["Qs", "9h", "9c"])
        assert _acts(tb)[:3] == [
            ("preflop", 6, "raise", 600), ("preflop", 4, "call", 500), ("preflop", 5, "call", 400),
        ]
        kept = tb.t._current_actions[1]                                          # noqa: SLF001
        assert kept.actor_source == "implied" and kept.needs_review
        assert kept.reason == "fold_word_but_cards_stayed_before_flop" and kept.raw_text == "フォールド"
        assert tb.gs.get_active_seats() == [4, 5, 6] and tb.gs.street == "flop"
        assert any("フォールドにしません" in n for n in tb.notices)

    def test_fold_word_just_before_the_next_street_is_still_a_fold(self, tmp_path):
        """語のすぐあと（札が離れたと分かる前）に次の札が置かれたときは、これまでどおりその人のフォールド。"""
        tb = _Table(tmp_path)
        _raise_then(tb)
        tb.say("フォールド")
        tb.tick(tb.now + 1.0)
        _board(tb, ["Qs", "9h", "9c"])
        assert _acts(tb)[1] == ("preflop", 4, "fold", 0)
        assert tb.t._current_actions[1].reason == "spoken_fold+implied_before_flop"   # noqa: SLF001

    def test_cards_leaving_after_the_word_still_use_the_departure(self, tmp_path):
        tb = _Table(tmp_path)
        _raise_then(tb)
        tb.say("フォールド")
        spoken = tb.now
        tb.tick(tb.now + 1.0)
        tb.lift(4)
        tb.tick(tb.now + 4.0)
        assert _acts(tb) == [("preflop", 6, "raise", 600), ("preflop", 4, "fold", 0)]
        fold = tb.t._current_actions[1]                                          # noqa: SLF001
        assert fold.actor_source == "rfid_departure" and fold.timestamp == tb.t._iso(spoken)   # noqa: SLF001
        tb.say("コール")
        tb.tick(tb.now + 1.0)
        assert _acts(tb)[-1] == ("preflop", 5, "call", 400)
        assert not any(a.actor_source == "spoken_fold" for a in tb.t._current_actions)   # noqa: SLF001

    def test_a_late_word_for_a_seat_that_already_left_is_not_a_new_fold(self, tmp_path):
        tb = _Table(tmp_path)
        _raise_then(tb)
        tb.lift(4)
        tb.tick(tb.now + 4.0)
        assert _acts(tb)[-1] == ("preflop", 4, "fold", 0)
        tb.say("フォールド、コール")                 # 「フォールド」は席4 のこと（言うのが遅れた）
        tb.tick(tb.now + 1.0)
        assert _acts(tb) == [
            ("preflop", 6, "raise", 600), ("preflop", 4, "fold", 0), ("preflop", 5, "call", 400),
        ]
        assert tb.gs.get_active_seats() == [5, 6]

    def _heads_up_bet(self, tb: _Table) -> None:
        _raise_then(tb)
        tb.say("フォールド、コール")
        tb.tick(tb.now + 2.0)
        _board(tb, ["Qs", "9h", "9c"])
        tb.say("チェック")
        tb.tick(tb.now + 2.0)
        tb.say("ベット 1000")
        tb.tick(tb.now + 2.0)

    def test_fold_out_by_the_word_then_the_winner_tosses(self, tmp_path):
        # ヘッズアップになったあと「フォールド」、降りた人が札を捨て、勝った人も札を投げる
        tb = _Table(tmp_path)
        self._heads_up_bet(tb)
        tb.say("フォールド")
        tb.tick(tb.now + 0.5)
        tb.muck(5)                                  # 降りた席5 が札を捨てた
        tb.tick(tb.now + 1.0)
        tb.lift(6)                                  # 勝った席6 も札を投げた
        tb.tick(tb.now + 15.0)
        (hand,) = tb.hands
        assert (hand.winner_seat, hand.winner_source) == (6, "fold")
        assert [(a.seat, a.action) for a in hand.actions][-2:] == [(6, "bet"), (5, "fold")]
        assert not any("確認してください" in n for n in tb.notices)

    def test_word_with_kept_cards_and_another_seat_leaving_is_not_the_actors_fold(self, tmp_path):
        """手番の席5 の札が残ったまま、別の席6 の札が離れた = 「フォールド」は席5 のことではない（店舗 2026-10-06:
        この形で手番の人を降ろした 3 回はすべて誤り）。席5 は降ろさない。判断は記録して再生も同じ。"""
        tb = _Table(tmp_path)
        self._heads_up_bet(tb)
        tb.say("フォールド")                         # 席5 の札は席に残っている
        tb.tick(tb.now + 1.0)
        tb.lift(6)
        tb.tick(tb.now + 15.0)
        assert tb.hands == []                       # ハンドは閉じない（席5 は降りていない）
        assert (5, "fold") not in [(a.seat, a.action) for a in tb.t._current_actions]   # noqa: SLF001
        assert any("席5 はフォールドにしません" in n for n in tb.notices)
        assert any(getattr(e, "kind", None) == "spoken_fold_drop" and e.seat == 5 for e in tb.recorder.events)
        tb.put(6, STORE_HOLES[6])                   # 次の配布で前のハンドを閉じる
        tb.deal({4: ["2c", "2d"], 5: ["3c", "3d"], 6: ["4c", "4d"]})
        live = tb.hands[0]
        replayed = replay_events(
            tb.recorder.events, backend="pokerkit",
            players=[PlayerState(seat=s, name=f"P{s}", stack=10000) for s in (4, 5, 6)],
            sb=100, bb=200, session_id="replay", out_dir=tmp_path / "replay",
            auto_new_hand=True, auto_winner=True, rfid_folds=True,
        )
        assert ([(a.street, a.seat, a.action, a.amount) for a in replayed[0].actions]
                == [(a.street, a.seat, a.action, a.amount) for a in live.actions])
        assert (5, "fold") not in [(a.seat, a.action) for a in replayed[0].actions]


class TestStoreHand:
    def test_store_hand_2026_09_27_reconstructs_heads_up(self, tmp_path):
        """店舗の 1 ハンド目。ターンの札は 20 秒読めなかった（声がターンの札より先に来る）。"""
        tb = _Table(tmp_path)
        _raise_then(tb)
        tb.tick(tb.now + 6.0)
        tb.say("フォールド、コール")
        tb.tick(tb.now + 15.0)
        _board(tb, ["Qs", "9h", "9c"])
        tb.tick(tb.now + 5.0)
        tb.say("チェック")
        tb.tick(tb.now + 3.0)
        tb.say("ベット 1000")
        tb.tick(tb.now + 3.0)
        tb.say("コール")
        tb.tick(tb.now + 13.0)
        tb.say("チェック")                           # ターン（札はまだ読めていない）
        tb.tick(tb.now + 2.0)
        tb.say("ベット 2500")
        tb.tick(tb.now + 6.0)
        tb.say("オールイン")
        tb.tick(tb.now + 3.0)
        tb.say("コール")
        tb.tick(tb.now + 2.0)
        _board(tb, ["Th"])
        _board(tb, ["8d"])
        tb.tick(tb.now + 30.0)
        tb.say("ハンド終了")
        tb.tick(tb.now + 1.0)
        (hand,) = tb.hands
        assert [(a.street, a.seat, a.action, a.amount) for a in hand.actions] == [
            ("preflop", 6, "raise", 600), ("preflop", 4, "fold", 0), ("preflop", 5, "call", 400),
            ("flop", 5, "check", 0), ("flop", 6, "bet", 1000), ("flop", 5, "call", 1000),
            ("turn", 5, "check", 0), ("turn", 6, "bet", 2500), ("turn", 5, "allin", 8400),
            ("turn", 6, "call", 5900),
        ]
        assert (hand.winner_seat, hand.winner_source, hand.pot_total) == (6, "cards", 20100)
        assert [p["seat"] for p in hand.players] == [4, 5, 6]
        assert not any("組み直し" in n or "決められません" in n for n in tb.notices)

    def test_replay_reproduces_the_spoken_fold(self, tmp_path):
        tb = _Table(tmp_path)
        _raise_then(tb)
        tb.say("フォールド、コール")
        tb.tick(tb.now + 2.0)
        _board(tb, ["Qs", "9h", "9c"])
        tb.say("チェック")
        tb.tick(tb.now + 2.0)
        tb.say("ベット 1000")
        tb.tick(tb.now + 2.0)
        tb.lift(5)                                  # 席5 が降りた（札が離れた）
        tb.tick(tb.now + 16.0)
        (live,) = tb.hands
        assert live.winner_seat == 6
        replayed = replay_events(
            tb.recorder.events, backend="pokerkit",
            players=[PlayerState(seat=s, name=f"P{s}", stack=10000) for s in (4, 5, 6)],
            sb=100, bb=200, session_id="replay", out_dir=tmp_path / "replay",
            auto_new_hand=True, auto_winner=True, rfid_folds=True,
        )
        assert ([(a.street, a.seat, a.action, a.amount) for a in replayed[0].actions]
                == [(a.street, a.seat, a.action, a.amount) for a in live.actions])
        assert [a.actor_source for a in replayed[0].actions][1] == "spoken_fold"
        assert replayed[0].winner_seat == 6
        assert any(getattr(e, "kind", None) == "spoken_fold" for e in tb.recorder.events)
