"""tests/test_seats_and_blinds.py

席の参加・休みとブラインドの変更（オーナーの回答 2026-09-26: 席は操作で決める / 離席は無い / ブラインドは
店舗の操作で上がる）:

- `name <席> -` = 休み（次のハンドから配られない）、`name <席> <名前>` = 参加。
- スタック 0 の席（バースト）は買い足す（`r`）まで配られない。pokerkit はスタック 0 を受け付けないので、
  そのまま配ると新しいハンドが始まらず、integration スレッドが止まっていた。
- ボタンはハンドに参加する席の中で回る（抜けた席は飛ばす）。
- 配られる席が 2 つ未満ならハンドを始めない（1 回だけ知らせる）。買い足し・参加で始まる。
- `blinds <SB> <BB>` は次のハンドから。ハンドの記録の blinds はそのハンドのもの。
"""
from __future__ import annotations

import pytest

from core.events import AudioEvent
from core.game_state import PlayerState

pytest.importorskip("pokerkit")

from core.poker_engine import PokerkitGameState  # noqa: E402
from tests.test_rfid_folds import _Table  # noqa: E402


def _engine(stacks: dict[int, int], button=None) -> PokerkitGameState:
    return PokerkitGameState(
        [PlayerState(seat=s, name=f"P{s}", stack=st) for s, st in stacks.items()],
        sb=100, bb=200, button_seat=button,
    )


class TestEngineSeats:
    def test_a_seat_sitting_out_is_not_dealt(self):
        gs = _engine({1: 10000, 2: 10000, 3: 10000})
        assert gs.sit_out(2) is True and gs.sit_out(2) is False
        gs.new_hand()
        assert gs.seats_in_hand() == [1, 3]
        assert set(gs.position_map()) == {1, 3} and set(gs.get_stacks()) == {1, 2, 3}
        assert gs.get_stacks()[2] == 10000            # スタックは持ち越し
        gs.end_hand(1)
        assert gs.sit_in(2) is True
        gs.new_hand()
        assert gs.seats_in_hand() == [1, 2, 3]

    def test_a_busted_seat_is_skipped_until_it_rebuys(self):
        gs = _engine({1: 10000, 2: 0, 3: 10000})
        gs.new_hand()
        assert gs.seats_in_hand() == [1, 3] and gs.playing_seats() == [1, 3]
        gs.end_hand(3)
        gs.rebuy(2, 5000)
        gs.new_hand()
        assert gs.seats_in_hand() == [1, 2, 3]

    def test_the_button_skips_a_seat_that_left(self):
        gs = _engine({1: 10000, 2: 10000, 3: 10000})
        buttons = []
        for step in (None, None, "out2", "swap"):
            if step == "out2":
                gs.sit_out(2)
            elif step == "swap":
                gs.sit_in(2)
                gs.sit_out(3)
            gs.new_hand()
            buttons.append(gs.button_seat)
            gs.end_hand(gs.seats_in_hand()[0])
        # 3（初回は最大の席）→ 1 → 3（席2 は休み）→ 1（ボタンの席3 が抜けた = その次の席）
        assert buttons == [3, 1, 3, 1]

    def test_fewer_than_two_seats_refuses_without_changing_state(self):
        gs = _engine({1: 10000, 2: 10000})
        gs.sit_out(2)
        with pytest.raises(ValueError):
            gs.new_hand()
        assert gs.hand_id == 0 and not gs.is_hand_active()
        gs.sit_in(2)
        gs.new_hand()
        assert gs.hand_id == 1

    def test_end_hand_rejects_a_seat_that_was_not_dealt(self):
        gs = _engine({1: 10000, 2: 10000, 3: 0})
        gs.new_hand()
        with pytest.raises(ValueError):
            gs.end_hand(3)
        gs.end_hand(1)


class TestEngineBlinds:
    def test_a_change_during_a_hand_applies_to_the_next(self):
        gs = _engine({1: 10000, 2: 10000, 3: 10000})
        gs.new_hand()
        gs.set_blinds(200, 400)
        assert gs.legal_context().bb == 200
        gs.end_hand(1)
        before = gs.get_stacks()
        gs.new_hand()
        assert gs.legal_context().bb == 400
        posted = sorted(before[s] - st for s, st in gs.get_stacks().items())
        assert posted == [0, 200, 400]              # SB 200 / BB 400 を post

    def test_a_change_between_hands_applies_at_once(self):
        gs = _engine({1: 10000, 2: 10000})
        gs.set_blinds(300, 600)
        gs.new_hand()
        assert gs.legal_context().bb == 600

    @pytest.mark.parametrize("sb, bb", [(0, 200), (400, 200), (-100, 200)])
    def test_invalid_blinds(self, sb, bb):
        gs = _engine({1: 10000, 2: 10000})
        with pytest.raises(ValueError):
            gs.set_blinds(sb, bb)


HOLES_A = {4: ["Jd", "2s"], 5: ["5s", "6h"], 6: ["8s", "Qd"]}
HOLES_B = {4: ["As", "Ad"], 5: ["Ks", "Kd"], 6: ["Qs", "Qh"]}


def _send(tb: _Table, **fields) -> None:
    event = AudioEvent(timestamp=tb.now, **{"amount": 0, "raw_text": "", **fields})
    tb.recorder.record(event)
    tb.t._handle_audio_event(event)          # noqa: SLF001


def _win(tb: _Table, seat: int) -> None:
    """アクションを 1 つ入れてから勝者を宣言する（配った直後の「ウィナー」は前のハンドへの遅れとみるため）。"""
    tb.say("コール")
    _send(tb, action="winner", raw_text=f"シート{seat} ウィナー")


class _LiveTable(_Table):
    """run() と同じく、配布を検出したハンドの開始を毎周試す卓。"""

    def tick(self, until: float, step: float = 0.1) -> None:
        while self.now < until - 1e-9:
            super().tick(self.now + step, step)
            self.t._start_dealt_hand_if_ready()  # noqa: SLF001


def _deal_without_assert(tb: _Table, holes) -> None:
    from core.events import RFIDEvent

    for seat, cards in holes.items():
        tb.put(seat, cards)
        for card in cards:
            ev = RFIDEvent(tag_id=card, card=card, reader_id=f"r{seat}", role="seat", seat=seat,
                           timestamp=tb.now, raw_tag_id=card)
            tb.recorder.record(ev)
            tb.t._process_rfid_event(ev)     # noqa: SLF001
    tb.tick(tb.now + 2.0)


class TestTable:
    def test_a_vacated_seat_is_not_dealt_from_the_next_hand(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal(HOLES_A)
        _send(tb, action="sit_out", seat=4, raw_text="シート4 休み")
        assert tb.t._seats_in_hand() == [4, 5, 6]          # noqa: SLF001 — いまのハンドはそのまま
        assert any("席4 は次のハンドから休み" in n for n in tb.notices)
        _win(tb, 5)
        tb.cards = {}
        tb.deal({5: HOLES_B[5], 6: HOLES_B[6]})
        assert tb.gs.seats_in_hand() == [5, 6]
        assert any("休み: 席4" in n for n in tb.notices)
        _win(tb, 6)
        assert [p["seat"] for p in tb.hands[1].players] == [5, 6]
        assert [p["seat"] for p in tb.hands[0].players] == [4, 5, 6]

    def test_a_named_seat_comes_back(self, tmp_path):
        tb = _Table(tmp_path)
        _send(tb, action="sit_out", seat=4, raw_text="シート4 休み")
        _send(tb, action="sit_in", seat=4, raw_text="シート4 参加")
        _send(tb, action="sit_in", seat=5, raw_text="シート5 参加")   # もともと参加 = 何も言わない
        assert sum("席4 は次のハンドから参加" in n for n in tb.notices) == 1
        assert not any("席5" in n for n in tb.notices)
        tb.deal(HOLES_A)
        assert tb.gs.seats_in_hand() == [4, 5, 6]

    def test_a_busted_seat_is_skipped(self, tmp_path):
        tb = _Table(tmp_path)
        tb.gs.update_stack(4, 0)
        tb.deal(HOLES_A)
        assert tb.gs.seats_in_hand() == [5, 6] and any("休み: 席4" in n for n in tb.notices)

    def test_the_deal_waits_until_two_seats_can_play(self, tmp_path):
        tb = _LiveTable(tmp_path)
        tb.gs.update_stack(4, 0)
        tb.gs.update_stack(5, 0)
        _deal_without_assert(tb, HOLES_A)
        tb.tick(tb.now + 5.0)
        assert not tb.t._hand_open                          # noqa: SLF001
        refusals = [n for n in tb.notices if "ハンドを始められません" in n]
        assert len(refusals) == 1 and "r <席> <金額>" in refusals[0]
        _send(tb, action="rebuy", seat=4, amount=5000, raw_text="シート4 リバイ 5000")
        assert any("席4 に 5000 を買い足しました" in n for n in tb.notices)
        tb.tick(tb.now + 1.0)
        assert tb.t._hand_open and tb.gs.seats_in_hand() == [4, 6]   # noqa: SLF001
        assert tb.t._hole_cards == {4: ["Jd", "2s"], 5: ["5s", "6h"], 6: ["8s", "Qd"]}  # noqa: SLF001

    def test_blinds_change_from_the_next_hand(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal(HOLES_A)
        _send(tb, action="set_blinds", amount=400, raw_text="200/400")
        assert any("ブラインドを 200/400 にしました（次のハンドから）" in n for n in tb.notices)
        _win(tb, 5)
        assert tb.hands[0].blinds == {"sb": 100, "bb": 200}
        tb.cards = {}
        tb.deal(HOLES_B)
        _send(tb, action="winner", raw_text="シート6 ウィナー")   # まだアクションが無い = 前のハンドへの遅れ
        assert len(tb.hands) == 1
        _win(tb, 6)
        assert tb.hands[1].blinds == {"sb": 200, "bb": 400}
        assert tb.hands[1].pot_total == 1000                # SB 200 + BB 400 + BTN のコール 400

    def test_invalid_blinds_are_reported(self, tmp_path):
        tb = _Table(tmp_path)
        _send(tb, action="set_blinds", amount=100, raw_text="400/100")
        assert any("ブラインドを変えられません" in n for n in tb.notices)
