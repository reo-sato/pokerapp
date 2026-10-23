"""tests/test_button_rescue.py

ディーラーがボタンを動かし忘れたときの救済（店舗 2026-09-29 9d1d8536 ハンド 4 のメモ: この回に限ってディーラーが
ボタンを動かすのを忘れた。手札を読み込む時間が最速と最遅になっている連番の席を見つけて、そこがボタンになるように
修正する）。

- 配った順（`core.positions.button_from_deal`）: ディーラーはボタンの次の席から 1 枚ずつ 2 周配る。1 枚目と 2 枚目の
  両方の読み取り時刻を見て、別の席をボタンとした配り方にだけ食い違いなく合うときだけ直す（オーナー: 1 人で 3 人を
  演じたテストの読み取りの時刻は当てにならない = 合わなければ何もしない）。
- 直すときは、同じ持ち点・ブラインドでボタンだけ変えてハンドを始め直し、入力（声・札の離脱・ボードの札）を流し直す。
  「フォールド」と言われた席（spoken_fold）は流し直した時点の手番の席にする。
- 手で直す: `button <席>` はハンドの途中ならそのハンドを組み直す（ハンドの外なら従来どおり次のハンド）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core.events import AudioEvent, RFIDEvent
from core.game_state import PlayerState
from core.positions import DEAL_ORDER_TIE_SEC, button_from_deal, deal_order_fit

pytest.importorskip("pokerkit")

from integration.replay import replay_events  # noqa: E402
from tests.test_rfid_folds import HOLES, _Table  # noqa: E402


def _dealt(order: list[int], start: float = 0.0, gap: float = 0.6) -> dict[int, list[float]]:
    """`order` の席から 1 枚ずつ 2 周配ったときの読み取り時刻。"""
    times: dict[int, list[float]] = {}
    for k in range(2):
        for i, seat in enumerate(order):
            times.setdefault(seat, []).append(start + (k * len(order) + i) * gap)
    return times


class TestDealOrder:
    SEATS = [4, 5, 6]

    def test_the_button_whose_deal_matches_every_card(self):
        times = _dealt([6, 4, 5])                     # ボタン 席5 の配り方（SB 席6 から）
        assert deal_order_fit(self.SEATS, times, 5) == (12, 12)
        assert deal_order_fit(self.SEATS, times, 6)[0] < 12
        assert button_from_deal(self.SEATS, times, current=6) == 5
        assert button_from_deal(self.SEATS, times, current=5) is None   # いまのボタンのまま

    def test_first_and_second_cards_both_count(self):
        """1 枚目の順と 2 枚目の順が違う配り方（どのボタンにも合わない）は直さない。"""
        times = {6: [0.0, 3.0], 4: [0.6, 3.6], 5: [1.2, 1.8]}   # 席5 だけ 2 枚続けて
        assert button_from_deal(self.SEATS, times, current=6) is None

    def test_one_player_placing_all_hands_seat_by_seat(self):
        """店舗のテストは 1 人で 3 人を演じた（席ごとに 2 枚ずつ置いた）= 配った順ではない → 直さない。"""
        times = {5: [0.0, 0.42], 6: [1.38, 2.0], 4: [3.0, 3.5]}
        assert button_from_deal(self.SEATS, times, current=6) is None

    def test_one_card_read_late_is_not_enough(self):
        times = _dealt([6, 4, 5])
        times[4][0] = times[5][0] + 0.5              # 席4 の 1 枚目が席5 の 1 枚目より遅れて読めた
        assert button_from_deal(self.SEATS, times, current=6) is None

    def test_cards_read_in_the_same_sweep_are_not_compared(self):
        times = _dealt([6, 4, 5], gap=DEAL_ORDER_TIE_SEC / 3)   # 0.1 秒おき = 順が分かる組は 0.3 秒以上離れた 3 組だけ
        assert deal_order_fit(self.SEATS, times, 5) == (3, 3)
        assert button_from_deal(self.SEATS, times, current=6) is None     # 比べられる組が 2 × 席数 に足りない

    def test_missing_cards_still_count_when_enough_are_read(self):
        times = _dealt([6, 4, 5])
        del times[4][1]                               # 席4 の 2 枚目が読めなかった
        assert button_from_deal(self.SEATS, times, current=6) == 5

    def test_heads_up_deals_to_the_big_blind_first(self):
        """heads-up はボタンが SB、配るのはボタンでない方から。"""
        times = _dealt([5, 4])                        # 席5 から = ボタン 席4
        assert button_from_deal([4, 5], times, current=5) == 4
        assert button_from_deal([4, 5], times, current=4) is None

    def test_seats_out_of_the_hand_are_ignored(self):
        times = _dealt([6, 4, 5])
        times[7] = [9.0, 9.5]                         # ハンドに配られていない席の読み取り
        assert button_from_deal(self.SEATS, times, current=6) == 5
        assert button_from_deal(self.SEATS, times, current=9) is None      # いまのボタンが卓に無い


class _DealTable(_Table):
    """札を 1 枚ずつ置き、札ごとに最初に置いた時刻（在否の `since`）を返す卓。"""

    def __init__(self, tmp_path: Path, **kwargs) -> None:
        self.since: dict[int, dict[str, float]] = {}
        super().__init__(tmp_path, **kwargs)

    def put(self, seat: int, cards) -> None:
        super().put(seat, cards)
        seen = self.since.setdefault(seat, {})
        for card in cards:
            seen.setdefault(card, self.now)

    def _presence(self) -> dict:
        snapshot = super()._presence()
        for seat, info in snapshot.items():
            info["since"] = dict(self.since.get(seat, {}))
        return snapshot

    def tick(self, until: float, step: float = 0.1) -> None:
        while self.now < until - 1e-9:
            self.now = round(self.now + step, 3)
            self.t._check_deal_presence()        # noqa: SLF001
            self.t._check_deal_order()           # noqa: SLF001
            self.t._poll_departures()            # noqa: SLF001
            self.t._apply_idle_observations()    # noqa: SLF001
            self.t._check_foldout_timeout()      # noqa: SLF001

    def deal_in_order(self, order: list[int], holes=HOLES, gap: float = 0.6) -> None:
        """`order` の席から 1 枚ずつ 2 周配る（札を置くたびに時間を進める）。"""
        for k in range(2):
            for seat in order:
                card = holes[seat][k]
                self.put(seat, holes[seat][:k + 1])
                ev = RFIDEvent(tag_id=card, card=card, reader_id=f"r{seat}", role="seat", seat=seat,
                               timestamp=self.now, raw_tag_id=card)
                self.recorder.record(ev)
                self.t._process_rfid_event(ev)   # noqa: SLF001
                self.tick(self.now + gap)
        self.tick(self.now + 2.0)
        assert self.t._hand_open                # noqa: SLF001

    def button(self, seat: int) -> None:
        event = AudioEvent(action="set_button", amount=0, timestamp=self.now,
                           raw_text=f"シート{seat} ボタン", seat=seat)
        self.recorder.record(event)
        self.t._handle_audio_event(event)       # noqa: SLF001

    def end(self) -> None:
        """終了（`q`）: 確定していないハンドを保存する（記録に残る = replay も同じ時点で閉じる）。"""
        event = AudioEvent(action="session_end", amount=0, timestamp=self.now, raw_text="（終了）")
        self.recorder.record(event)
        self.t._handle_audio_event(event)       # noqa: SLF001


def _replayed(tb: _Table, tmp_path: Path):
    return replay_events(
        tb.recorder.events, backend="pokerkit",
        players=[PlayerState(seat=s, name=f"P{s}", stack=10000) for s in (4, 5, 6)],
        sb=100, bb=200, session_id="replay", out_dir=tmp_path / "replay",
        auto_new_hand=True, auto_winner=True, rfid_folds=True,
    )


class TestManualButton:
    def test_button_in_the_middle_of_a_hand_rebuilds_it(self, tmp_path):
        tb = _DealTable(tmp_path)
        tb.deal()                                    # 記録のボタン 席6（SB 4 / BB 5）
        assert tb.gs.button_seat == 6
        tb.say("コール")                              # 実際はボタン 席5: 席5 のコール
        tb.say("コール")                              # 席6（SB）
        tb.say("チェック")                            # 席4（BB）
        assert tb.played() == [("preflop", 6, "call", 200), ("preflop", 4, "call", 100),
                               ("preflop", 5, "check", 0)]
        tb.button(5)
        assert tb.gs.button_seat == 5
        assert tb.gs.position_map() == {6: "SB", 4: "BB", 5: "BTN"}
        assert tb.played() == [("preflop", 5, "call", 200), ("preflop", 6, "call", 100),
                               ("preflop", 4, "check", 0)]
        assert tb.gs.street == "flop" and tb.gs.pot == 600
        assert any("ボタンを席5 に直して" in n and "席4=BB 席5=BTN 席6=SB" in n for n in tb.notices)
        assert tb.t._hand_needs_review           # noqa: SLF001
        # 組み直した記録を画面にもう一度流す
        assert [(a.seat, a.action) for a in tb.actions[-3:]] == [(5, "call"), (6, "call"), (4, "check")]

    def test_the_next_hand_moves_on_from_the_corrected_button(self, tmp_path):
        tb = _DealTable(tmp_path)
        tb.deal()
        tb.button(5)
        tb.say("フォールド")                          # 席5
        tb.lift(5)
        tb.tick(tb.now + 3.5)
        tb.say("フォールド")                          # 席6（SB）→ 席4 の勝ち
        tb.lift(6)
        tb.tick(tb.now + 20.0)
        (hand,) = tb.hands
        assert hand.button_seat == 5 and hand.winner_seat == 4
        tb.cards = {}
        tb.deal({4: ["2c", "3c"], 5: ["2d", "3d"], 6: ["2h", "3h"]})
        assert tb.gs.button_seat == 6                # 席5 の次

    def test_the_same_button_and_seats_not_in_the_hand(self, tmp_path):
        tb = _DealTable(tmp_path)
        tb.deal()
        tb.say("コール")
        tb.button(6)
        assert any("このハンドのボタンは席6 です" in n for n in tb.notices)
        tb.button(9)
        assert any("席9 はこのハンドに配られていません" in n for n in tb.notices)
        assert tb.played() == [("preflop", 6, "call", 200)] and tb.gs.button_seat == 6

    def test_a_fold_word_waiting_for_the_cards_moves_to_the_new_actor(self, tmp_path):
        """「フォールド」と言われたが札が残っていた手番の席（spoken_fold）は、組み直した手番の席にする。"""
        tb = _DealTable(tmp_path)
        tb.deal()
        tb.say("フォールド")                          # 記録では席6 の番（実際は席5）。札はまだ席にある
        assert tb.played() == []
        tb.button(5)
        tb.lift(5)                                   # 席5 の札が離れた
        tb.tick(tb.now + 3.5)
        assert tb.played() == [("preflop", 5, "fold", 0)]

    def test_replay_matches_live(self, tmp_path):
        tb = _DealTable(tmp_path)
        tb.deal()
        tb.say("コール")
        tb.say("フォールド")
        tb.button(5)
        tb.lift(6)
        tb.tick(tb.now + 3.5)
        tb.say("チェック")
        tb.tick(tb.now + 1.0)
        live = tb.played()
        assert live == [("preflop", 5, "call", 200), ("preflop", 6, "fold", 0), ("preflop", 4, "check", 0)]
        tb.end()
        (hand,) = tb.hands
        (replayed,) = _replayed(tb, tmp_path)
        assert [(a.street, a.seat, a.action, a.amount) for a in replayed.actions] == [
            (a.street, a.seat, a.action, a.amount) for a in hand.actions]
        assert replayed.button_seat == hand.button_seat == 5

    def test_without_a_hand_the_button_is_for_the_next_hand(self, tmp_path):
        tb = _DealTable(tmp_path)
        tb.button(4)
        assert any("次のハンドのボタンを席4 にします" in n for n in tb.notices)


class TestButtonFromTheDeal:
    def test_the_deal_order_moves_the_button_before_any_action(self, tmp_path):
        tb = _DealTable(tmp_path)
        tb.deal_in_order([6, 4, 5])                  # ボタン 席5 の配り方（記録は席6 に進めた）
        assert tb.gs.button_seat == 5
        assert tb.gs.position_map() == {6: "SB", 4: "BB", 5: "BTN"}
        assert any("手札を読んだ順" in n and "ボタンを席5 に直して" in n for n in tb.notices)
        tb.say("コール")
        assert tb.played() == [("preflop", 5, "call", 200)]
        (signal,) = [e for e in tb.recorder.events if isinstance(e, RFIDEvent) and e.kind == "deal_order"]
        assert len(signal.cards) == len(signal.times) == 6

    def test_the_deal_from_the_recorded_button_changes_nothing(self, tmp_path):
        tb = _DealTable(tmp_path)
        tb.deal_in_order([4, 5, 6])                  # 記録どおり（ボタン 席6）
        assert tb.gs.button_seat == 6
        assert not any("ボタンを席" in n for n in tb.notices)

    def test_seat_by_seat_placing_changes_nothing(self, tmp_path):
        tb = _DealTable(tmp_path)
        for seat in (5, 6, 4):                       # 1 人で席ごとに 2 枚ずつ置いた
            for k in range(2):
                tb.put(seat, HOLES[seat][:k + 1])
                tb.tick(tb.now + 0.6)
        tb.tick(tb.now + 2.0)
        assert tb.t._hand_open and tb.gs.button_seat == 6   # noqa: SLF001

    def test_disabled_it_only_records(self, tmp_path):
        tb = _DealTable(tmp_path)
        tb.t._button_from_deal = False              # noqa: SLF001
        tb.deal_in_order([6, 4, 5])
        assert tb.gs.button_seat == 6
        assert any(isinstance(e, RFIDEvent) and e.kind == "deal_order" for e in tb.recorder.events)

    def test_all_cards_at_once_records_nothing(self, tmp_path):
        tb = _DealTable(tmp_path)
        tb.deal()
        assert not any(isinstance(e, RFIDEvent) and e.kind == "deal_order" for e in tb.recorder.events)

    def test_replay_reproduces_the_correction(self, tmp_path):
        tb = _DealTable(tmp_path)
        tb.deal_in_order([6, 4, 5])
        tb.say("コール")
        tb.say("フォールド")
        tb.lift(6)
        tb.tick(tb.now + 3.5)
        tb.say("チェック")
        tb.tick(tb.now + 1.0)
        tb.end()
        (hand,) = tb.hands
        assert hand.button_seat == 5 and hand.review_required
        (replayed,) = _replayed(tb, tmp_path)
        assert replayed.button_seat == 5
        assert [(a.street, a.seat, a.action, a.amount) for a in replayed.actions] == [
            (a.street, a.seat, a.action, a.amount) for a in hand.actions]

    def test_the_signal_matches_the_schema(self, tmp_path):
        import json

        jsonschema = pytest.importorskip("jsonschema")
        from output.event_recorder import event_to_envelope

        schema = json.loads((Path(__file__).resolve().parents[1]
                             / "docs/contracts/schemas/reconstruction_event.schema.json").read_text(encoding="utf-8"))
        tb = _DealTable(tmp_path)
        tb.deal_in_order([6, 4, 5])
        for event in tb.recorder.events:
            jsonschema.validate(event_to_envelope(event), schema)
