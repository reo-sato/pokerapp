"""tests/test_store_2026_10_01.py

店舗の通しテスト 2026-10-01 のレビューで見つかったこと（オーナー「言い直しはそのように修正できるようにしてください」）:

- 額の言い直し: 42f7b964 ハンド 1 で「せーのっ!」（第 2 の耳が 千八百）→「1700」→「コール 1700点」が、1800 のレイズと
  1400 のコールになった（正しくは 1700 と 1300）。直前に記録したベット・レイズから 8 秒以内の違う額で、次の人の
  レイズにならない額は、その賭けの額の言い直し（あとから言った額で組み直す。要確認）。
- 宣言と復唱: 1709932e ハンド 3 でプレイヤーの「レイズにします。」とディーラーの「レイズ」「レイズ 1 千点」が 3 つの
  レイズになった。額の無いレイズのあとの同じ語は復唱、そのあとの額はその賭けの額。
"""
from __future__ import annotations

import pytest

from core.events import RFIDEvent

pytest.importorskip("pokerkit")

from integration.engine import ALLIN_RESTATE_SEC  # noqa: E402
from tests.test_rfid_folds import _Table  # noqa: E402
from tests.test_store_2026_09_27 import STORE_HOLES  # noqa: E402
from tests.test_store_2026_09_29 import _acts, _replayed_open  # noqa: E402


def _say(tmp_path, *said: tuple[str, float]) -> _Table:
    """ボタン 席6（最初の手番）/ SB 席4 / BB 席5。持ち点はどの席も 10000、ブラインド 100/200。"""
    tb = _Table(tmp_path)
    tb.deal(STORE_HOLES)
    for text, wait in said:
        tb.say(text)
        tb.tick(tb.now + wait)
    return tb


def _rebuilt(tb: _Table) -> list[tuple]:
    """ハンドの入力を始めから流し直した記録（札が戻った・ボタンを直したときの組み直しと同じ）。"""
    t = tb.t
    inputs = list(t._hand_inputs)        # noqa: SLF001
    t._hand_inputs = []                  # noqa: SLF001
    t._restore_checkpoint(t._hand_origin)   # noqa: SLF001
    t._last_wager = t._last_wager_point = None   # noqa: SLF001
    t._replay_inputs(inputs)             # noqa: SLF001
    return _acts(tb)


class TestAmountRestated:
    def test_a_lower_amount_restates_the_raise(self, tmp_path):
        """42f7b964 ハンド 1: 1800 のレイズのあとの「1700」は次の人のレイズにならない = 言い直し。"""
        tb = _say(tmp_path, ("600", 1.0), ("1800", 1.5), ("1700", 1.5), ("コール", 1.0))
        assert _acts(tb) == [(6, "raise", 600), (4, "raise", 1700), (5, "call", 1500)]
        record = tb.t._current_actions[1]    # noqa: SLF001
        assert record.needs_review and "amount_restated" in record.reason.split("+")
        assert record.raw_text == "1800 → 1700"
        assert any("言い直し" in n and "1700" in n for n in tb.notices)
        assert [a.amount for a in tb.actions if a.seat == 4][-1] == 1700    # 画面にも組み直した記録を流す
        (hand,) = _replayed_open(tb, tmp_path)
        assert [(a.seat, a.action, a.amount) for a in hand.actions][:3] == _acts(tb)

    def test_a_slightly_higher_amount_restates_it(self, tmp_path):
        tb = _say(tmp_path, ("600", 1.0), ("1800", 1.5), ("2000", 1.0))
        assert _acts(tb) == [(6, "raise", 600), (4, "raise", 2000)]

    def test_an_amount_the_next_player_can_raise_to_is_a_reraise(self, tmp_path):
        tb = _say(tmp_path, ("600", 1.0), ("1800", 1.5), ("4000", 1.0))
        assert _acts(tb) == [(6, "raise", 600), (4, "raise", 1800), (5, "raise", 4000)]
        assert not any("言い直し" in n for n in tb.notices)

    def test_an_amount_nobody_can_use_is_not_recorded(self, tmp_path):
        tb = _say(tmp_path, ("600", 1.0), ("1800", 1.5), ("700", 1.0))
        assert _acts(tb) == [(6, "raise", 600), (4, "raise", 1800)]
        assert not any("言い直し" in n for n in tb.notices)

    def test_restating_twice_keeps_the_last(self, tmp_path):
        tb = _say(tmp_path, ("600", 1.0), ("1800", 1.0), ("1700", 1.0), ("1600", 1.0))
        assert _acts(tb) == [(6, "raise", 600), (4, "raise", 1600)]
        record = tb.t._current_actions[1]    # noqa: SLF001
        assert record.raw_text == "1800 → 1700 → 1600"
        assert record.reason.split("+").count("amount_restated") == 1

    def test_a_restatement_long_after_is_not_applied(self, tmp_path):
        tb = _say(tmp_path, ("600", 1.0), ("1800", ALLIN_RESTATE_SEC + 1.0), ("1700", 1.0))
        assert _acts(tb) == [(6, "raise", 600), (4, "raise", 1800)]
        assert not any("言い直し" in n for n in tb.notices)

    def test_an_action_in_between_ends_it(self, tmp_path):
        tb = _say(tmp_path, ("600", 1.0), ("1800", 1.0), ("コール", 1.0), ("1700", 1.0))
        assert _acts(tb) == [(6, "raise", 600), (4, "raise", 1800), (5, "call", 1600)]

    def test_a_fold_word_in_between_ends_it(self, tmp_path):
        """7b897671 ハンド 3（席4 の札が席に残っていた読み）:「1600」→「フォールド」→「2千3百」の「2千3百」は次の人の額。
        「フォールド」は札が離れるか次のアクションまで席に付けない（保留）が、あいだのアクションに数える。"""
        tb = _say(tmp_path, ("600", 1.0), ("フォールド", 1.0), ("800", 1.0))
        assert _acts(tb) == [(6, "raise", 600), (4, "fold", 0), (5, "raise", 1000)]
        assert not any("言い直し" in n for n in tb.notices)

    def test_a_fold_word_in_between_ends_an_allin_restatement(self, tmp_path):
        tb = _say(tmp_path, ("オールイン", 1.0), ("フォールド", 1.0), ("オールイン", 1.0))
        assert [s for s, _, _ in _acts(tb)] == [6, 4, 5]

    def test_rebuilding_the_hand_gives_the_same_record(self, tmp_path):
        tb = _say(tmp_path, ("600", 1.0), ("1800", 1.5), ("1700", 1.5), ("コール", 1.0))
        before = _acts(tb)
        assert _rebuilt(tb) == before


class TestDeclarationAndEcho:
    def test_the_declaration_the_echo_and_the_amount_are_one_raise(self, tmp_path):
        """1709932e ハンド 3: プレイヤーの宣言 → ディーラーの復唱 → 額。"""
        tb = _say(tmp_path, ("レイズにします。", 1.0), ("レイズ", 0.8), ("レイズ1千点", 1.0), ("コール", 1.0))
        assert _acts(tb) == [(6, "raise", 1000), (4, "call", 900)]
        record = tb.t._current_actions[0]    # noqa: SLF001
        assert record.raw_text == "レイズにします。 → レイズ → レイズ1千点" and record.needs_review
        (hand,) = _replayed_open(tb, tmp_path)
        assert [(a.seat, a.action, a.amount) for a in hand.actions][:2] == _acts(tb)
        assert _rebuilt(tb) == [(6, "raise", 1000), (4, "call", 900)]

    def test_a_raise_word_then_the_amount(self, tmp_path):
        tb = _say(tmp_path, ("レイズ", 1.0), ("レイズ 1000", 1.0))
        assert _acts(tb) == [(6, "raise", 1000)]

    def test_the_echo_alone_is_one_raise(self, tmp_path):
        tb = _say(tmp_path, ("レイズにします。", 1.0), ("レイズ", 1.0))
        assert _acts(tb) == [(6, "raise", 400)]
        assert "restated" in tb.t._current_actions[0].reason.split("+")   # noqa: SLF001

    def test_a_raise_after_a_bet_word_is_the_next_player(self, tmp_path):
        """額の無い「ベット」のあとの「レイズ N」は次の人のレイズ（前の人の額は聞こえなかった）。"""
        tb = _say(tmp_path, ("ベット", 1.0), ("レイズ 1000", 1.0))
        assert [s for s, _, _ in _acts(tb)] == [6, 4]
        assert _acts(tb)[1] == (4, "raise", 1000)

    def test_a_raise_word_after_a_raise_with_an_amount_is_the_next_player(self, tmp_path):
        tb = _say(tmp_path, ("レイズ 600", 1.0), ("レイズ", 1.0))
        assert [s for s, _, _ in _acts(tb)] == [6, 4]


def _deal_waiting(tb: _Table, speech_since: float) -> None:
    """手札を配る（認識待ちの発話の話し始めが `speech_since`）。始まるかどうかは確かめない。"""
    tb.t._speech_backlog = lambda: 1       # noqa: SLF001 — 認識待ちの発話がずっとある（話し声が続く）
    tb.speech_since = speech_since
    for seat, cards in STORE_HOLES.items():
        tb.put(seat, cards)
        for card in cards:
            ev = RFIDEvent(tag_id=card, card=card, reader_id=f"r{seat}", role="seat", seat=seat,
                           timestamp=tb.now, raw_tag_id=card)
            tb.recorder.record(ev)
            tb.t._process_rfid_event(ev)     # noqa: SLF001
    tb.tick(tb.now + 2.0)


class TestHandStartWhileTalking:
    """店舗 b0a27270 ハンド 2: 配ってから 20 秒ハンドが始まらなかった（話し声が続いて「認識待ちの発話」が 0 に
    ならなかった）。そのあいだに降りた席6 に、始めるきっかけになった「八百」が付いた。"""

    def test_speech_started_after_the_deal_does_not_hold_the_hand(self, tmp_path):
        tb = _Table(tmp_path)
        tb.now = 10.0
        _deal_waiting(tb, speech_since=10.5)          # 配ったあとに話し始めた発話だけが認識待ち
        assert tb.t._hand_open                         # noqa: SLF001

    def test_speech_started_before_the_deal_still_holds_it(self, tmp_path):
        tb = _Table(tmp_path)
        tb.now = 10.0
        _deal_waiting(tb, speech_since=9.0)           # 配る前に話し始めた発話（前のハンドのもの）を待つ
        assert not tb.t._hand_open                     # noqa: SLF001
        tb.speech_since = None
        tb.t._start_dealt_hand_if_ready()              # noqa: SLF001 — run() の発話が無いときの確認
        assert tb.t._hand_open                         # noqa: SLF001

    def test_a_fold_while_waiting_goes_before_the_first_action(self, tmp_path):
        tb = _Table(tmp_path)
        tb.now = 10.0
        _deal_waiting(tb, speech_since=9.0)
        tb.lift(6)                                     # 席6（ボタン = 最初の手番）が降りた
        tb.tick(tb.now + 5.0)
        assert not tb.t._hand_open                     # noqa: SLF001
        tb.say("800", spoken_at=tb.now - 1.0)          # 配ったあとの発話が届いて始まる
        assert _acts(tb) == [(6, "fold", 0), (4, "raise", 800)]
