"""tests/test_street_change_words.py

ストリートが変わるときの言葉（店舗 2026-10-06 のログとオーナーの説明）:

- 「チェックアラウンド」はストリートが変わるときに言う。前のストリートの一人ひとりのチェックは言わないこともある。
  いまのストリートでまだ誰も動いていず、その札の直後（`CHECK_AROUND_STREET_CHANGE_SEC` 秒内）に話し始めた
  チェックアラウンドは、閉じた前のストリートのこと = 記録しない（札で補ったチェックがあれば、その裏付けにする）。
- 「チェック、アンド」「チェック、ハンド」はチェックアラウンドの聞き違い（オーナー / 台本の読み上げ 2026-10-01）。
- 前のラウンドを閉じた発話の中のコール（払う額が無い）は言い直し（「コールします、コール」）。
- ベットの途中（チェックできる手番の人がいる）の「チョップ」はチェックの聞き違い（要確認）。
- 前のラウンドを閉じた発話の中の「チェック」（店舗 2026-10-06 e82f5005 ハンド 9:「コール2千、ロック、チェック」）と、
  いまのストリートの札が見えるより前に話し始めた「チェック」は、次のストリートの最初の人のチェックではない。札が
  読めていなければ決めない（読めないボードで本当のチェックを捨てない）。
"""
from __future__ import annotations

import pytest

from audio.recognizer import parse_actions

pytest.importorskip("pokerkit")

from integration.engine import CHECK_AROUND_STREET_CHANGE_SEC  # noqa: E402
from tests.test_rfid_folds import _Table  # noqa: E402
from tests.test_silent_runs import _board, _to_flop  # noqa: E402


def _street(tb: _Table, street: str) -> list[tuple]:
    return [(a.seat, a.action, a.amount) for a in tb.t._current_actions if a.street == street]   # noqa: SLF001


def _checks_through_flop(tb: _Table) -> None:
    _to_flop(tb)                                        # フロップの手番: 席4 → 席5 → 席6
    for _ in range(3):
        tb.say("チェック")
        tb.tick(tb.now + 1.0)
    assert _street(tb, "flop") == [(4, "check", 0), (5, "check", 0), (6, "check", 0)]


class TestReading:
    @pytest.mark.parametrize("text", ["チェック、アンド", "チェック アンド", "チェック、ハンド"])
    def test_check_and_is_check_around(self, text):
        (event,) = parse_actions(text, confidence=0.9)
        assert event.action == "check" and "check_around" in event.parse_flags

    def test_checks_just_before_a_check_around_are_marked(self):
        events = parse_actions("チェック、チェックラウンド、ラストカード", confidence=0.9)
        assert [(e.action, e.parse_flags) for e in events] == [
            ("check", ("before_check_around",)), ("check", ("check_around",))]
        assert [e.parse_flags for e in parse_actions("チェック、チェック", confidence=0.9)] == [(), ()]

    def test_check_then_check_and_all(self):
        first, second = parse_actions("チェック、チェック、アンド、オール。", confidence=0.9)
        assert "check_around" not in first.parse_flags and "check_around" in second.parse_flags

    @pytest.mark.parametrize("text", ["チェック、ラスト カード", "チェック、ターン"])
    def test_street_words_after_check_are_not_around(self, text):
        (event,) = parse_actions(text, confidence=0.9)
        assert "check_around" not in event.parse_flags

    def test_hand_end_after_check_is_still_hand_end(self):
        assert [e.action for e in parse_actions("チェック、ハンド終了", confidence=0.9)] == ["check", "end_hand"]


class TestCheckAroundAtTheStreetChange:
    def test_said_just_after_the_next_card_is_the_closed_street(self, tmp_path):
        tb = _Table(tmp_path)
        _checks_through_flop(tb)
        _board(tb, ["Kc"])                              # ターンの札（1 秒前）
        tb.say("チェック、アンド", spoken_at=tb.now - 0.5)   # 札の 0.5 秒後に話し始めた = フロップのこと
        assert _street(tb, "turn") == []
        assert "前のストリートのチェックアラウンド" in tb.notices[-1]
        tb.say("ベット 600")
        assert _street(tb, "turn") == [(4, "bet", 600)]

    def test_it_confirms_the_checks_the_card_filled_in(self, tmp_path):
        tb = _Table(tmp_path)
        _to_flop(tb)
        tb.tick(tb.now + 6.0)                           # フロップは誰の言葉も聞こえない
        _board(tb, ["Kc"])                              # ターンの札 → フロップのチェックを補う（要確認）
        flop = [a for a in tb.t._current_actions if a.street == "flop"]   # noqa: SLF001
        assert [(a.seat, a.action, a.actor_source, a.needs_review) for a in flop] == [
            (4, "check", "implied", True), (5, "check", "implied", True), (6, "check", "implied", True)]
        tb.say("チェックアラウンド", spoken_at=tb.now - 0.8)
        assert [a.needs_review for a in flop] == [False, False, False]
        assert all("check_around" in a.reason and a.raw_text == "チェックアラウンド" for a in flop)
        assert _street(tb, "turn") == [] and "裏付け" in tb.notices[-1]

    def test_said_well_after_the_card_is_the_current_street(self, tmp_path):
        tb = _Table(tmp_path)
        _checks_through_flop(tb)
        _board(tb, ["Kc"])
        tb.tick(tb.now + CHECK_AROUND_STREET_CHANGE_SEC + 2.0)   # ターンで全員がチェックした
        tb.say("チェックアラウンド")
        assert _street(tb, "turn") == [(4, "check", 0), (5, "check", 0), (6, "check", 0)]

    def test_it_still_checks_the_rest_of_an_open_round(self, tmp_path):
        tb = _Table(tmp_path)
        _to_flop(tb)
        tb.say("チェック")                              # 席4
        tb.tick(tb.now + 1.0)
        tb.say("チェックアラウンド")                    # 席5・6（ターンの札はまだ）
        assert _street(tb, "flop") == [(4, "check", 0), (5, "check", 0), (6, "check", 0)]


class TestCallRestatedWithTheRoundCloser:
    def test_second_call_in_the_closing_utterance_is_not_a_check(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal()
        tb.say("レイズ 600")                            # 席6（BTN）
        tb.say("コール")                                # 席4
        tb.say("コールします、コール")                  # 席5 のコールでプリフロップが閉じる + 言い直し
        assert _street(tb, "preflop")[-1] == (5, "call", 400)
        assert _street(tb, "flop") == []
        _board(tb, ["Jd", "9d", "3d"])
        tb.say("チェック")
        assert _street(tb, "flop") == [(4, "check", 0)]


class TestChopHeardAsCheck:
    def _to_turn(self, tb: _Table) -> None:
        _checks_through_flop(tb)
        _board(tb, ["Kc"])
        tb.tick(tb.now + 4.0)

    def test_chop_while_a_check_is_possible_is_a_check(self, tmp_path):
        tb = _Table(tmp_path)
        self._to_turn(tb)
        tb.say("チョップ")
        (record,) = [a for a in tb.t._current_actions if a.street == "turn"]   # noqa: SLF001
        assert (record.seat, record.action) == (4, "check") and record.needs_review
        assert "チェックの聞き違い" in tb.notices[-1]
        tb.say("1000")
        assert _street(tb, "turn") == [(4, "check", 0), (5, "bet", 1000)]

    def test_chop_facing_a_bet_is_not_a_check(self, tmp_path):
        tb = _Table(tmp_path)
        self._to_turn(tb)
        tb.say("ベット 1000")
        tb.say("チョップ")
        assert _street(tb, "turn") == [(4, "bet", 1000)]
        assert tb.hands == []

    def test_chop_with_seats_is_still_a_split(self, tmp_path):
        tb = _Table(tmp_path)
        self._to_turn(tb)
        tb.say("シート4 シート5 チョップ")
        (hand,) = tb.hands                              # 分けた（チェックにしない）
        assert [a for a in hand.actions if a.street == "turn"] == []


class TestCheckLeftOverFromTheClosedRound:
    def test_a_check_in_the_closing_utterance_is_not_the_flops(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal()
        tb.say("レイズ 600")                            # 席6（BTN）
        tb.say("コール")                                # 席4
        tb.say("コール、ロック、チェック")              # 席5 のコールでプリフロップが閉じる（余りのチェック）
        assert _street(tb, "flop") == [] and "余り" in tb.notices[-1]
        _board(tb, ["Jd", "9d", "3d"])
        tb.say("1800")
        assert _street(tb, "flop") == [(4, "bet", 1800)]

    def test_a_check_said_before_the_card_is_the_closed_rounds(self, tmp_path):
        tb = _Table(tmp_path)
        _checks_through_flop(tb)
        _board(tb, ["Kc"])
        tb.say("チェック", spoken_at=tb.now - 2.5)      # ターンの札（1 秒前）より前に話し始めた
        assert _street(tb, "turn") == []
        tb.say("ベット 600")
        assert _street(tb, "turn") == [(4, "bet", 600)]

    def test_a_check_after_the_card_is_the_streets(self, tmp_path):
        tb = _Table(tmp_path)
        _checks_through_flop(tb)
        _board(tb, ["Kc"])
        tb.say("チェック")
        assert _street(tb, "turn") == [(4, "check", 0)]

    def test_a_check_before_a_closed_streets_check_around_is_also_the_closed_streets(self, tmp_path):
        # 店舗 2026-10-06 05cccd6c ハンド 14: ターンを閉じた「チェック、チェック」のあとの「チェック、チェックラウンド、
        # ラストカード」（リバーの札はまだ）。最初のチェックがリバーの最初の人のチェックになっていた
        tb = _Table(tmp_path)
        _checks_through_flop(tb)
        _board(tb, ["Kc"])
        for _ in range(3):
            tb.say("チェック")                          # ターンは全員チェック = 閉じた
            tb.tick(tb.now + 1.0)
        tb.say("チェック、チェックラウンド、ラストカード")   # 閉じた発話とは別の発話
        assert _street(tb, "river") == []
        assert any("チェックアラウンドの言い直し" in n for n in tb.notices[-3:])
        _board(tb, ["2s"])
        tb.say("3500")
        assert _street(tb, "river") == [(4, "bet", 3500)]

    def test_a_check_and_check_around_on_the_open_street_are_the_streets(self, tmp_path):
        tb = _Table(tmp_path)
        _checks_through_flop(tb)
        _board(tb, ["Kc"])
        tb.say("チェック")                              # 席4
        tb.say("チェック、チェックアラウンド")          # 席5 のチェック → 残りの席6 もチェック
        assert _street(tb, "turn") == [(4, "check", 0), (5, "check", 0), (6, "check", 0)]

    def test_without_the_board_cards_a_check_is_kept(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal()
        for text in ("コール", "コール", "チェック"):   # プリフロップ（ボードの札は読めていない）
            tb.say(text)
        tb.tick(tb.now + 5.0)
        tb.say("チェック", spoken_at=tb.now - 3.0)
        assert _street(tb, "flop") == [(4, "check", 0)]

