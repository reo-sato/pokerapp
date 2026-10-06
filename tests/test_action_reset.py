"""tests/test_action_reset.py

口頭でのアクションのやり直し（オーナー 2026-10-06「口頭の修正プロンプトを利用する」）。店舗 e82f5005 ハンド 11 では
ボタンの話し合いのあと「もう一度アクションをやり直して」とアクションを最初から言い直したが、記録を戻す手段が無く
1 回目と 2 回目のアクションが混ざった。

- ディーラーが「アクションリセット」（「アクションやり直し」）と言うと、そのハンドのアクションを捨てて、始め（ブラインドの
  あと）からやり直す。手札・ボード・ボタン・持ち点はそのまま。続けて最初のアクションを言ってよい。
- 発話の頭がこの言葉のときだけ。会話の「リセットできないんで」「一回それリセットして」は合図にしない（店舗の書き起こし
  3106 発話で 0 回）。
- 合図より前に札が離れた席（降りた人）は、言い直しでその人の手番が来たらフォールドにする（間の人を補わない）。
- 記録した合図で replay も同じになる。合図の聞き違いで正しい記録を消さないよう、そのハンドは要確認。
"""
from __future__ import annotations


import pytest

from audio.recognizer import RESET_ACTIONS, parse_actions
from core.game_state import PlayerState

pytest.importorskip("pokerkit")

from integration.replay import replay_events  # noqa: E402
from tests.test_rfid_folds import HOLES, _Table  # noqa: E402


def _actions(text: str) -> list[tuple]:
    return [(e.action, e.amount) for e in parse_actions(text)]


class TestReadingTheCue:
    @pytest.mark.parametrize("text", [
        "アクションリセット", "アクションリセット。", "はい、アクションリセットします", "アクション、リセット",
        "アクションをやり直します", "アクションやり直し", "アクションやりなおし", "では、アクションやり直しましょう",
    ])
    def test_the_cue(self, text):
        assert _actions(text) == [(RESET_ACTIONS, 0)]

    @pytest.mark.parametrize("text", [
        "リセットできないんで。",                             # 店舗 e82f5005 ハンド 11 の会話
        "いいよ、じゃあ一回それリセットして。",
        "もう一度アクションをやり直してもらっていいですか? いいよ。",
        "いや、分かんない。一回リセットしようかなと思って。",   # 9d1d8536
        "アクションです。", "アクションどうぞ", "リセット", "アクションリセットできない",
        "アクションをやり直してください", "アクションリセット?",
    ])
    def test_conversation_is_not_the_cue(self, text):
        assert RESET_ACTIONS not in [a for a, _ in _actions(text)]

    def test_the_first_action_can_follow_the_cue(self):
        assert _actions("アクションリセット、700") == [(RESET_ACTIONS, 0), ("bet", 700)]
        assert _actions("アクションリセット。レイズ1800、コール") == [
            (RESET_ACTIONS, 0), ("raise", 1800), ("call", 0)]


class TestResetInTheEngine:
    def test_the_hand_restarts_with_the_same_cards_and_button(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal()
        button = tb.gs.button_seat
        tb.say("レイズ 600")                       # 席6（ボタン）
        tb.say("コール")                           # 席4
        assert tb.played() == [("preflop", 6, "raise", 600), ("preflop", 4, "call", 500)]
        tb.say("アクションリセット")
        assert tb.played() == []
        assert tb.gs.button_seat == button
        assert tb.gs.legal_context().actor_seat == 6     # 最初の手番に戻った
        assert tb.t._hole_cards == HOLES                  # noqa: SLF001
        assert any("最初からやり直します" in n and "2 件" in n for n in tb.notices)
        tb.say("レイズ 800")
        tb.say("コール")
        tb.say("コール")
        assert tb.played() == [("preflop", 6, "raise", 800), ("preflop", 4, "call", 700),
                               ("preflop", 5, "call", 600)]
        assert tb.t._hand_needs_review                    # noqa: SLF001

    def test_the_cue_and_the_first_action_in_one_utterance(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal()
        tb.say("レイズ 600")
        tb.say("アクションリセット、レイズ 900")
        assert tb.played() == [("preflop", 6, "raise", 900)]

    def test_a_seat_that_folded_before_the_cue_folds_on_its_turn(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal()
        tb.say("レイズ 600")                       # 席6
        tb.lift(4)                                  # 席4 が降りた（札が離れた）
        tb.tick(tb.now + 3.5)
        assert tb.played() == [("preflop", 6, "raise", 600), ("preflop", 4, "fold", 0)]
        tb.say("アクションリセット")
        # 席4 の手番はまだ来ていない: フォールドにしない・席6 のアクションを補わない
        assert tb.played() == []
        tb.say("レイズ 600")                       # 言い直し: 席6
        assert tb.played() == [("preflop", 6, "raise", 600), ("preflop", 4, "fold", 0)]
        tb.say("コール")                           # 席5
        assert tb.played()[-1] == ("preflop", 5, "call", 400)

    def test_the_hand_ends_with_only_the_restated_actions(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal()
        tb.say("コール")                           # 1 回目（あとでやり直す）
        tb.say("アクションリセット")
        tb.say("レイズ 600")
        tb.lift(4)
        tb.tick(tb.now + 3.5)                       # 席4 フォールド
        tb.lift(5)
        tb.tick(tb.now + 3.5)                       # 席5 フォールド → 席6 の勝ち
        tb.tick(tb.now + 12.0)
        assert tb.hands, "ハンドが確定していない"
        hand = tb.hands[-1]
        assert [(a.seat, a.action) for a in hand.actions] == [(6, "raise"), (4, "fold"), (5, "fold")]
        assert hand.winner_seat == 6 and hand.review_required

    def test_outside_a_hand_it_does_nothing(self, tmp_path):
        tb = _Table(tmp_path)
        tb.say("アクションリセット")
        assert tb.played() == []
        assert any("やり直す進行中のハンドがありません" in n for n in tb.notices)

    def test_the_replay_gives_the_same_hand(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal()
        tb.say("レイズ 600")
        tb.lift(4)
        tb.tick(tb.now + 3.5)
        tb.say("アクションリセット")
        tb.say("レイズ 600")                       # 言い直し: 席6 → 席4 は降りている
        tb.lift(5)
        tb.tick(tb.now + 3.5)                       # 席5 フォールド → 席6 の勝ち
        tb.tick(tb.now + 12.0)
        assert tb.hands, "ハンドが確定していない"
        live = [(a.street, a.seat, a.action, a.amount) for h in tb.hands for a in h.actions]
        assert live == [("preflop", 6, "raise", 600), ("preflop", 4, "fold", 0), ("preflop", 5, "fold", 0)]
        replayed = replay_events(
            tb.recorder.events, backend="pokerkit",
            players=[PlayerState(seat=s, name=f"P{s}", stack=10000) for s in (4, 5, 6)],
            sb=100, bb=200, session_id="replay", out_dir=tmp_path / "replay",
            auto_new_hand=True, auto_winner=True, rfid_folds=True,
        )
        assert [(a.street, a.seat, a.action, a.amount) for h in replayed for a in h.actions] == live

    def test_a_card_returning_after_the_cue_keeps_the_reset(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal()
        tb.say("レイズ 600")
        tb.say("コール")
        tb.say("アクションリセット")
        tb.say("レイズ 800")                       # 席6
        tb.lift(4)
        tb.tick(tb.now + 3.5)                       # 席4 フォールド
        assert tb.played() == [("preflop", 6, "raise", 800), ("preflop", 4, "fold", 0)]
        tb.put(4, HOLES[4])                         # 札が戻った = 降りていない（組み直し）
        tb.tick(tb.now + 0.5)
        assert tb.played() == [("preflop", 6, "raise", 800)]   # やり直す前のアクションは戻らない

    def test_fixing_the_button_after_the_cue_keeps_only_the_restated_actions(self, tmp_path):
        from core.events import AudioEvent

        tb = _Table(tmp_path)
        tb.deal()
        tb.say("レイズ 600")
        tb.say("アクションリセット")
        tb.say("レイズ 800")
        tb.t._handle_audio_event(AudioEvent(       # noqa: SLF001 — `button 4`（ボタンを手で直す）
            action="set_button", amount=0, timestamp=tb.now, raw_text="シート4 ボタン", seat=4))
        assert tb.gs.button_seat == 4
        assert [(a, amt) for _s, _seat, a, amt in tb.played()] == [("raise", 800)]
