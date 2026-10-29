"""オールインのランアウト（店舗 2026-10-29 メモ ハンド 121「オールインコールはハンドを前に出してボードをディールしますよ。
ここからハンドが読まれなくなっちゃった」）。書き起こしを後回しにするとライブはオールインを知らないので、札を前に出した
2 人の離脱がフォールドになり、勝者を取り違えた（10/29 は 19 ハンドのうち 5）。

最後の 1 人を残すフォールドの確定待ちのあいだにボードの札が置かれたら、オールインのショーダウンとみる: 離脱を取り消し、
ボードが揃ったら手札で決める（要確認）。中央を通した札（マック）なら本当のフォールド。
"""
from __future__ import annotations

import pytest

pytest.importorskip("pokerkit")

from core.events import RFIDEvent  # noqa: E402
from core.game_state import PlayerState  # noqa: E402
from integration.replay import replay_events  # noqa: E402
from tests.test_rfid_folds import _Table  # noqa: E402

# 席 4・5 が残る。ボード 7d 7h 2c 9s Ks で 席5（4s 7c）がトリップス
BOARD = ["7d", "7h", "2c", "9s", "Ks"]


def _board(tb: _Table, index: int, card: str) -> None:
    ev = RFIDEvent(tag_id=card, card=card, reader_id="b", role="board", seat=None,
                   timestamp=tb.now, raw_tag_id=card, board_index=index)
    tb.recorder.record(ev)
    tb.t._process_rfid_event(ev)   # noqa: SLF001
    tb.board_count += 1


def _runout(tb: _Table, muck_first: bool = False) -> None:
    tb.deal()
    tb.lift(6)                                     # 席6 は降りる
    tb.tick(tb.now + 3.5)
    (tb.muck if muck_first else tb.lift)(4)        # オールインとコールのあと、2 人が札を前に出す
    tb.tick(tb.now + 1.0)
    tb.lift(5)
    tb.tick(tb.now + 4.0)                          # 席4 は離れて 3 秒 = フォールドの確定待ち
    for i, card in enumerate(BOARD[:3], start=1):
        _board(tb, i, card)
        tb.tick(tb.now + 0.5)
    tb.tick(tb.now + 3.0)
    _board(tb, 4, BOARD[3])
    tb.tick(tb.now + 3.0)
    _board(tb, 5, BOARD[4])
    tb.tick(tb.now + 15.0)


def test_board_cards_after_both_hands_go_forward_are_a_runout(tmp_path):
    tb = _Table(tmp_path)
    _runout(tb)
    (hand,) = tb.hands
    assert hand.winner_source == "cards" and hand.winner_seat == 5
    assert hand.board == BOARD
    assert [(a.seat, a.action) for a in hand.actions if a.action == "fold"] == [(6, "fold")]
    assert hand.review_required
    assert any("ランアウト" in n for n in tb.notices)


def test_cards_through_the_middle_are_still_a_fold(tmp_path):
    tb = _Table(tmp_path)
    _runout(tb, muck_first=True)
    hand = tb.hands[0]
    assert hand.winner_source == "fold" and hand.winner_seat == 5
    assert not any("ランアウト" in n for n in tb.notices)


def test_it_is_the_same_on_replay(tmp_path):
    tb = _Table(tmp_path)
    _runout(tb)
    replayed = replay_events(
        tb.recorder.events, backend="pokerkit",
        players=[PlayerState(seat=s, name=f"P{s}", stack=10000) for s in (4, 5, 6)],
        sb=100, bb=200, session_id="replay", out_dir=tmp_path / "replay",
        auto_new_hand=True, auto_winner=True, rfid_folds=True,
    )
    assert (replayed[0].winner_seat, replayed[0].winner_source) == (tb.hands[0].winner_seat, "cards")
    assert [(a.seat, a.action) for a in replayed[0].actions] == [(a.seat, a.action) for a in tb.hands[0].actions]
