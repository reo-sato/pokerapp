"""tests/test_spoken_fold.py

「フォールド」と言われたのに札が席に残っていた手番の席（店舗 2026-09-27, セッション c2cd4a53）。

店舗の通しテストで、BTN のレイズに SB が降り BB がコールした「フォールド、コール」が、SB の札が
席に残ったままだったため「コール」が SB に付き、以降のハンドが 3 人のまま記録された。
「フォールド」は手番の人にすぐには付けないが、同じ人の番のまま次のアクションが聞こえたとき・
次のストリートの札が置かれたときは、その人のフォールドにする（記録した `spoken_fold` 信号で replay も同じ）。
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

    def test_fold_alone_is_applied_when_the_next_street_comes(self, tmp_path):
        tb = _Table(tmp_path)
        _raise_then(tb)
        tb.say("フォールド")
        tb.tick(tb.now + 5.0)
        assert _acts(tb) == [("preflop", 6, "raise", 600)]      # 札が残っているうちは入れない
        _board(tb, ["Qs", "9h", "9c"])
        assert _acts(tb)[:3] == [
            ("preflop", 6, "raise", 600), ("preflop", 4, "fold", 0), ("preflop", 5, "call", 400),
        ]
        fold, call = tb.t._current_actions[1:3]                                  # noqa: SLF001
        assert fold.actor_source == "spoken_fold" and fold.reason == "spoken_fold+implied_before_flop"
        assert call.actor_source == "implied" and call.needs_review              # 「コール」は言われていない
        assert tb.gs.street == "flop"

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

    def test_fold_out_by_the_word_then_the_winner_tosses(self, tmp_path):
        # ヘッズアップになったあと「フォールド」、次の発話は無く勝った人が札を投げる
        tb = _Table(tmp_path)
        _raise_then(tb)
        tb.say("フォールド、コール")
        tb.tick(tb.now + 2.0)
        _board(tb, ["Qs", "9h", "9c"])
        tb.say("チェック")
        tb.tick(tb.now + 2.0)
        tb.say("ベット 1000")
        tb.tick(tb.now + 2.0)
        tb.say("フォールド")                         # 席5 が降りた（札は席に残っている）
        tb.tick(tb.now + 1.0)
        tb.lift(6)                                  # 勝った席6 が札を投げた（席5 の札より先に離れる）
        tb.tick(tb.now + 15.0)
        (hand,) = tb.hands
        assert (hand.winner_seat, hand.winner_source, hand.review_required) == (6, "fold", False)
        assert [(a.seat, a.action) for a in hand.actions][-2:] == [(6, "bet"), (5, "fold")]
        assert hand.actions[-1].actor_source == "spoken_fold"
        assert not any("確認してください" in n for n in tb.notices)


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
