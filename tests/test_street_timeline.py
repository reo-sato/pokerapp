"""tests/test_street_timeline.py

ストリートの札より前に話したアクションは、そのストリートのものではない（オーナー 2026-10-07:「ターンカードのディールや、
ターンカード/チェックアラウンドの宣言に先立ってターンのアクションが宣言されることはありません」）。

- 札より前に話したチェック・コールが次のストリートに入っていた → 閉じたラウンドの言い直しとして外す（手番がずれない）。
- 札より前に話したベット・レイズが次のストリートに入っていた → 前のラウンドは閉じていなかった: そのラウンドを閉じた声の
  チェック（多く聞こえた語）を外して組み直す（要確認 `reopened_before_<street>_card`）。店舗 05cccd6c ハンド 19。
- 直せない（前のラウンドを閉じたのが札の離脱など）ときは要確認の印だけ（`spoken_before_<street>_card`）。
- 札より前に離れた札を次のストリートのアクションにしたら要確認（`left_before_<street>_card`）。
- フロップの札が離れて読めた（札の位置がずれた疑い）ハンドでは、札の時刻で直さない・印も付けない。
- 組み直しは発話の処理のあと。live と replay で同じになる。
"""
from __future__ import annotations

import pytest

pytest.importorskip("pokerkit")

from tests.test_departure_words import _board, _replayed  # noqa: E402
from tests.test_rfid_folds import _Table  # noqa: E402

# 席 4・5・6（ボタン 席6）: プリフロップは 6, 4, 5、フロップからは 4, 5, 6


def _to_flop(tb: _Table, spread: float = 0.0) -> None:
    tb.deal()
    for word in ("コール", "コール", "チェック"):
        tb.say(word)
    _board(tb, ["2c", "7d"])
    if spread:
        tb.tick(tb.now + spread)
    _board(tb, ["9s"], start=3)
    tb.tick(tb.now + 2.0)


def _closed_flop_then(tb: _Table, *words: str) -> None:
    """フロップを 3 つのチェックで閉じ（1 つは多く聞こえた語）、ターンの札の前に `words` を話す。"""
    for word in ("チェック", "チェック", "チェック"):
        tb.say(word)
    tb.tick(tb.now + 1.0)
    for word in words:
        tb.say(word)
        tb.tick(tb.now + 1.0)


def _turn_card(tb: _Table) -> None:
    tb.tick(tb.now + 5.0)
    _board(tb, ["Jc"], start=4)
    tb.tick(tb.now + 2.0)


class TestWagerBeforeTheCard:
    def test_the_previous_street_is_reopened(self, tmp_path):
        tb = _Table(tmp_path)
        _to_flop(tb)
        _closed_flop_then(tb, "1200", "コール", "コール")
        assert tb.played()[6:] == [("turn", 4, "bet", 1200), ("turn", 5, "call", 1200), ("turn", 6, "call", 1200)]
        _turn_card(tb)
        tb.say("チェック")                            # ターンの札のあとの最初の発話の処理のあとで組み直す
        assert tb.played()[3:] == [
            ("flop", 4, "check", 0), ("flop", 5, "check", 0), ("flop", 6, "bet", 1200),
            ("flop", 4, "call", 1200), ("flop", 5, "call", 1200), ("turn", 4, "check", 0),
        ]
        bet = tb.t._current_actions[5]               # noqa: SLF001
        assert "reopened_before_turn_card" in bet.reason and bet.needs_review
        assert any("フロップのアクションとみて組み直しました" in n for n in tb.notices)

    def test_replay_matches_live(self, tmp_path):
        tb = _Table(tmp_path)
        _to_flop(tb)
        _closed_flop_then(tb, "1200", "コール", "コール")
        _turn_card(tb)
        tb.say("チェック")
        tb.say("シート4 ウィナー")
        (hand,) = tb.hands
        live = [(a.street, a.seat, a.action, a.amount) for a in hand.actions]
        assert ("flop", 6, "bet", 1200) in live
        assert _replayed(tb, tmp_path) == live

    def test_a_wager_after_the_card_is_unchanged(self, tmp_path):
        tb = _Table(tmp_path)
        _to_flop(tb)
        _closed_flop_then(tb)
        _turn_card(tb)
        tb.say("1200")
        tb.say("コール")
        assert tb.played()[6:] == [("turn", 4, "bet", 1200), ("turn", 5, "call", 1200)]
        assert not any("card" in (a.reason or "") for a in tb.t._current_actions)   # noqa: SLF001

    def test_a_spread_out_flop_leaves_the_record(self, tmp_path):
        # フロップの 3 枚目が 13 秒あとに読めた: 札の位置がずれた疑い（09-29 d0f055fb ハンド 5）
        tb = _Table(tmp_path)
        _to_flop(tb, spread=13.0)
        _closed_flop_then(tb, "1200")
        _turn_card(tb)
        tb.say("コール")
        assert ("turn", 4, "bet", 1200) in tb.played()
        assert not any("card" in (a.reason or "") for a in tb.t._current_actions)   # noqa: SLF001

    def test_a_wager_without_a_heard_amount_does_not_reopen(self, tmp_path):
        # 額の聞こえない賭け（あいまいな読み）は前のラウンドを開き直す根拠にしない: 印だけ（09-29 d0f055fb ハンド 5 では
        # 残りの人数の聞き違いを賭けとみて、正しいコールを外していた）
        tb = _Table(tmp_path)
        _to_flop(tb)
        _closed_flop_then(tb, "ベット")
        _turn_card(tb)
        tb.say("コール")
        bet = next(a for a in tb.t._current_actions if a.action == "bet")   # noqa: SLF001
        assert (bet.street, bet.seat) == ("turn", 4)
        assert "spoken_before_turn_card" in bet.reason and bet.needs_review

    def test_the_closer_in_the_same_utterance_is_kept(self, tmp_path):
        # 閉じた語と賭けが同じ発話（言った順は確か）: 閉じた語は外さない（数字だけなら既存の規則が額を外す）
        tb = _Table(tmp_path)
        _to_flop(tb)
        tb.say("チェック、チェック、チェック、ベット 1200")
        _turn_card(tb)
        tb.say("コール")
        assert tb.played()[3:6] == [("flop", 4, "check", 0), ("flop", 5, "check", 0), ("flop", 6, "check", 0)]
        bet = next(a for a in tb.t._current_actions if a.action == "bet")   # noqa: SLF001
        assert (bet.street, bet.seat) == ("turn", 4)
        assert "spoken_before_turn_card" in bet.reason

    def test_waits_for_the_third_flop_card(self, tmp_path):
        # フロップの 3 枚目がまだ（読めない札があるかもしれない）: 札の時刻で直すのは 3 枚そろってから。3 枚目が離れて
        # 読めたら（札の位置がずれた疑い）直さない
        tb = _Table(tmp_path)
        tb.deal()
        tb.say("コール")
        tb.say("コール")
        tb.say("チェック")
        tb.tick(tb.now + 1.0)
        tb.say("チェック")                            # プリフロップを閉じたあとの余りの「チェック」（札の 4 秒前）
        tb.tick(tb.now + 4.0)
        _board(tb, ["2c", "7d"])
        tb.tick(tb.now + 2.0)
        tb.say("ベット 500")
        assert tb.t._before_card                      # noqa: SLF001 — 3 枚目を待っている
        tb.tick(tb.now + 12.0)
        _board(tb, ["9s"], start=3)
        tb.tick(tb.now + 2.0)
        tb.say("コール")
        assert not tb.t._before_card                  # noqa: SLF001
        assert not any("before_flop_card" in (a.reason or "") for a in tb.t._current_actions)   # noqa: SLF001

    def test_unfixable_is_marked(self, tmp_path):
        # 前のラウンドを閉じたのが札の離脱（声のチェックではない）: 外す語が無いので印だけ
        tb = _Table(tmp_path)
        _to_flop(tb)
        tb.say("チェック")
        tb.say("チェック")
        tb.lift(6)
        tb.tick(tb.now + 3.5)                        # 席6 の札の離脱（ベットの無いところ）でフロップが閉じる
        assert tb.played()[-1][:3] == ("flop", 6, "fold")
        tb.say("1200")
        _turn_card(tb)
        tb.say("コール")
        bet = next(a for a in tb.t._current_actions if a.action == "bet")   # noqa: SLF001
        assert (bet.street, bet.seat) == ("turn", 4)
        assert "spoken_before_turn_card" in bet.reason and bet.needs_review


class TestLateCardRead:
    """札が読めるのは配ってから少し遅れる（宣言から札が読めるまで最大 2.5 秒）。読み取りが遅れた札の直前に話した本当の
    アクションは書き換えない（書き換えるのは札の `BEFORE_CARD_FIX_MARGIN_SEC` 秒以上前だけ。印は付けてよい）。"""

    def _real_flop(self, tb: _Table) -> None:
        _to_flop(tb)
        for word in ("チェック", "チェック", "チェック"):      # フロップは本当に 3 人のチェックで閉じた
            tb.say(word)
            tb.tick(tb.now + 1.0)
        tb.tick(tb.now + 3.0)

    @pytest.mark.parametrize("late", [1.5, 2.5])
    def test_a_real_check_before_a_late_card_is_kept(self, tmp_path, late):
        tb = _Table(tmp_path)
        self._real_flop(tb)
        tb.say("チェック")                            # 席4 のターンのチェック（札は配られたが、まだ読めていない）
        tb.tick(tb.now + late)
        _board(tb, ["Jc"], start=4)
        tb.tick(tb.now + 1.0)
        tb.say("ベット 500")
        assert tb.played()[6:] == [("turn", 4, "check", 0), ("turn", 5, "bet", 500)]
        assert not any("札が置かれる" in n for n in tb.notices)

    def test_a_card_read_long_after_the_turn_actions(self, tmp_path):
        # ターンの札が 12 秒読めなかった（店舗 09-27）: ターンのアクションはフロップが閉じてから 8 秒あと = 札を配ったあと。
        # 言い直しとして外さず、フロップも開き直さない（印だけ）
        tb = _Table(tmp_path)
        self._real_flop(tb)
        tb.tick(tb.now + 5.0)
        tb.say("チェック")
        tb.tick(tb.now + 2.0)
        tb.say("ベット 500")
        tb.tick(tb.now + 2.0)
        tb.say("コール")
        tb.tick(tb.now + 8.0)
        _board(tb, ["Jc"], start=4)
        tb.tick(tb.now + 1.0)
        tb.say("コール")
        assert tb.played()[6:9] == [("turn", 4, "check", 0), ("turn", 5, "bet", 500), ("turn", 6, "call", 500)]
        assert not any("札が置かれる" in n for n in tb.notices)
        assert "spoken_before_turn_card" in tb.t._current_actions[6].reason   # noqa: SLF001

    @pytest.mark.parametrize("late", [1.5, 2.5])
    def test_a_real_bet_before_a_late_card_is_kept(self, tmp_path, late):
        tb = _Table(tmp_path)
        self._real_flop(tb)
        tb.say("ベット 500")
        tb.tick(tb.now + late)
        _board(tb, ["Jc"], start=4)
        tb.tick(tb.now + 1.0)
        tb.say("コール")
        assert tb.played()[6:] == [("turn", 4, "bet", 500), ("turn", 5, "call", 500)]


class TestAtTheEndOfTheHand:
    """札のあとに発話が無いままハンドを終えるときも、終える前に直す（live = replay）。"""

    def test_the_winner_word_right_after_the_card(self, tmp_path):
        tb = _Table(tmp_path)
        _to_flop(tb)
        _closed_flop_then(tb, "1200", "コール", "コール")
        _turn_card(tb)
        tb.say("シート4 ウィナー")
        (hand,) = tb.hands
        live = [(a.street, a.seat, a.action, a.amount) for a in hand.actions]
        assert ("flop", 6, "bet", 1200) in live
        assert _replayed(tb, tmp_path) == live

    def test_two_cards_waiting(self, tmp_path):
        tb = _Table(tmp_path)
        _to_flop(tb)
        _closed_flop_then(tb, "1200", "コール", "コール")
        _turn_card(tb)
        tb.tick(tb.now + 5.0)
        _board(tb, ["2h"], start=5)                   # リバーの札も、発話が無いまま
        tb.tick(tb.now + 5.0)
        tb.say("シート4 ウィナー")
        (hand,) = tb.hands
        live = [(a.street, a.seat, a.action, a.amount) for a in hand.actions]
        assert ("flop", 6, "bet", 1200) in live
        assert _replayed(tb, tmp_path) == live


class TestCheckBeforeTheCard:
    def test_a_restated_check_is_dropped(self, tmp_path):
        tb = _Table(tmp_path)
        _to_flop(tb)
        _closed_flop_then(tb, "チェック")            # ターンの札の前の余りの「チェック」
        assert tb.played()[-1] == ("turn", 4, "check", 0)
        _turn_card(tb)
        tb.say("ベット 500")
        assert tb.played()[6:] == [("turn", 4, "bet", 500)]          # 手番がずれない（席5 のベットにならない）
        assert tb.t._hand_needs_review                               # noqa: SLF001
        assert any("言い直しとみて外しました" in n for n in tb.notices)

    def test_replay_matches_live(self, tmp_path):
        tb = _Table(tmp_path)
        _to_flop(tb)
        _closed_flop_then(tb, "チェック")
        _turn_card(tb)
        tb.say("ベット 500")
        tb.say("コール")
        tb.say("シート4 ウィナー")
        (hand,) = tb.hands
        live = [(a.street, a.seat, a.action, a.amount) for a in hand.actions]
        assert live[6:8] == [("turn", 4, "bet", 500), ("turn", 5, "call", 500)]
        assert _replayed(tb, tmp_path) == live


class TestFoldBeforeTheCard:
    def test_a_departure_before_the_card_is_marked(self, tmp_path):
        # フロップで札が離れた席5 のフォールドが、手番が来たターンに入る（間の聞き落とし）: 記録は変えず要確認
        tb = _Table(tmp_path)
        _to_flop(tb)
        for word in ("チェック", "チェック", "チェック"):
            tb.say(word)
        tb.lift(5)
        _turn_card(tb)
        tb.say("チェック")
        fold = next(a for a in tb.t._current_actions if a.seat == 5 and a.action == "fold")   # noqa: SLF001
        assert fold.street == "turn"
        assert "left_before_turn_card" in fold.reason and fold.needs_review

    def test_a_fold_that_closes_the_street_at_the_next_card_is_not_marked(self, tmp_path):
        # フロップのベットに降りた席6 の札の離脱を、ターンの札で閉じるときに入れる: フロップの札よりあとなので印なし
        # （ラウンドが閉じて次のストリートになったあとの札の時刻と比べない）
        tb = _Table(tmp_path)
        _to_flop(tb)
        tb.say("ベット 500")
        tb.lift(6)                                    # 手番は席5（席6 の離脱はまだ入れない）
        tb.tick(tb.now + 3.5)
        _turn_card(tb)
        fold = next(a for a in tb.t._current_actions if a.seat == 6 and a.action == "fold")   # noqa: SLF001
        assert fold.street == "flop"
        assert "before" not in (fold.reason or "")


class TestStreetCall:
    """ディーラーのストリートの宣言（推定器が札の読めなかったストリートの境目に使う）。"""

    @pytest.mark.parametrize("text,index", [
        ("ターンです。", 4), ("いざ、ターンです。", 4), ("スリーウェイ、ターンです。どうぞ。", 4),
        ("ラストカード", 5), ("チェック、チェックラウンド、ラストカード", 5), ("ヘッズアップ ラストカード", 5),
        ("では、プラストカードです。", 5), ("リバーです。", 5), ("ヘッツアップ リバーです。", 5),
        ("ターンカード", 4), ("リバーカードです", 5),
    ])
    def test_read(self, text, index):
        from audio.recognizer import street_call

        found = street_call(text)
        assert found is not None and found[0] == index and 0.0 <= found[1] < 1.0

    @pytest.mark.parametrize("text", ["以上、パターンです。", "ちょっとワンパターン化してきてない?",
                                      "チェック、チェックアウンド、ラストターンです。", "ターンオーバー", "チェック"])
    def test_not_a_street_call(self, text):
        from audio.recognizer import street_call

        assert street_call(text) is None

    def test_not_an_action(self):
        from audio.recognizer import parse_actions

        assert parse_actions("ラストカード") == []
