"""tests/test_hand_before_deal.py

配る前に `n`（「ハンド開始」）で始めたハンド（店舗の 5 回目の通しテスト, セッション c9e150f3）:

- 配るまで卓は空なので、15 秒後に「片付け = プレーの終わり」とみて聞き流しに入っていた。そのまま配っても
  プレー中に戻らず、ボードの札と声を読まずに、ショーダウンで前に出した札をプリフロップのフォールドにした。
  → まだ何も起きていないハンドの空の卓は片付けではない（プレー中のまま）。
- 配るまでの間にボードのリーダーの上で札を混ぜる（ウォッシュ）と、その札がボードになる。
  → 手札が 1 枚も届いていないハンドのボードの札は読まない。RFID が振った位置は最初の手札で捨てる。
"""
from __future__ import annotations

import threading

import pytest

from core.events import AudioEvent, RFIDEvent
from integration.engine import TABLE_CLEAR_SEC

pytest.importorskip("pokerkit")

from tests.test_rfid_folds import _Table  # noqa: E402

# セッション c9e150f3 の実際の札（ボタン 席6）。`n` からの秒数。
HOLES = [(21.7, 4, "Jd"), (24.2, 4, "2s"), (25.0, 5, "5s"), (25.4, 5, "6h"), (26.5, 6, "8s"), (26.5, 6, "Qd")]
BOARD = [(45.5, "Tc"), (45.8, "7c"), (48.5, "5d"), (67.0, "4h"), (75.0, "3h")]
LIFTS = [(60.9, 4), (83.7, 6), (84.5, 5)]   # 席4 はフロップで降りた / 席6・席5 はショーダウンで札を前に出した


class _ManualTable(_Table):
    """run() の 1 周に片付けの判断も入れ、RFID のリセット（`on_new_hand`）の回数を数える卓。"""

    def __init__(self, tmp_path, seats=(4, 5, 6)):
        super().__init__(tmp_path, seats)
        self.gate = threading.Event()
        self.t._listen_gate = self.gate          # noqa: SLF001
        self.resets = 0

        def reset() -> None:
            self.resets += 1

        self.t._on_new_hand = reset              # noqa: SLF001

    def tick(self, until: float, step: float = 0.1) -> None:
        while self.now < until - 1e-9:
            self.now = round(self.now + step, 3)
            self.t._check_deal_presence()        # noqa: SLF001
            self.t._check_table_cleared()        # noqa: SLF001
            self.t._poll_departures()            # noqa: SLF001
            self.t._apply_idle_observations()    # noqa: SLF001
            self.t._check_foldout_timeout()      # noqa: SLF001

    def type_n(self) -> None:
        ev = AudioEvent(action="new_hand", amount=0, timestamp=self.now, raw_text="")
        self.recorder.record(ev)
        self.t._handle_audio_event(ev)           # noqa: SLF001

    def seat_card(self, seat: int, card: str) -> None:
        self.cards[seat] = [*self.cards.get(seat, []), card]
        self.absent_since.pop(seat, None)
        ev = RFIDEvent(tag_id=card, card=card, reader_id=f"r{seat}", role="seat", seat=seat,
                       timestamp=self.now, raw_tag_id=card)
        self.recorder.record(ev)
        self.t._process_rfid_event(ev)           # noqa: SLF001

    def board(self, index: int, card: str) -> None:
        ev = RFIDEvent(tag_id=card, card=card, reader_id="b", role="board", seat=None,
                       timestamp=self.now, raw_tag_id=card, board_index=index)
        self.recorder.record(ev)
        self.t._process_rfid_event(ev)           # noqa: SLF001

    def play(self, wash=()) -> None:
        """`n` → （ウォッシュ）→ 配布 → ボード → 札の離脱。"""
        self.type_n()
        steps = [(at, "wash", (i, card)) for i, (at, card) in enumerate(wash, start=1)]
        steps += [(at, "hole", (seat, card)) for at, seat, card in HOLES]
        steps += [(at, "board", (i, card)) for i, (at, card) in enumerate(BOARD, start=1)]
        steps += [(at, "lift", (seat,)) for at, seat in LIFTS]
        for at, kind, arg in sorted(steps, key=lambda s: s[0]):
            self.tick(at)
            if kind == "hole":
                self.seat_card(*arg)
            elif kind in ("board", "wash"):
                self.board(*arg)
            else:
                self.lift(*arg)
        self.tick(100.0)


class TestStoreSession:
    def test_the_empty_table_before_the_deal_is_not_the_end_of_play(self, tmp_path):
        tb = _ManualTable(tmp_path)
        tb.type_n()
        tb.tick(TABLE_CLEAR_SEC + 10)
        assert tb.t._in_play and tb.gate.is_set()   # noqa: SLF001

    def test_the_hand_is_recorded_from_the_board_and_the_cards(self, tmp_path):
        tb = _ManualTable(tmp_path)
        tb.play()
        assert tb.t._board_cards == ["Tc", "7c", "5d", "4h", "3h"]   # noqa: SLF001
        played = tb.played()
        assert ("flop", 4, "fold", 0) in played                          # 席4 はフロップで降りた
        assert [a for a in played if a[2] == "fold" and a[1] in (5, 6)] == []
        tb.cards = {}
        tb.deal({4: ["As", "Ad"], 5: ["Ks", "Kd"], 6: ["Qs", "Qh"]})
        (hand,) = tb.hands
        assert (hand.winner_seat, hand.winner_source) == (5, "cards")   # 3〜7 のストレート
        assert hand.board == ["Tc", "7c", "5d", "4h", "3h"]

    def test_the_gate_stays_open_until_the_table_is_cleared(self, tmp_path):
        tb = _ManualTable(tmp_path)
        closed: list[float] = []
        set_in_play = tb.t._set_in_play          # noqa: SLF001

        def spy(in_play: bool) -> None:
            if not in_play:
                closed.append(tb.now)
            set_in_play(in_play)

        tb.t._set_in_play = spy                  # noqa: SLF001
        tb.play()
        assert all(at >= LIFTS[-1][0] + TABLE_CLEAR_SEC for at in closed)   # 片付け（最後の離脱 + 15 秒）のあとだけ


class TestWashBeforeTheDeal:
    def test_board_cards_before_the_first_hole_card_are_ignored(self, tmp_path):
        tb = _ManualTable(tmp_path)
        tb.type_n()
        resets = tb.resets                       # `n` で 1 回
        tb.tick(2.0)
        for index, card in enumerate(["8h", "4d", "6h"], start=1):
            tb.board(index, card)
        assert tb.t._board_positions == {} and tb.played() == []   # noqa: SLF001
        tb.tick(5.0)
        assert tb.played() == []                 # ボードの札でストリートを進めない（全員のフォールドにしない）
        tb.seat_card(4, "Jd")
        assert tb.resets == resets + 1           # RFID のボードの位置を捨てた（本物の flop を 1 枚目から）
        tb.seat_card(4, "2s")
        assert tb.resets == resets + 1           # 1 回だけ

    def test_the_real_board_is_read_after_the_wash(self, tmp_path):
        tb = _ManualTable(tmp_path)
        tb.play(wash=[(2.0, "8h"), (2.3, "4d"), (2.6, "6h")])
        assert tb.t._board_cards == ["Tc", "7c", "5d", "4h", "3h"]   # noqa: SLF001
        assert ("flop", 4, "fold", 0) in tb.played()

    def test_no_reset_without_a_wash(self, tmp_path):
        tb = _ManualTable(tmp_path)
        tb.type_n()
        resets = tb.resets
        tb.tick(3.0)
        tb.seat_card(4, "Jd")
        assert tb.resets == resets

    def test_without_seat_readers_the_board_is_read(self, tmp_path):
        # 席のリーダーが無い構成（手札が届かない）では、ボードを待たせない
        tb = _ManualTable(tmp_path)
        tb.t._seat_presence = lambda: {}         # noqa: SLF001
        tb.type_n()
        tb.tick(2.0)
        tb.board(1, "Tc")
        assert tb.t._board_positions == {1: "Tc"}   # noqa: SLF001


class TestTableClearedAfterPlay:
    def test_the_gate_closes_after_a_played_hand_is_cleared(self, tmp_path):
        # 何か起きたハンド（ボードがある）の片付けは、これまでどおりプレーの終わり
        tb = _ManualTable(tmp_path)
        tb.type_n()
        tb.tick(1.0)
        for at, seat, card in HOLES:
            tb.tick(at)
            tb.seat_card(seat, card)
        tb.tick(30.0)
        for index, card in enumerate(["Tc", "7c", "5d"], start=1):
            tb.board(index, card)
        tb.tick(31.0)
        board = {"present_count": 0}
        tb.t._board_presence = lambda: board    # noqa: SLF001
        for seat in (4, 5, 6):
            tb.lift(seat)
        tb.tick(31.0 + TABLE_CLEAR_SEC + 1)
        assert not tb.t._in_play and not tb.gate.is_set()   # noqa: SLF001
