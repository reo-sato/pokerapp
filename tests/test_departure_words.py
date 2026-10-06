"""tests/test_departure_words.py

札の離脱と、手番の順で付けた語の食い違い（店舗 2026-10-06 の 79 ハンドを Fable 5.1 と見直した結果）:

- ベットに向き合っていない席の札の離脱（ベットの無いところのフォールド）は、店舗の真のアクションのフォールド 171 のうち
  0。その席の最後の声のコールが札の離れたあと（語が離脱の 0.5 秒前〜6 秒後）なら、そのコールは次の人のもので、その席は
  コールの前のベットに降りていた → 離脱をコールの前に移して組み直す（要確認 `late_departure_retracted_call`）。コールが
  札の離れるより前なら付け直さない（その席より前の人のコール = 手番のずれはもっと前にある）。
- 1 つの席に「フォールド」の語は 1 つ: もう「フォールド」と言われて札が離れかけている席に、次の「フォールド」は付けない
  （次の人のこと）。
- 「コールド」「コード」は聞き違いのコール: その語のころに札が離れた席があればフォールド、無ければコール（要確認）。
- 配る前からの不在（前のハンドの札が離れたまま）は、このハンドのフォールドにしない。
- チェックで閉じたラウンドのあと、次のストリートの札より 3 秒以上前に話した賭けは要確認（記録は変えない）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from audio.recognizer import parse_actions
from core.events import RFIDEvent
from core.game_state import PlayerState
from integration.engine import FOLDOUT_CONFIRM_SEC

pytest.importorskip("pokerkit")

from integration.replay import replay_events  # noqa: E402
from tests.test_rfid_folds import _Table  # noqa: E402

FOUR = (4, 5, 6, 7)                 # ボタン 席7 → プリフロップは 6, 7, 4, 5、フロップからは 4, 5, 6, 7
HOLES4 = {4: ["6s", "Qc"], 5: ["4s", "7c"], 6: ["Qd", "Ac"], 7: ["Kh", "Kd"]}


def _board(tb: _Table, cards: list[str], start: int = 1) -> None:
    for i, card in enumerate(cards, start=start):
        ev = RFIDEvent(tag_id=card, card=card, reader_id="b", role="board", seat=None,
                       timestamp=tb.now, raw_tag_id=card, board_index=i)
        tb.recorder.record(ev)
        tb.t._process_rfid_event(ev)            # noqa: SLF001


def _replayed(tb: _Table, tmp_path: Path) -> list[tuple]:
    replayed = replay_events(
        tb.recorder.events, backend="pokerkit",
        players=[PlayerState(seat=s, name=f"P{s}", stack=10000) for s in tb.seats],
        sb=100, bb=200, session_id="replay", out_dir=tmp_path / "replay",
        auto_new_hand=True, auto_winner=True, rfid_folds=True,
    )
    return [(a.street, a.seat, a.action, a.amount) for h in replayed for a in h.actions]


def _finish_by_folds(tb: _Table, *seats: int) -> None:
    for seat in seats:
        tb.lift(seat)
        tb.tick(tb.now + 3.5)
    tb.tick(tb.now + FOLDOUT_CONFIRM_SEC + 0.5)


class TestGarbledCallWords:
    @pytest.mark.parametrize("text", ["コールド", "コールド。", "コールドです", "コード", "コードです"])
    def test_read_as_a_garbled_call(self, text):
        (event,) = parse_actions(text)
        assert event.action == "call" and "garbled_call" in event.parse_flags

    @pytest.mark.parametrize("text", ["チュック", "チョック", "チュック。"])
    def test_a_garbled_check_word(self, text):
        # 店舗の 15 回のうち 14 回、第 2 の耳がその場で「チェック」と聞いた（音の近さでは「チョップ」とも同じくらい）
        (event,) = parse_actions(text)
        assert event.action == "check" and "fuzzy_keyword" in event.parse_flags

    def test_with_other_words(self):
        events = parse_actions("コールド、ヘッドアップ!")
        assert [(e.action, "garbled_call" in e.parse_flags) for e in events] == [("call", True), ("heads_up", False)]
        assert [(e.action, e.parse_flags) for e in parse_actions("コード、コール")] == [("call", ())]
        assert parse_actions("ゴールド") == []

    def test_cards_leaving_at_the_word_make_it_a_fold(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal()
        tb.say("レイズ 600")                        # 席6
        tb.lift(4)
        spoken = tb.now + 0.1
        tb.tick(tb.now + 1.5)
        tb.say("コールド", spoken_at=spoken)
        assert tb.played()[-1] == ("preflop", 4, "fold", 0)
        tb.say("コール")
        assert tb.played()[-1] == ("preflop", 5, "call", 400)

    def test_without_a_departure_it_is_a_call_to_review(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal()
        tb.say("レイズ 600")
        tb.say("コード")
        assert tb.played()[-1] == ("preflop", 4, "call", 500)
        record = tb.t._current_actions[-1]          # noqa: SLF001
        assert record.needs_review and "garbled_call" in record.reason


class TestOneFoldWordPerSeat:
    """店舗 5b1ca234 ハンド 1・9db1e032 ハンド 5・80078377 ハンド 1:「フォールド」（札は席に残る）→ その席の札が離れる →
    確定の 3 秒より前に「フォールド、コール」。2 つ目の「フォールド」が 1 つ目の席に吸われ、コールが次の人に付いていた。"""

    def _hand(self, tb: _Table) -> None:
        tb.deal(HOLES4)
        tb.say("600")                               # 席6 レイズ
        tb.say("フォールド")                        # 席7（札はまだ席にある）
        tb.tick(tb.now + 1.3)
        tb.lift(7)
        tb.tick(tb.now + 1.0)                       # 席7 の離脱はまだ確定していない
        tb.say("フォールド、コール")

    def test_the_second_fold_word_is_the_next_seats(self, tmp_path):
        tb = _Table(tmp_path, seats=FOUR)
        self._hand(tb)
        assert tb.played() == [("preflop", 6, "raise", 600), ("preflop", 7, "fold", 0),
                               ("preflop", 4, "fold", 0), ("preflop", 5, "call", 400)]

    def test_replay_matches_live(self, tmp_path):
        tb = _Table(tmp_path, seats=FOUR)
        self._hand(tb)
        tb.say("ベット 1000")                       # フロップ: 席5
        _finish_by_folds(tb, 6)
        (live,) = tb.hands
        assert _replayed(tb, tmp_path) == [(a.street, a.seat, a.action, a.amount) for a in live.actions]
        assert live.winner_seat == 5


class TestDepartureBeforeTheCall:
    """店舗 9db1e032 ハンド 3・e82f5005 ハンド 6・13: 席の札が離れた直後（確定の 3 秒の前）の「コール」が、その席に
    付いていた。次のストリートでその席がベットの無いところで降りた（`no_bet`）ことになっていた。"""

    def _hand(self, tb: _Table, gap: float = 0.6) -> None:
        tb.deal(HOLES4)
        tb.say("600")                               # 席6
        tb.say("コール")                            # 席7
        tb.lift(4)                                  # 席4（SB）が降りた
        spoken = tb.now + gap
        tb.tick(tb.now + max(gap, 0.0) + 0.8)
        tb.say("コール", spoken_at=spoken)          # 席5（BB）のコール。離脱はまだ確定していない
        tb.tick(tb.now + 4.0)
        _board(tb, ["2c", "7d", "9s"])
        tb.tick(tb.now + 2.0)

    def test_the_call_goes_to_the_next_seat(self, tmp_path):
        tb = _Table(tmp_path, seats=FOUR)
        self._hand(tb)
        tb.say("チェック")                          # フロップ: 席5
        assert tb.played() == [
            ("preflop", 6, "raise", 600), ("preflop", 7, "call", 600), ("preflop", 4, "fold", 0),
            ("preflop", 5, "call", 400), ("flop", 5, "check", 0),
        ]
        fold = tb.t._current_actions[2]             # noqa: SLF001
        assert "late_departure_retracted_call" in fold.reason and fold.needs_review
        assert any("付け直しました" in n for n in tb.notices)

    def test_replay_matches_live(self, tmp_path):
        tb = _Table(tmp_path, seats=FOUR)
        self._hand(tb)
        tb.say("チェック")
        tb.say("ベット 1000")                       # 席6
        _finish_by_folds(tb, 7, 5)
        (live,) = tb.hands
        assert ("preflop", 4, "fold", 0) in [(a.street, a.seat, a.action, a.amount) for a in live.actions]
        assert _replayed(tb, tmp_path) == [(a.street, a.seat, a.action, a.amount) for a in live.actions]
        assert live.winner_seat == 6

    def test_retracted_before_the_hand_ends_without_more_speech(self, tmp_path):
        # 付け直しは発話の処理のあとに行うが、次の発話を待たずにハンドが終わる（勝者の宣言・配布・確定）なら、その前に行う
        tb = _Table(tmp_path, seats=FOUR)
        self._hand(tb)                              # フロップの札で 席4 がベットの無いところで降りることになる
        assert ("flop", 4, "fold", 0) in tb.played()
        tb.say("シート6 ウィナー")
        (hand,) = tb.hands
        rows = [(a.street, a.seat, a.action, a.amount) for a in hand.actions]
        assert rows[:4] == [("preflop", 6, "raise", 600), ("preflop", 7, "call", 600), ("preflop", 4, "fold", 0),
                            ("preflop", 5, "call", 400)]
        assert "late_departure_retracted_call" in hand.actions[2].reason
        assert _replayed(tb, tmp_path) == rows

    def test_a_garbled_word_rebuilt_after_the_retraction(self, tmp_path):
        # 組み直しで「これで終わりです」を読み直すとき、札の離脱の入力が語の前（replay）でも後（live）でも同じに（Fable の
        # 見直し 2 回目: 席4 の離脱と組にした語が、組み直しで席4 / 席5 のコールになり live と replay が食い違った）
        tb = _Table(tmp_path)
        tb.deal()
        tb.say("レイズ 600")
        tb.lift(4)
        spoken = tb.now + 0.1
        tb.tick(tb.now + 1.5)
        tb.say("これで終わりです。", spoken_at=spoken)
        tb.say("コール")
        tb.lift(5)                                  # コールと同時に席5 の札が離れた = 席5 はコールの前に降りていた
        tb.tick(tb.now + 3.5 + FOLDOUT_CONFIRM_SEC)
        (live,) = tb.hands
        rows = [(a.street, a.seat, a.action, a.amount) for a in live.actions]
        assert rows == [("preflop", 6, "raise", 600), ("preflop", 4, "fold", 0), ("preflop", 5, "fold", 0)]
        assert "late_departure_retracted_call" in live.actions[2].reason and live.winner_seat == 6
        replayed = replay_events(
            tb.recorder.events, backend="pokerkit",
            players=[PlayerState(seat=s, name=f"P{s}", stack=10000) for s in tb.seats],
            sb=100, bb=200, session_id="replay", out_dir=tmp_path / "replay",
            auto_new_hand=True, auto_winner=True, rfid_folds=True,
        )
        assert [(a.street, a.seat, a.action, a.amount) for h in replayed for a in h.actions] == rows

    def test_a_call_before_the_departure_is_kept(self, tmp_path):
        # 札が離れる 1 秒前の「コール」: その席より前の人のコール（店舗 9d1d8536 ハンド 4 = ボタンのずれ、e82f5005 ハンド 6
        # = 持ち上げ）。付け直すとずれが隠れるので、ベットの無いところのフォールド（要確認）のまま推定器に任せる
        tb = _Table(tmp_path, seats=FOUR)
        tb.deal(HOLES4)
        tb.say("600")
        tb.say("コール")
        tb.say("コール")                            # 席4
        tb.tick(tb.now + 1.0)
        tb.lift(4)
        tb.tick(tb.now + 4.0)
        tb.say("コール")                            # 席5
        _board(tb, ["2c", "7d", "9s"])
        tb.tick(tb.now + 2.0)
        tb.say("チェック")
        assert ("preflop", 4, "call", 500) in tb.played()
        assert not any("late_departure_retracted_call" in (a.reason or "") for a in tb.t._current_actions)  # noqa: SLF001

    def test_a_call_long_before_the_departure_is_kept(self, tmp_path):
        # 離脱がコールの 3 秒より前ではない（語のあと 10 秒）: 別の理由（聞き落としたベット）かもしれないので動かさない
        tb = _Table(tmp_path, seats=FOUR)
        tb.deal(HOLES4)
        tb.say("600")
        tb.say("コール")
        tb.say("コール")                            # 席4
        tb.tick(tb.now + 10.0)
        tb.lift(4)
        tb.tick(tb.now + 4.0)
        tb.say("コール")                            # 席5
        _board(tb, ["2c", "7d", "9s"])
        tb.tick(tb.now + 2.0)
        tb.say("チェック")
        assert ("preflop", 4, "call", 500) in tb.played()
        assert not any("late_departure_retracted_call" in (a.reason or "") for a in tb.t._current_actions)  # noqa: SLF001

    def test_a_call_with_the_seat_named_is_kept(self, tmp_path):
        tb = _Table(tmp_path, seats=FOUR)
        tb.deal(HOLES4)
        tb.say("600")
        tb.say("コール")
        tb.lift(4)
        spoken = tb.now + 0.6
        tb.tick(tb.now + 1.4)
        tb.say("シート4 コール", spoken_at=spoken)  # 席を言った = 手番の推定ではない
        tb.tick(tb.now + 4.0)
        tb.say("コール")
        _board(tb, ["2c", "7d", "9s"])
        tb.tick(tb.now + 2.0)
        tb.say("チェック")
        assert ("preflop", 4, "call", 500) in tb.played()


class TestAbsenceBeforeTheDeal:
    """店舗 e82f5005 ハンド 13: 前のハンドで席5 の札が中央を通ったまま（在否の読み直しの間）このハンドが始まり、
    席5 が配る 30 秒前の時刻でフォールドになっていた。"""

    def test_it_is_not_a_fold_of_this_hand(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal()
        dealt = tb.t._hand_started_epoch            # noqa: SLF001
        tb.cards[5] = []                            # 在否はまだ前のハンドのまま
        tb.absent_since[5] = dealt - 33.0
        tb.mucked_at[5] = dealt - 18.0
        tb.tick(tb.now + 4.0)
        tb.put(5, ["4s", "7c"])
        tb.say("600")
        tb.say("コール")
        tb.say("コール")
        assert tb.played() == [("preflop", 6, "raise", 600), ("preflop", 4, "call", 500),
                               ("preflop", 5, "call", 400)]


class TestWagerBeforeTheStreetCard:
    """店舗 05cccd6c ハンド 19: フロップの「チェック」が 1 つ多く聞こえ（聞き直しの重複）、フロップのベット「千二百」が
    ターンの札の 13.5 秒前なのにターンのベットになった。記録は変えず要確認にする。"""

    def _to_flop(self, tb: _Table) -> None:
        tb.deal()
        tb.say("コール")
        tb.say("コール")
        tb.say("チェック")
        _board(tb, ["2c", "7d", "9s"])
        tb.tick(tb.now + 2.0)

    def test_flagged_when_spoken_well_before_the_card(self, tmp_path):
        tb = _Table(tmp_path)
        self._to_flop(tb)
        for _ in range(3):
            tb.say("チェック")
        tb.tick(tb.now + 1.0)
        tb.say("1200")                              # ターンの札の前
        assert tb.played()[-1] == ("turn", 4, "bet", 1200)
        tb.tick(tb.now + 5.0)
        _board(tb, ["Jc"], start=4)
        tb.tick(tb.now + 1.0)
        record = tb.t._current_actions[-1]          # noqa: SLF001
        assert (record.street, record.action) == ("turn", "bet")
        assert "wager_before_street_card" in record.reason and record.needs_review

    def test_not_flagged_after_the_card(self, tmp_path):
        tb = _Table(tmp_path)
        self._to_flop(tb)
        for _ in range(3):
            tb.say("チェック")
        _board(tb, ["Jc"], start=4)
        tb.tick(tb.now + 1.0)
        tb.say("1200")
        record = tb.t._current_actions[-1]          # noqa: SLF001
        assert "wager_before_street_card" not in (record.reason or "")
