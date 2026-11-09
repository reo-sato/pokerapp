"""tests/test_allin_count.py

オールインの額を数える（オーナー 2026-11-09）: ディーラーは「カウント」「カウントします」「カウントいたします」と言ってから
数え、「オールインは〜点」で終える。頼んだ人が撤回して、数え終わらないうちにアクションを言うこともある。

- 合図の読み: 申し出の「お声掛けでカウントいたします」（11/05 に 4 回）・語の続き（「カウントしてくれてる」）・
  「カウント大丈夫です」は合図にしない。
- engine: 数える声（いまのベット以下の数字だけの発話）は記録しない・「ここまでの言い直し」とも見ない（続く「コール」を
  消さない）。「オールインは〜点」は時間がたっていても数えたオールインの言い直し。ほかのアクションの語は撤回して言った
  アクション（いまのベットより大きい数字はレイズ）。
- 推定器: 数える声は使われないのがふつう（`p_count`）。
"""
from __future__ import annotations

import pytest

from audio.recognizer import COUNT, parse_actions
from core.events import AudioEvent
from core.game_state import PlayerState

pytest.importorskip("pokerkit")

from integration.estimator import PARAMS  # noqa: E402
from integration.replay import replay_events  # noqa: E402
from tests.test_rfid_folds import _Table  # noqa: E402


def _actions(text: str) -> list[tuple]:
    return [(e.action, e.amount) for e in parse_actions(text)]


class TestReadingTheCue:
    @pytest.mark.parametrize("text", [
        "カウント", "カウントします。", "カウントいたします", "カウントお願いします", "かうんとします", "カウントね",
    ])
    def test_the_cue(self, text):
        assert _actions(text) == [(COUNT, 0)]

    @pytest.mark.parametrize("text", [
        "オールイン入ってます。お声掛けでカウントいたします。",      # 11/05 の申し出
        "お声がけでカウントいたします",
        "声がけで、カウントいたします",
        "一応そのチップをカウントしてくれてるんですけど。",           # 9/29 の雑談
        "アカウント",
    ])
    def test_not_the_cue(self, text):
        assert all(a != COUNT for a, _ in _actions(text))

    def test_the_words_around_the_cue_are_read(self):
        assert _actions("カウントします。1万、2万。") == [(COUNT, 0), ("bet", 10000), ("bet", 20000)]
        assert _actions("コール。カウントします") == [("call", 0), (COUNT, 0)]
        assert _actions("カウント大丈夫です。コール") == [("call", 0)]     # 数えないと言った = 合図ではない


def _allin_and_count(tb: _Table) -> None:
    tb.deal()
    tb.say("オールイン")                           # 席6（ボタン）10000
    tb.tick(tb.now + 3)
    tb.say("カウントします")


class TestLive:
    def test_the_amount_at_the_end_restates_the_counted_allin(self, tmp_path):
        tb = _Table(tmp_path)
        _allin_and_count(tb)
        for amount in ("2千", "5千", "8千"):          # 数える声
            tb.tick(tb.now + 8)
            tb.say(amount)
        tb.tick(tb.now + 15)                           # 合図から 40 秒（言い直しの 8 秒の窓を過ぎた）
        tb.say("オールインは1万点です")
        assert tb.played() == [("preflop", 6, "allin", 10000)]
        assert "allin_restated" in tb.t._current_actions[0].reason   # noqa: SLF001
        tb.say("コール")
        assert tb.played()[-1] == ("preflop", 4, "call", 9900)

    def test_without_the_cue_the_late_amount_is_the_next_players_allin(self, tmp_path):
        """合図が無ければ今までどおり（8 秒を過ぎた「オールインは〜点」は次の人のオールイン）。"""
        tb = _Table(tmp_path)
        tb.deal()
        tb.say("オールイン")
        tb.tick(tb.now + 40)
        tb.say("オールインは1万点です")
        assert tb.played()[-1][1:3] == (4, "allin")

    def test_a_counted_amount_does_not_swallow_the_next_call(self, tmp_path):
        """オールイン → コール → 次の人が数えてもらう: 数え終わりの「1万」のあとの「コール」は次の人のコール
        （ここまでの言い直しの「コール」にしない）。"""
        tb = _Table(tmp_path)
        tb.deal()
        tb.say("オールイン")                       # 席6
        tb.say("コール")                           # 席4
        tb.tick(tb.now + 5)
        tb.say("カウントします")
        tb.tick(tb.now + 10)
        tb.say("1万")                              # いまのベットと同じ額 = 数え終わり
        tb.tick(tb.now + 1)
        tb.say("コール")                           # 席5
        assert tb.played() == [("preflop", 6, "allin", 10000), ("preflop", 4, "call", 9900),
                               ("preflop", 5, "call", 9800)]

    def test_withdrawn_count(self, tmp_path):
        """数え終わらないうちに撤回してコール: コールはそのまま記録し、数えるのをやめる。"""
        tb = _Table(tmp_path)
        _allin_and_count(tb)
        tb.tick(tb.now + 5)
        tb.say("3千")
        tb.tick(tb.now + 2)
        tb.say("コール")
        assert tb.played() == [("preflop", 6, "allin", 10000), ("preflop", 4, "call", 9900)]
        assert tb.t._counting is None              # noqa: SLF001

    def test_a_bigger_number_is_a_raise(self, tmp_path):
        """短いオールイン（3000）を数えているあいだの、いまのベットより大きい数字は撤回して言ったレイズ。"""
        tb = _Table(tmp_path)
        tb.gs.set_stacks({4: 10000, 5: 10000, 6: 3000})
        _allin_and_count(tb)
        tb.tick(tb.now + 5)
        tb.say("2千")                              # 数える声
        assert tb.played() == [("preflop", 6, "allin", 3000)]
        tb.tick(tb.now + 3)
        tb.say("6千")                              # 席4 のレイズ
        assert tb.played()[-1] == ("preflop", 4, "raise", 6000)
        assert tb.t._counting is None              # noqa: SLF001

    def test_no_allin_no_count(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal()
        tb.say("カウントします")
        tb.say("600")
        assert tb.played() == [("preflop", 6, "raise", 600)]
        assert any("オールインが無いので" in n for n in tb.notices)

    def test_the_count_ends_with_the_street(self, tmp_path):
        tb = _Table(tmp_path)
        _allin_and_count(tb)
        tb.say("コール")
        tb.say("コール")                           # ラウンドが閉じる（次のストリート）
        assert tb.t._counting is None              # noqa: SLF001

    def test_replay_is_the_same(self, tmp_path):
        tb = _Table(tmp_path)
        _allin_and_count(tb)
        tb.tick(tb.now + 20)
        tb.say("1万")
        tb.tick(tb.now + 20)
        tb.say("オールインは1万点です")
        tb.say("コール")
        end = AudioEvent(action="session_end", amount=0, timestamp=tb.now, raw_text="q")
        tb.recorder.record(end)
        tb.t._handle_audio_event(end)              # noqa: SLF001
        live = [(a.street, a.seat, a.action, a.amount) for h in tb.hands for a in h.actions]
        assert live[:2] == [("preflop", 6, "allin", 10000), ("preflop", 4, "call", 9900)]
        replayed = replay_events(
            tb.recorder.events, backend="pokerkit",
            players=[PlayerState(seat=s, name=f"P{s}", stack=10000) for s in (4, 5, 6)],
            sb=100, bb=200, session_id="replay", out_dir=tmp_path / "replay",
            auto_new_hand=True, auto_winner=True, rfid_folds=True,
        )
        assert [(a.street, a.seat, a.action, a.amount) for h in replayed for a in h.actions] == live


def test_the_estimator_lets_counting_go_unused():
    assert PARAMS["p_count"] >= PARAMS["p_restate"]
