"""tests/test_store_2026_09_29.py

店舗の通しテスト 2026-09-29（4 セッション・真のアクション 11 ハンド）のレビューで見つかったこと:

- 再生が記録と食い違った（d0f055fb の 5 ハンド）。live は配った直後の「フォールド」でハンドを始め、その処理の
  中で札の離脱の信号を発話と同じ時刻で記録した。再生は同じ時刻の信号を発話より先に流すので、離脱がハンドの開始
  より前に届いて捨てられ、フォールドも保留のまま消えていた → 配布を検出してからハンドを始めるまでに届いた
  そのハンドの信号は、始めてから反映する。
- ディーラーはオールインを言い直す:「オーリー」→「オールイン、コール」/「オールインフォールド」、「オールイン」→
  「オールイン、コール、ショーダウン」。「オーリー」をオールインと読むようにした（2026-09-29）ので、言い直しの
  「オールイン」が次の人のオールインになっていた → 直前の記録のオールインから 8 秒以内の別の発話の「オールイン」は
  同じオールインの言い直し。
- 「2千500」を 2000 と読んでいた（単位のあとに算用数字が続く額）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from audio.recognizer import parse_actions, parse_amount_ex
from core.events import AudioEvent, RFIDEvent
from core.game_state import PlayerState

pytest.importorskip("pokerkit")

from integration.engine import ALLIN_RESTATE_SEC  # noqa: E402
from integration.replay import _in_replay_order, replay_events  # noqa: E402
from tools.eval_store import replay_session  # noqa: E402
from tests.test_rfid_folds import _Table  # noqa: E402
from tests.test_store_2026_09_27 import STORE_HOLES  # noqa: E402


def _acts(tb: _Table) -> list[tuple]:
    return [(a.seat, a.action, a.amount) for a in tb.t._current_actions]   # noqa: SLF001


FIXTURE = Path(__file__).parent / "fixtures" / "store" / "2026-09-29-d0f055fb"


class TestReplayOfTheFirstHand:
    def test_the_fold_right_after_the_deal_is_replayed(self):
        """live は配った直後の「フォールド」でハンドを始め、その処理の中で席6 の札の離脱を同じ時刻で記録した。
        再生では同じ時刻の信号が発話より先に流れる（ハンドの開始より前）ので、始めてから反映する。"""
        expected = json.loads((FIXTURE / "expected.json").read_text(encoding="utf-8"))
        setup = expected["setup"]
        hands = replay_session(FIXTURE / "events.jsonl", setup,
                               {k: bool(setup.get(k)) for k in ("auto_new_hand", "auto_winner", "rfid_folds")},
                               expected["session_id"])
        first = hands[0]
        assert [(a["street"], a["seat"], a["action"], a["amount"]) for a in first["actions"]][:3] == [
            ("preflop", 6, "fold", 0), ("preflop", 4, "raise", 600), ("preflop", 5, "call", 400)]
        assert first["board"] == ["4s", "Ad", "7h", "6h", "9s"] and first["winner_seat"] == 4


def _audio(action: str, ts: float) -> AudioEvent:
    return AudioEvent(action=action, amount=0, timestamp=ts, raw_text=action, utterance_start_ts=ts - 3.0)


def _signal(kind: str, ts: float, seat: int | None = None) -> RFIDEvent:
    return RFIDEvent(tag_id="", card="", reader_id="", role="seat", seat=seat, timestamp=ts, raw_tag_id="", kind=kind)


def _card(ts: float) -> RFIDEvent:
    return RFIDEvent(tag_id="t", card="Ah", reader_id="r", role="board", seat=None, timestamp=ts, raw_tag_id="t")


class TestReplayOrder:
    def test_the_recorded_order_is_kept_even_when_card_times_go_back(self):
        """ボードの札の時刻は最初に見えた時刻（2 秒載り続けてから確定）= そのあいだに処理した信号より前。"""
        leave, street, turn = _signal("leave", 49.81, seat=5), _signal("street", 49.82), _card(49.26)
        assert _in_replay_order([leave, street, turn]) == [leave, street, turn]

    def test_a_signal_made_while_handling_an_utterance_goes_before_it(self):
        """発話と同じ時刻の信号は、その発話の処理の中で（アクションを入れる前に）反映した。"""
        amount, leave, call = _audio("bet", 10.0), _signal("leave", 10.0, seat=4), _audio("call", 12.0)
        assert _in_replay_order([amount, leave, call]) == [leave, amount, call]

    def test_only_the_action_that_made_it(self):
        """「オールインフォールド」: 席4 の離脱は 2 つ目（フォールド）の処理の中で出した。"""
        allin, fold, leave = _audio("allin", 20.0), _audio("fold", 20.0), _signal("leave", 20.0, seat=4)
        assert _in_replay_order([allin, fold, leave]) == [allin, leave, fold]

    def test_a_later_signal_stays_after(self):
        fold, start, leave = _audio("fold", 13.61), _signal("hand_start", 13.63), _signal("leave", 13.61, seat=6)
        assert _in_replay_order([fold, start, leave]) == [leave, fold, start]

    def test_a_record_without_signals_is_in_time_order(self):
        speech, card = _audio("call", 5.0), _card(5.0)
        assert _in_replay_order([speech, card]) == [card, speech]


class TestAllinRestated:
    """ボタン 席6（最初の手番）/ SB 席4 / BB 席5。持ち点はどの席も 10000。"""

    def _raise_then(self, tmp_path, *said: tuple[str, float]) -> _Table:
        tb = _Table(tmp_path)
        tb.deal(STORE_HOLES)
        tb.say("600")                                  # 席6 レイズ 600
        tb.tick(tb.now + 1.0)
        for text, wait in said:
            tb.say(text)
            tb.tick(tb.now + wait)
        return tb

    def test_the_dealer_restating_the_allin_is_one_allin(self, tmp_path):
        tb = self._raise_then(tmp_path, ("オーリー", 3.0), ("オールイン、コール", 1.0))
        assert _acts(tb) == [(6, "raise", 600), (4, "allin", 10000), (5, "call", 9800)]
        assert any("言い直し" in n for n in tb.notices)
        assert "allin_restated" in tb.t._current_actions[1].reason   # noqa: SLF001
        (hand,) = _replayed_open(tb, tmp_path)
        assert [(a.seat, a.action, a.amount) for a in hand.actions][:3] == _acts(tb)

    def test_a_summary_after_the_allin_restates_it(self, tmp_path):
        tb = self._raise_then(tmp_path, ("オールイン", 6.5), ("オールインフォールド", 1.0))
        assert _acts(tb)[:2] == [(6, "raise", 600), (4, "allin", 10000)]
        assert (5, "allin", 10000) not in _acts(tb)

    def test_an_allin_followed_by_a_call_is_kept(self, tmp_path):
        tb = self._raise_then(tmp_path, ("オーリー", 2.0), ("コール", 1.0))
        assert _acts(tb) == [(6, "raise", 600), (4, "allin", 10000), (5, "call", 9800)]
        assert not any("言い直し" in n for n in tb.notices)

    def test_two_allins_in_one_utterance_are_a_restatement(self, tmp_path):
        """間を置かずに 2 人が続けてオールインすることはまず無い（オーナー 2026-09-30。前は 2 人とみていた）。"""
        tb = self._raise_then(tmp_path, ("オールイン、オールインです", 1.0))
        assert _acts(tb) == [(6, "raise", 600), (4, "allin", 10000)]
        assert any("言い直し" in n for n in tb.notices)
        (hand,) = _replayed_open(tb, tmp_path)
        assert [(a.seat, a.action, a.amount) for a in hand.actions][:2] == _acts(tb)

    def test_an_allin_long_after_is_another_player(self, tmp_path):
        tb = self._raise_then(tmp_path, ("オールイン", ALLIN_RESTATE_SEC + 2.0), ("オールイン", 1.0))
        assert [s for s, _, _ in _acts(tb)] == [6, 4, 5]

    def test_an_action_in_between_ends_the_restatement(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal(STORE_HOLES)
        tb.say("オールイン")                             # 席6 オールイン
        tb.tick(tb.now + 1.0)
        tb.say("コール")                                # 席4 コール
        tb.tick(tb.now + 1.0)
        tb.say("オールイン")                             # 席5（BB）もオールイン = 言い直しではない
        tb.tick(tb.now + 1.0)
        assert [s for s, _, _ in _acts(tb)] == [6, 4, 5]


class TestWagerRestated:
    """同じ額のベット・レイズの言い直し（読み上げ集 2026-09-30「ベット、2000」。オーナー「言い直しはする」）。
    次の人は同じ額をベット・レイズできない（同じ額ならコール）ので、次の人のレイズにしていたのを止める。
    ボタン 席6（最初の手番）/ SB 席4 / BB 席5。"""

    def _say(self, tmp_path, *said: tuple[str, float]) -> _Table:
        tb = _Table(tmp_path)
        tb.deal(STORE_HOLES)
        for text, wait in said:
            tb.say(text)
            tb.tick(tb.now + wait)
        return tb

    def test_a_restated_raise_is_one_raise(self, tmp_path):
        tb = self._say(tmp_path, ("レイズ 600", 1.5), ("レイズ 600です", 2.0), ("コール", 1.0))
        assert _acts(tb) == [(6, "raise", 600), (4, "call", 500)]
        assert "restated" in tb.t._current_actions[0].reason.split("+")    # noqa: SLF001
        assert any("言い直し" in n for n in tb.notices)
        (hand,) = _replayed_open(tb, tmp_path)
        assert [(a.seat, a.action, a.amount) for a in hand.actions][:2] == _acts(tb)

    def test_a_restated_raise_in_one_utterance(self, tmp_path):
        tb = self._say(tmp_path, ("レイズ 600、レイズ 600", 1.0))
        assert _acts(tb) == [(6, "raise", 600)]

    def test_a_different_amount_is_a_reraise(self, tmp_path):
        tb = self._say(tmp_path, ("レイズ 600", 1.5), ("レイズ 1800", 1.0))
        assert _acts(tb) == [(6, "raise", 600), (4, "raise", 1800)]
        assert not any("言い直し" in n for n in tb.notices)

    def test_an_action_in_between_ends_the_restatement(self, tmp_path):
        tb = self._say(tmp_path, ("レイズ 600", 1.5), ("コール", 1.5), ("レイズ 600", 1.0))
        assert [s for s, _, _ in _acts(tb)] == [6, 4, 5]
        assert not any("言い直し" in n for n in tb.notices)

    def test_a_repeat_long_after_is_not_a_restatement(self, tmp_path):
        tb = self._say(tmp_path, ("レイズ 600", ALLIN_RESTATE_SEC + 2.0), ("レイズ 600", 1.0))
        assert [s for s, _, _ in _acts(tb)] == [6, 4]


def _replayed_open(tb: _Table, tmp_path) -> list:
    """確定していないハンドも閉じて再生する（言い直しの判断が記録から同じになるか）。"""
    return replay_events(
        tb.recorder.events, backend="pokerkit",
        players=[PlayerState(seat=s, name=f"P{s}", stack=10000) for s in (4, 5, 6)],
        sb=100, bb=200, session_id="replay", out_dir=tmp_path / "replay",
        auto_new_hand=True, auto_winner=True, rfid_folds=True, close_open_hand=True,
    )


class TestAmounts:
    @pytest.mark.parametrize("text, value, ambiguous", [
        ("2千500", 2500, False), ("2千500点", 2500, False), ("3千200円", 3200, False),
        ("1万2000", 12000, False), ("1万500", 10500, False), ("千500", 1500, False), ("二千500", 2500, False),
        ("2千5", 2500, True),
        # 変わらないもの
        ("2千5百", 2500, False), ("1万2千", 12000, False), ("4万2", 42000, True), ("2千", 2000, False),
    ])
    def test_digits_after_a_unit_are_added(self, text, value, ambiguous):
        parsed = parse_amount_ex(text)
        assert (parsed.value, parsed.ambiguous) == (value, ambiguous)

    def test_in_an_utterance(self):
        assert [(e.action, e.amount) for e in parse_actions("2千500")] == [("bet", 2500)]
        assert [(e.action, e.amount) for e in parse_actions("レイズ2千500")] == [("raise", 2500)]
        assert [(e.action, e.amount) for e in parse_actions("千500")] == [("bet", 1500)]
        assert parse_actions("2千、500") == []                  # 区切って言った 2 つの数は額にしない


def _parsed(text: str) -> list[tuple]:
    return [(e.action, e.amount, tuple(e.parse_flags)) for e in parse_actions(text, confidence=0.5)]


class TestActionAfterChatter:
    """メモ（7b897671 ハンド 2）「最初の1300が長い雑談に巻き込まれてる」: 雑談の文に続けて言った額は、発話全体では
    会話とみて読まなかった。発話全体で読めないときだけ、最後の文から前へアクションとして読める文を読み直す
    （要確認 = sentence_after_chatter。雑談に挟まれた文は読まない）。"""

    def test_the_amount_after_the_chatter_is_read(self):
        text = "それが撮りづらくなります。 そんな機能入れてないんですよ、まだ。 1300"
        assert _parsed(text) == [("bet", 1300, ("amount_only", "sentence_after_chatter"))]
        (event,) = parse_actions(text)
        assert event.raw_text == "1300"

    def test_an_action_word_after_the_chatter_is_read(self):
        text = "そうなんですよ、エアコンが効きすぎてしまって。 コール"
        assert _parsed(text) == [("call", 0, ("sentence_after_chatter",))]

    def test_filler_after_the_action_is_skipped(self):
        text = "そうなんですよ、エアコンが効きすぎてしまって。 コール。 はい。"
        assert _parsed(text) == [("call", 0, ("sentence_after_chatter",))]

    @pytest.mark.parametrize("text", [
        "それが撮りづらくなります。 そんな機能入れてないんですよ、まだ。",
        # 会話の中のアクションの語は、文ごとに読み直しても読まない
        "結構コールとか迷路に発音してますよね。 そうなんですよ、本当に困ってしまって。",
        # 雑談に挟まれた文は読まない（d0f055fb: 前は 2 行目をレイズ 4 と読み、ハンド 7 の一致が 2 → 1 行に）
        "ラッシャーに4とか書いちゃってたよ。4番レイズとかね。最悪ね。",
    ])
    def test_chatter_alone_is_not_read(self, text):
        assert _parsed(text) == []

    def test_an_utterance_read_as_a_whole_is_not_split(self):
        assert _parsed("チェック。 ベット600。") == [("check", 0, ()), ("bet", 600, ())]

    def test_the_flag_asks_for_review(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal(STORE_HOLES)
        tb.say("それが撮りづらくなります。 そんな機能入れてないんですよ、まだ。 1300")
        (record,) = tb.t._current_actions                   # noqa: SLF001
        assert (record.action, record.amount, record.needs_review) == ("raise", 1300, True)
        assert "sentence_after_chatter" in (record.reason or "")
