"""tests/test_store_2026_09_27.py

店舗の通しテスト 2026-09-27（6 セッション）のレビューで見つかったこと:

- 途中で `q` を押したハンドが記録に残らなかった（2 ハンド）→ 終了時に次の配布と同じ規則で保存する。
- 聞き取りの取りこぼし: 疑問形の文のあとの「600」/「3200円」/ 書き起こしゆれ（オーリン・ソーダウン・
  シーン = 千・参戦 = 3 千・発表 = 8 百）/ 会話の中の「コール」を記録した。
- ディーラーは残りが 2 人になると「ヘッズアップ」と言う → 言われなかったコールの裏付けに使う。
- ベッティングの途中の「ショーダウン」のあとの札の離脱をフォールドにしていた。
- 最後のハンド（2e658148）はオーナーが真のアクションと確認済み = 回帰テスト。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from audio.recognizer import is_question, parse_actions
from audio.recorder import describe_event
from core.game_state import PlayerState

pytest.importorskip("pokerkit")

from integration.replay import load_events, replay_events  # noqa: E402
from tests.test_rfid_folds import _Table  # noqa: E402
from tests.test_silent_runs import _board  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "store" / "2026-09-27-2e658148"
STORE_HOLES = {4: ["2h", "5d"], 5: ["3c", "Td"], 6: ["4c", "Tc"]}


def _parsed(text: str) -> list[tuple]:
    return [(e.action, e.amount, tuple(e.parse_flags)) for e in parse_actions(text, confidence=0.5)]


def _acts(tb: _Table) -> list[tuple]:
    return [(a.street, a.seat, a.action, a.amount) for a in tb.t._current_actions]   # noqa: SLF001


class TestParsing:
    def test_a_question_sentence_does_not_swallow_the_amount_after_it(self):
        assert _parsed("600だけもう一回言ってもらっていいですか? 600") == [("bet", 600, ("amount_only",))]
        assert is_question("コールですか？") and is_question("何だっけ?")
        assert not is_question("600だけもう一回言ってもらっていいですか? 600")
        assert _parsed("コールですか？ はい、コール") == [("call", 0, ())]
        assert _parsed("レイズ 2400 でよろしいですか？") == []

    def test_yen_after_an_amount(self):
        assert _parsed("3200円") == [("bet", 3200, ("amount_only",))]

    @pytest.mark.parametrize("text, amount", [("シーン", 1000), ("参戦!", 3000), ("発表!", 800), ("セン。", 1000)])
    def test_misheard_amounts_said_alone(self, text, amount):
        assert _parsed(text) == [("bet", amount, ("amount_only",))]

    def test_misheard_amount_words_inside_other_text_are_not_amounts(self):
        assert _parsed("シーンとしてます") == [] and _parsed("発表します") == []

    @pytest.mark.parametrize("text, action", [
        ("オーリン", "allin"), ("ソーダウン", "showdown"), ("ヘッドアップ!", "heads_up"),
        ("ヘッズアップ", "heads_up"), ("ヘッドホップ", "heads_up"),
    ])
    def test_store_spellings(self, text, action):
        assert [a for a, _, _ in _parsed(text)] == [action]

    def test_heads_up_is_described_in_japanese(self):
        (event,) = parse_actions("ヘッドアップ!")
        assert describe_event(event) == "ヘッズアップ（残り 2 人）"

    @pytest.mark.parametrize("text", [
        "結構コールとか迷路に発音してますよね。",
        "コールの場所の問題でここが読めないんですよね",
    ])
    def test_action_words_inside_conversation_are_ignored(self, text):
        assert _parsed(text) == []

    @pytest.mark.parametrize("text, expected", [
        ("はい、コールです", [("call", 0, ())]),
        ("ショーダウンです。", [("showdown", 0, ())]),
        ("えーと、ベット600点です", [("bet", 600, ())]),
        ("はい、ではフロップを開きます、チェック", [("check", 0, ())]),
        ("チェック、チェックアランド", [("check", 0, ()), ("check", 0, ("check_around",))]),
    ])
    def test_short_dealer_phrases_are_not_conversation(self, text, expected):
        assert _parsed(text) == expected


class TestHeadsUp:
    def _raise(self, tb: _Table) -> None:
        tb.deal(STORE_HOLES)
        tb.say("レイズ 600")
        tb.tick(tb.now + 1.0)

    def test_heads_up_confirms_the_unannounced_call(self, tmp_path):
        tb = _Table(tmp_path)
        self._raise(tb)
        tb.lift(4)
        tb.tick(tb.now + 4.0)
        tb.say("ヘッドアップ!")
        tb.tick(tb.now + 1.0)
        assert _acts(tb) == [("preflop", 6, "raise", 600), ("preflop", 4, "fold", 0), ("preflop", 5, "call", 400)]
        call = tb.t._current_actions[-1]                                          # noqa: SLF001
        assert (call.actor_source, call.needs_review, call.reason) == ("implied", False, "heads_up_call")

    def test_heads_up_after_the_flop_confirms_the_call_made_at_the_flop(self, tmp_path):
        tb = _Table(tmp_path)
        self._raise(tb)
        tb.lift(4)
        tb.tick(tb.now + 4.0)
        _board(tb, ["7d", "6h", "Qc"])               # 札が先 = フロップの札でコールを補う（要確認）
        call = tb.t._current_actions[-1]                                          # noqa: SLF001
        assert (call.action, call.needs_review) == ("call", True)
        tb.say("ヘッドアップ!")
        tb.tick(tb.now + 1.0)
        assert call.needs_review is False and call.reason.endswith("+heads_up")

    def test_heads_up_applies_a_pending_spoken_fold(self, tmp_path):
        tb = _Table(tmp_path)
        self._raise(tb)
        tb.say("フォールド")                          # 席4 の札は席に残っている
        tb.tick(tb.now + 1.0)
        tb.say("ヘッズアップ")
        tb.tick(tb.now + 1.0)
        assert _acts(tb) == [("preflop", 6, "raise", 600), ("preflop", 4, "fold", 0), ("preflop", 5, "call", 400)]
        assert [a.actor_source for a in tb.t._current_actions[1:]] == ["spoken_fold", "implied"]   # noqa: SLF001
        assert not any(a.needs_review for a in tb.t._current_actions[1:])                         # noqa: SLF001

    def test_heads_up_with_three_players_left_is_flagged(self, tmp_path):
        tb = _Table(tmp_path)
        self._raise(tb)
        tb.say("ヘッズアップ")
        tb.tick(tb.now + 1.0)
        assert _acts(tb) == [("preflop", 6, "raise", 600)]
        assert tb.t._hand_needs_review                                            # noqa: SLF001
        assert any("3 人残っています" in n for n in tb.notices)


class TestShowdownWord:
    def test_showdown_closes_the_betting_so_cards_leaving_are_not_folds(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal(STORE_HOLES)
        tb.say("レイズ 600")
        tb.tick(tb.now + 1.0)
        tb.say("フォールド、コール")
        tb.tick(tb.now + 1.0)
        _board(tb, ["7d", "6h", "Qc"])
        tb.say("チェック")
        tb.tick(tb.now + 1.0)
        tb.say("ショーダウンです。")                    # 席6 の番のまま「ショーダウン」
        tb.tick(tb.now + 1.0)
        assert any("閉じていないベッティング" in n for n in tb.notices)
        tb.lift(6)                                  # 札を前に出した（フォールドではない）
        tb.tick(tb.now + 5.0)
        _board(tb, ["6c", "Ac"])
        tb.say("ハンド終了")
        tb.tick(tb.now + 1.0)
        (hand,) = tb.hands
        assert not any(a.seat == 6 and a.action == "fold" for a in hand.actions)
        assert hand.winner_source == "cards" and hand.winner_seat == 6        # 6c で席6 は 6 のペア（席5 は T 高）


class TestQuitSavesTheHand:
    def test_quit_in_the_middle_of_the_river_saves_the_hand(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal(STORE_HOLES)
        tb.say("レイズ 600")
        tb.tick(tb.now + 1.0)
        tb.say("フォールド、コール")
        tb.tick(tb.now + 1.0)
        for cards in (["7d", "6h", "Qc"], ["6c"], ["Ac"]):
            _board(tb, cards)
            tb.say("チェック")
            tb.tick(tb.now + 2.0)
        tb.say("2千")                                 # リバーで席6 がベット… ここで終了（席5 の番）
        tb.tick(tb.now + 1.0)
        assert tb.hands == []
        tb.t._close_open_hand_at_stop()                                            # noqa: SLF001
        (hand,) = tb.hands
        assert hand.review_required
        assert any(getattr(e, "action", None) == "session_end" for e in tb.recorder.events)
        replayed = replay_events(
            tb.recorder.events, backend="pokerkit",
            players=[PlayerState(seat=s, name=f"P{s}", stack=10000) for s in (4, 5, 6)],
            sb=100, bb=200, session_id="replay", out_dir=tmp_path / "replay",
            auto_new_hand=True, auto_winner=True, rfid_folds=True,
        )
        assert [(a.street, a.seat, a.action, a.amount) for a in replayed[0].actions] == [
            (a.street, a.seat, a.action, a.amount) for a in hand.actions
        ]
        assert (replayed[0].winner_seat, replayed[0].winner_source) == (hand.winner_seat, hand.winner_source)

    def test_quit_without_a_hand_in_play_saves_nothing(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal(STORE_HOLES)
        tb.t._close_open_hand_at_stop()                                            # noqa: SLF001
        assert tb.hands == []
        assert not any(getattr(e, "action", None) == "session_end" for e in tb.recorder.events)


class TestConfirmedStoreHand:
    def test_replay_reproduces_the_owner_confirmed_hand(self, tmp_path):
        expected = json.loads((FIXTURE / "expected.json").read_text(encoding="utf-8"))
        players = [PlayerState(seat=p["seat"], name=p["name"], stack=p["stack"]) for p in expected["players"]]
        (hand,) = replay_events(
            load_events(FIXTURE / "events.jsonl"), backend="pokerkit", players=players,
            sb=expected["blinds"]["sb"], bb=expected["blinds"]["bb"], session_id="store",
            out_dir=tmp_path, auto_new_hand=True, auto_winner=True, rfid_folds=True,
        )
        assert [[a.street, a.seat, a.action, a.amount] for a in hand.actions] == expected["actions"]
        assert (hand.winner_seat, hand.winner_source, hand.pot_total) == (
            expected["winner_seat"], expected["winner_source"], expected["pot_total"])
        assert hand.board == expected["board"] and hand.button_seat == expected["button_seat"]
        assert ({str(p["seat"]): sorted(p["hole_cards"]) for p in hand.players}
                == {s: sorted(c) for s, c in expected["hole_cards"].items()})   # 並び順は読んだ順
