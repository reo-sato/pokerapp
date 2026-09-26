"""tests/test_rfid_table_flow.py

ADR-0058: 卓の流れに合わせた RFID の解釈（本番の起動経路 = main.py で有効）。

店舗の実卓で分かった 2 つの前提を、入力なしで扱う:

- **フォールドした手札は卓の中央へ押し出され、ボードのリーダーの上を通る** → そのハンドで席に
  記録した札はボードの札にしない（その席の「マック」として記録する）。記録していない札も、
  ボードは載り続けてから確定するので一瞬の通過では位置を取らない。
- **ミスディールの配り直しで、ディーラーに入力を求められない** → 前の札が `release_sec` 以上
  見えず、新しい札が載り続けたら差し替える。一瞬の読み落ち（ISSUE-0026）では差し替わらない。
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

import pytest

from core.event_queue import make_audio_queue, make_rfid_queue
from core.events import RFIDEvent
from core.game_state import GameStateManager, PlayerState
from core.table_state import build_table_state
from integration.engine import IntegrationThread
from integration.replay import event_from_envelope
from output.event_recorder import event_to_envelope
from output.json_writer import JsonWriter
from rfid.card_master import CardMaster
from rfid.reader_thread import RFIDThread

ROOT = Path(__file__).parent.parent

CARDS = {
    # 席 1 / 2 / 3 の手札
    "04:A1": "As", "04:A2": "Kd", "04:B1": "Qh", "04:B2": "Jc", "04:C1": "9s", "04:C2": "8s",
    # ボード（flop / turn / river）
    "04:F1": "5d", "04:F2": "Tc", "04:F3": "2h", "04:D1": "7c", "04:D2": "6h",
    # 配り直しの新しい札（UID は実機と同じく 16 進）
    "04:E1": "Ah", "04:E2": "Ad", "04:E3": "Ac",
}


class _Bridge:
    def __init__(self) -> None:
        self.uids: list[str] = []

    def connect(self) -> bool:
        return True

    def read_uids(self) -> list[str]:
        return list(self.uids)

    def close(self) -> None:
        pass


class Table:
    """席 3 つ + ボード 3 台（左 BL / 中 BM / 右 BR）の卓を、時計を手で進めながら回す決定的ドライバ。"""

    CONFIGS = [
        {"name": "S1", "role": "seat", "seat": 1},
        {"name": "S2", "role": "seat", "seat": 2},
        {"name": "S3", "role": "seat", "seat": 3},
        {"name": "BL", "role": "board"},
        {"name": "BM", "role": "board"},
        {"name": "BR", "role": "board"},
    ]

    def __init__(self, tmp_path: Path, *, commit: float = 2.0, gap: float = 1.5,
                 release: float | None = 6.0, window: float | None = 30.0,
                 confirm: float | None = 3.0) -> None:
        cm = CardMaster(tmp_path / "cards.json")
        for uid, card in CARDS.items():
            cm.register(uid, card)
        self.bridges = {cfg["name"]: _Bridge() for cfg in self.CONFIGS}
        self.now = 1000.0
        self.queue = make_rfid_queue()
        self.thread = RFIDThread(
            rfid_queue=self.queue, card_master=cm, reader_configs=self.CONFIGS,
            stop_event=threading.Event(),
            bridge_factory=lambda name, reader=0: self.bridges[name],
            clock=lambda: self.now,
            commit_sec=commit, gap_sec=gap, release_sec=release, redeal_window_sec=window,
            redeal_confirm_sec=confirm,
        )

    def put(self, reader: str, *uids: str) -> None:
        self.bridges[reader].uids = list(uids)

    def run(self, seconds: float, step: float = 0.3) -> list[RFIDEvent]:
        """`seconds` 秒ぶん全リーダーを poll し、その間に出た event を返す。"""
        events: list[RFIDEvent] = []
        end = self.now + seconds
        while self.now < end - 1e-9:
            self.now = round(self.now + step, 6)
            for i, cfg in enumerate(self.CONFIGS):
                self.thread._poll_reader(self.bridges[cfg["name"]], cfg, f"reader_{i}")  # noqa: SLF001
            while not self.queue.empty():
                events.append(self.queue.get_nowait())
        return events

    def mucked_at(self, seat: int):
        return self.thread.presence_snapshot()[seat]["mucked_at"]


def _board(events: list[RFIDEvent]) -> list[tuple]:
    return [(e.card, e.board_index, e.replaces) for e in events if e.role == "board"]


class TestFoldedHandsOverTheBoard:
    def test_folded_hand_passing_over_the_board_is_not_a_board_card(self, tmp_path: Path):
        t = Table(tmp_path)
        t.put("S1", "04:A1", "04:A2")
        t.put("S2", "04:B1", "04:B2")
        dealt = t.run(0.3)
        assert [(e.seat, e.card) for e in dealt] == [(1, "As"), (1, "Kd"), (2, "Qh"), (2, "Jc")]

        # 席 1 がフォールド: 手札が席から消え、左のボードのリーダーの上を 0.6 秒で通過する
        t.put("S1")
        t.put("BL", "04:A1", "04:A2")
        assert t.run(0.6) == []
        t.put("BL")
        assert t.run(5.0) == []
        assert t.mucked_at(1) is not None          # 卓モニタに「マック」と出る
        assert t.mucked_at(2) is None

        # flop は 1..3、turn は 4（通過した手札が位置を取っていない）
        t.put("BL", "04:F1")
        t.put("BR", "04:F2", "04:F3")
        assert _board(t.run(2.4)) == [("5d", 1, None), ("Tc", 2, None), ("2h", 3, None)]
        t.put("BR", "04:F2", "04:F3", "04:D1")
        assert _board(t.run(2.4)) == [("7c", 4, None)]

    def test_hole_card_resting_on_a_board_reader_never_becomes_a_board_card(self, tmp_path: Path):
        """押し出された手札がボードのリーダーの上で止まっても、ボードの札にはしない。"""
        t = Table(tmp_path)
        t.put("S3", "04:C1", "04:C2")
        t.run(0.3)
        t.put("S3")
        t.put("BL", "04:C1", "04:C2")
        assert t.run(10.0) == []
        assert t.mucked_at(3) is not None

    def test_unknown_card_passing_quickly_does_not_take_a_position(self, tmp_path: Path):
        """席で読めなかった札（手に持っていた等）も、通過しただけでは位置を取らない。"""
        t = Table(tmp_path)
        t.put("BR", "04:A1")               # 席では読んでいない札が 0.6 秒通過
        assert t.run(0.6) == []
        t.put("BR")
        assert t.run(5.0) == []
        t.put("BL", "04:F1")
        assert _board(t.run(2.4)) == [("5d", 1, None)]

    def test_hole_card_filter_also_applies_in_the_immediate_mode(self, tmp_path: Path):
        """従来の解釈（最初に見えた瞬間に確定）でも、手札はボードの札にしない。"""
        t = Table(tmp_path, commit=0.0, gap=0.0, release=None)
        t.put("S1", "04:A1", "04:A2")
        t.run(0.3)
        t.put("BL", "04:A1")
        assert t.run(0.3) == []
        assert t.mucked_at(1) is not None
        t.put("BL", "04:A1", "04:F1")
        assert _board(t.run(0.3)) == [("5d", 1, None)]


class TestBoardDwell:
    def test_board_card_counts_after_it_stays_and_keeps_its_first_seen_time(self, tmp_path: Path):
        t = Table(tmp_path)
        t.put("BL", "04:F1")
        assert t.run(1.5) == []                   # まだ確定しない
        events = t.run(1.0)
        assert _board(events) == [("5d", 1, None)]
        assert events[0].timestamp == pytest.approx(1000.3)   # 最初に見えた時刻（ADR-0055）

    def test_intermittent_reads_commit_once(self, tmp_path: Path):
        """結合の弱い台で 0.9 秒読み落ちても（gap 1.5 秒以内）、1 回だけ確定し再発火しない。"""
        t = Table(tmp_path)
        events = []
        for present, seconds in ((True, 0.6), (False, 0.9), (True, 0.9), (False, 0.6), (True, 1.2)):
            t.put("BL", *(["04:F1"] if present else []))
            events += t.run(seconds)
        assert _board(events) == [("5d", 1, None)]

    def test_flop_card_dropout_does_not_give_its_slot_to_the_turn(self, tmp_path: Path):
        """左のリーダーの flop の 1 枚が 7 秒読めなくても、turn は右のリーダーに置かれたので 4 枚目。"""
        t = Table(tmp_path)
        t.put("BL", "04:F1", "04:F2")
        t.put("BR", "04:F3")
        t.run(2.4)
        t.put("BL", "04:F1")                      # 2 枚目が読めなくなる
        t.run(7.0)
        t.put("BR", "04:F3", "04:D1")
        assert _board(t.run(2.4)) == [("7c", 4, None)]
        t.put("BL", "04:F1", "04:F2")             # 読めるようになれば元の 2 枚目で再発火
        assert _board(t.run(0.3)) == [("Tc", 2, None)]


class TestRedealWithoutInput:
    def test_flop_redeal_replaces_the_positions(self, tmp_path: Path):
        t = Table(tmp_path)
        t.put("BL", "04:F1", "04:F2")
        t.put("BR", "04:F3")
        t.run(2.4)
        t.put("BL")                               # 早すぎた flop を回収
        t.put("BR")
        t.run(7.0)
        t.put("BL", "04:E1", "04:E2")
        t.put("BR", "04:E3")
        assert _board(t.run(2.4)) == [("Ah", 1, "5d"), ("Ad", 2, "Tc"), ("Ac", 3, "2h")]
        t.put("BR", "04:E3", "04:D1")
        assert _board(t.run(2.4)) == [("7c", 4, None)]

    def test_hole_card_redeal_replaces_both_cards(self, tmp_path: Path):
        t = Table(tmp_path)
        t.put("S1", "04:A1", "04:A2")
        t.run(0.3)
        t.put("S1")                               # ミスディールで回収
        t.run(7.0)
        t.put("S1", "04:E1", "04:E2")
        assert t.run(1.5) == []                   # 新しい札が載り続けるまで待つ
        events = t.run(1.0)
        assert {e.card for e in events} == {"Ah", "Ad"}
        assert {e.replaces for e in events} == {"As", "Kd"}
        assert all(e.seat == 1 for e in events)

    def test_peeking_at_cards_does_not_replace_them(self, tmp_path: Path):
        t = Table(tmp_path)
        t.put("S1", "04:A1", "04:A2")
        t.run(0.3)
        t.put("S1")                               # 持ち上げて見ている
        t.run(10.0)
        t.put("S1", "04:A1", "04:A2")
        events = t.run(2.4)
        assert sorted(e.card for e in events) == ["As", "Kd"]   # 同じ札の再発火だけ
        assert all(e.replaces is None for e in events)

    def test_third_card_waits_until_an_old_card_is_gone(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ):
        t = Table(tmp_path)
        t.put("S1", "04:A1", "04:A2")
        t.run(0.3)
        t.put("S1", "04:A1", "04:A2", "04:E1")
        with caplog.at_level(logging.WARNING, logger="rfid.reader_thread"):
            assert t.run(10.0) == []              # 前の 2 枚が載っている間は記録しない
        assert sum("3 枚目" in r.getMessage() for r in caplog.records) == 1
        t.put("S1", "04:A1", "04:E1")             # Kd が回収された
        events = t.run(7.0)
        assert [(e.card, e.replaces) for e in events] == [("Ah", "Kd")]

    def test_redeal_can_move_a_card_to_another_seat(self, tmp_path: Path):
        t = Table(tmp_path)
        t.put("S1", "04:A1", "04:A2")
        t.put("S2", "04:B1", "04:B2")
        t.run(0.3)
        t.put("S1")                               # 全員の札を回収して配り直す
        t.put("S2")
        t.run(7.0)
        t.put("S1", "04:E2", "04:E3")
        t.put("S2", "04:A1", "04:E1")             # 前は席 1 だった As が席 2 に来る
        events = t.run(2.4)
        by_seat = {seat: {e.card for e in events if e.seat == seat} for seat in (1, 2)}
        assert by_seat == {1: {"Ad", "Ac"}, 2: {"As", "Ah"}}
        assert {e.replaces for e in events if e.seat == 2} == {"Qh", "Jc"}

    def test_board_card_on_a_seat_reader_is_not_a_hole_card(self, tmp_path: Path):
        t = Table(tmp_path)
        t.put("BL", "04:F1")
        t.run(2.4)
        t.put("BL")
        t.put("S2", "04:F1")
        assert t.run(10.0) == []

    def test_release_none_keeps_explicit_corrections_only(self, tmp_path: Path):
        """`release_sec=None` では自動の差し替えをしない（明示の cs / cb だけ）。"""
        t = Table(tmp_path, release=None)
        t.put("S1", "04:A1", "04:A2")
        t.run(0.3)
        t.put("S1")
        t.run(10.0)
        t.put("S1", "04:E1", "04:E2")
        assert t.run(5.0) == []


def _deal_flop(t: Table) -> list[RFIDEvent]:
    """flop = 左のリーダーに 5d・Tc、右のリーダーに 2h（1..3 で確定させる）。"""
    t.put("BL", "04:F1", "04:F2")
    t.put("BR", "04:F3")
    events = t.run(2.4)
    assert _board(events) == [("5d", 1, None), ("Tc", 2, None), ("2h", 3, None)]
    return events


class TestSingleBoardCardRedeal:
    """ボードは 1 枚だけ差し直すことがある（flop 全体を外すとは限らない）。"""

    def test_one_flop_card_swapped_in_place_keeps_its_position(self, tmp_path: Path):
        t = Table(tmp_path)
        _deal_flop(t)
        t.put("BL", "04:F1")                      # Tc だけ取る
        t.run(1.0)
        t.put("BL", "04:F1", "04:E1")             # 同じ場所に新しい札
        placed_at = t.now + 0.3
        events = t.run(2.4)                       # 新しい札が確定した時点で Tc は 3 秒以上消えている
        assert _board(events) == [("Ah", 2, "Tc")]
        assert events[0].timestamp == pytest.approx(placed_at)   # 置いた時刻（ADR-0055）
        t.put("BR", "04:F3", "04:D1")
        assert _board(t.run(2.4)) == [("7c", 4, None)]            # turn は 4 枚目のまま

    def test_quick_swap_waits_until_the_old_card_is_gone_3_seconds(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ):
        """取ってすぐ置くと、前の札が 3 秒見えないのを確かめてから差し替える（戻れば読み落ち）。"""
        t = Table(tmp_path)
        _deal_flop(t)
        t.put("BL", "04:F1", "04:E1")             # Tc を取り、同じ動きで Ah を置く
        with caplog.at_level(logging.INFO, logger="rfid.reader_thread"):
            assert t.run(2.4) == []
            events = t.run(1.5)
        assert _board(events) == [("Ah", 2, "Tc")]
        assert sum("差し直しか確かめています" in r.getMessage() for r in caplog.records) == 1

    def test_new_card_put_down_just_before_the_old_one_is_taken(self, tmp_path: Path):
        """新しい札を先に置いて、すぐ前の札を取っても差し直しになる（確定を待つ間に取れば）。"""
        t = Table(tmp_path)
        _deal_flop(t)
        t.put("BL", "04:F1", "04:F2", "04:E1")
        t.run(0.9)
        t.put("BL", "04:F1", "04:E1")
        assert _board(t.run(8.0)) == [("Ah", 2, "Tc")]

    def test_turn_card_swapped_in_place(self, tmp_path: Path):
        t = Table(tmp_path)
        _deal_flop(t)
        t.put("BR", "04:F3", "04:D1")
        assert _board(t.run(2.4)) == [("7c", 4, None)]
        t.put("BR", "04:F3")
        t.run(1.0)
        t.put("BR", "04:F3", "04:E1")
        assert _board(t.run(2.4)) == [("Ah", 4, "7c")]
        t.put("BR", "04:F3", "04:E1", "04:D2")
        assert _board(t.run(2.4)) == [("6h", 5, None)]

    def test_turn_swapped_onto_the_neighbouring_reader(self, tmp_path: Path):
        """turn の位置がリーダーの境目にあり、置き直した札を隣の台が読んでも差し直しになる（店舗の実卓）。"""
        t = Table(tmp_path)
        _deal_flop(t)
        t.put("BM", "04:D1")                      # turn を中のリーダーが読んだ
        assert _board(t.run(2.4)) == [("7c", 4, None)]
        t.put("BM")
        t.run(1.0)
        t.put("BR", "04:F3", "04:E1")             # 置き直した札は右のリーダーが読んだ
        assert _board(t.run(2.4)) == [("Ah", 4, "7c")]

    def test_turn_swapped_after_a_long_pause(self, tmp_path: Path):
        """最後に配った turn は、外してから時間が空いても差し直しになる（早すぎた turn の配り直し）。"""
        t = Table(tmp_path)
        dealt = _deal_flop(t)
        t.put("BR", "04:F3", "04:D1")
        dealt += t.run(2.4)
        t.put("BR", "04:F3")
        t.run(45.0)
        t.put("BR", "04:F3", "04:E1")
        turn_at = t.now + 0.3
        swapped = t.run(2.4)
        assert _board(swapped) == [("Ah", 4, "7c")]
        t.put("BR", "04:F3", "04:E1", "04:D2")
        river = t.run(2.4)
        assert _board(river) == [("6h", 5, None)]

        engine = _engine(tmp_path)
        for ev in [*dealt, *swapped, *river]:
            engine._process_rfid_event(ev)                        # noqa: SLF001
        assert engine._board_cards == ["5d", "Tc", "2h", "Ah", "6h"]  # noqa: SLF001
        assert engine._board_dealt_at[4] == pytest.approx(turn_at)    # noqa: SLF001
        assert engine._hand_needs_review is True                      # noqa: SLF001

    def test_flop_card_needs_the_same_reader(self, tmp_path: Path):
        """flop の札は前の札と同じリーダーの上に置き直したときだけ差し直し（隣の台なら次の位置）。"""
        t = Table(tmp_path)
        _deal_flop(t)
        t.put("BL", "04:F1")
        t.run(1.0)
        t.put("BM", "04:E1")
        assert _board(t.run(2.4)) == [("Ah", 4, None)]

    def test_last_card_on_a_far_reader_is_not_a_swap(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ):
        """隣でもない台に置かれた札は、最後の札が消えていても次の位置。ログに理由が分かる形で残す。"""
        t = Table(tmp_path)
        _deal_flop(t)
        t.put("BL", "04:F1", "04:F2", "04:D1")    # turn を左のリーダーが読んだ
        assert _board(t.run(2.4)) == [("7c", 4, None)]
        t.put("BL", "04:F1", "04:F2")
        t.run(1.0)
        t.put("BR", "04:F3", "04:E1")
        with caplog.at_level(logging.INFO, logger="rfid.reader_thread"):
            assert _board(t.run(2.4)) == [("Ah", 5, None)]
        msg = next(r.getMessage() for r in caplog.records if "5 枚目にしました" in r.getMessage())
        assert "左から 3 台目" in msg
        assert "7c（4 枚目・左から 1 台目・" in msg

    def test_card_that_dropped_out_before_is_not_swapped_but_fixed_by_the_sixth_card(
        self, tmp_path: Path,
    ):
        """一度読めなくなって戻った札（読みにくい位置）は、消えても差し直しとはみなさない。本当に
        差し直していたら新しい札は次の位置に入り、6 枚目（river）が来た時点で抜いて詰める。"""
        t = Table(tmp_path)
        dealt = _deal_flop(t)
        t.put("BM", "04:D1")
        dealt += t.run(2.4)
        t.put("BM")
        t.run(2.1)                                # 2 秒ほど読めなかった
        t.put("BM", "04:D1")
        t.run(0.6)
        t.put("BM")                               # 取った
        t.run(1.0)
        t.put("BM", "04:E1")
        appended = t.run(8.0)
        assert _board(appended) == [("Ah", 5, None)]
        t.put("BR", "04:F3", "04:D2")             # river = 6 枚目
        river = t.run(2.4)
        assert _board(river) == [("6h", 5, "Ah"), ("Ah", 4, "7c")]

        engine = _engine(tmp_path)
        for ev in [*dealt, *appended, *river]:
            engine._process_rfid_event(ev)                        # noqa: SLF001
        assert engine._board_cards == ["5d", "Tc", "2h", "Ah", "6h"]  # noqa: SLF001

    def test_card_that_dropped_out_before_is_not_swapped_from_the_next_reader(self, tmp_path: Path):
        t = Table(tmp_path)
        _deal_flop(t)
        t.put("BM", "04:D1")
        t.run(2.4)
        t.put("BM")
        t.run(2.1)
        t.put("BM", "04:D1")
        t.run(0.6)
        t.put("BM")
        t.run(1.0)
        t.put("BR", "04:F3", "04:E1")
        assert _board(t.run(8.0)) == [("Ah", 5, None)]

    def test_dropout_that_comes_back_was_not_a_swap(self, tmp_path: Path):
        """同じリーダーの札が読めない間に turn が来ても、札が戻れば turn は 4 枚目。"""
        t = Table(tmp_path)
        _deal_flop(t)
        t.put("BL", "04:F1")                      # Tc が読めなくなる（取ってはいない）
        t.run(0.3)
        t.put("BL", "04:F1", "04:D1")             # turn が同じリーダーの上に置かれた
        assert t.run(2.4) == []                   # 差し直しかもしれないので待つ
        t.put("BL", "04:F1", "04:F2", "04:D1")    # Tc がまた読めた
        events = t.run(0.3)
        assert sorted(_board(events), key=lambda e: e[1]) == [("Tc", 2, None), ("7c", 4, None)]

    def test_card_unread_since_long_before_is_not_swapped(self, tmp_path: Path):
        """ずっと前から読めていない flop の札（時間窓の外）は、同じリーダーでも差し直しにしない。"""
        t = Table(tmp_path)
        _deal_flop(t)
        t.put("BL", "04:F1")
        t.run(40.0)
        t.put("BL", "04:F1", "04:D1")
        assert _board(t.run(2.4)) == [("7c", 4, None)]           # 待たずに 4 枚目

    def test_whole_flop_redealt_after_a_long_pause(self, tmp_path: Path):
        """早すぎた flop を戻し、ベッティングの後で配り直した（時間窓の外でも flop を差し替える）。"""
        t = Table(tmp_path)
        _deal_flop(t)
        t.put("BL")
        t.put("BR")
        t.run(40.0)
        t.put("BL", "04:E1", "04:E2")
        t.put("BR", "04:E3")
        assert _board(t.run(2.4)) == [("Ah", 1, "5d"), ("Ad", 2, "Tc"), ("Ac", 3, "2h")]

    def test_flop_card_redealt_after_a_long_pause_is_fixed_by_the_sixth_card(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ):
        """時間窓の外で flop の 1 枚を差し直すと次の位置に入るが、6 枚目が来た時点で、5d が消えたあとに
        置いた Ah を 5d の位置へ移し、後ろを詰める（flop の札の集合・turn・river が正しくなる）。"""
        t = Table(tmp_path)
        dealt = _deal_flop(t)
        t.put("BL", "04:F2")                      # 5d を外し、しばらくしてから Ah を置く
        t.run(40.0)
        t.put("BL", "04:F2", "04:E1")
        first = t.run(2.4)
        assert _board(first) == [("Ah", 4, None)]         # この時点では読み落ちと区別できない
        t.put("BR", "04:F3", "04:D1")
        second = t.run(2.4)
        assert _board(second) == [("7c", 5, None)]
        t.put("BR", "04:F3", "04:D1", "04:D2")    # river = 6 枚目
        third = t.run(2.4)
        assert _board(third) == [("6h", 5, "7c"), ("7c", 4, "Ah"), ("Ah", 1, "5d")]  # 後ろの位置から

        engine = _engine(tmp_path)
        with caplog.at_level(logging.WARNING, logger="integration.engine"):
            for ev in [*dealt, *first, *second, *third]:
                engine._process_rfid_event(ev)                        # noqa: SLF001
        assert engine._board_cards == ["Ah", "Tc", "2h", "7c", "6h"]  # noqa: SLF001
        assert engine._hand_needs_review is True                      # noqa: SLF001
        assert not any("複数あります" in r.getMessage() for r in caplog.records)

    def test_turn_swapped_after_the_river_goes_in_its_place(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ):
        """店舗の実卓（2026-09-25 13:46:54）: river まで配ったあとで turn を差し直した。新しい札は turn の
        位置に入る（後ろへ入れて river を前へ詰めると turn と river が逆になる）。turn は途中で読めなく
        なったことがあり、1 枚の差し直しの対象外 = 5 枚埋まったボードの規則で決まる。"""
        t = Table(tmp_path)
        dealt = _deal_flop(t)
        t.put("BR", "04:F3", "04:D1")             # turn 7c
        dealt += t.run(2.4)
        t.put("BR", "04:F3")
        t.run(2.1)                                # 7c がいったん読めなくなった（読みにくい位置）
        t.put("BR", "04:F3", "04:D1")
        t.run(0.6)
        t.put("BR", "04:F3", "04:D1", "04:D2")    # river 6h（turn は載ったまま）
        dealt += t.run(2.4)
        t.put("BR", "04:F3", "04:D2")             # turn を取り、少ししてから新しい turn を置く
        t.run(5.0)
        t.put("BR", "04:F3", "04:D2", "04:E1")
        with caplog.at_level(logging.INFO, logger="rfid.reader_thread"):
            swapped = t.run(2.4)
        assert _board(swapped) == [("Ah", 4, "7c")]
        assert any("4 枚目を差し替えます" in r.getMessage() for r in caplog.records)

        engine = _engine(tmp_path)
        for ev in [*dealt, *swapped]:
            engine._process_rfid_event(ev)                        # noqa: SLF001
        assert engine._board_cards == ["5d", "Tc", "2h", "Ah", "6h"]  # noqa: SLF001

    def test_quick_swap_on_a_full_board_waits_instead_of_overflowing(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ):
        """5 枚埋まったボードで取ってすぐ置くと、前の札が取り除かれたと言えるまで待つ（「差し直し確認中」）。
        以前は 6 枚目として位置なしで記録し、二度と位置を与えなかった。"""
        t = Table(tmp_path)
        _deal_flop(t)
        t.put("BR", "04:F3", "04:D1")
        t.run(2.4)
        t.put("BR", "04:F3")
        t.run(2.1)
        t.put("BR", "04:F3", "04:D1")
        t.run(0.6)
        t.put("BR", "04:F3", "04:D1", "04:D2")
        t.run(2.4)
        t.put("BR", "04:F3", "04:D2")             # turn を取り
        t.run(1.0)
        t.put("BR", "04:F3", "04:D2", "04:E1")    # すぐ新しい turn を置いた
        with caplog.at_level(logging.INFO, logger="rfid.reader_thread"):
            assert t.run(2.4) == []
        assert t.thread.board_presence()["pending"]["Ah"]["swap"] is True
        assert sum("取り除かれたか確かめています" in r.getMessage() for r in caplog.records) == 1
        assert not any("5 枚を超えました" in r.getMessage() for r in caplog.records)
        assert _board(t.run(3.0)) == [("Ah", 4, "7c")]

    def test_river_swapped_in_is_not_mistaken_for_the_turns_replacement(self, tmp_path: Path):
        """turn を取ったあとに river を差し直し、最後に新しい turn を置いた。差し直しで入った river は
        turn の代わりではない（turn が消えたあとに置かれていても）ので、新しい turn は turn の位置。"""
        t = Table(tmp_path)
        _deal_flop(t)
        t.put("BR", "04:F3", "04:D1")
        t.run(2.4)
        t.put("BR", "04:F3")
        t.run(2.1)                                # 7c は読めなくなったことがある
        t.put("BR", "04:F3", "04:D1")
        t.run(0.6)
        t.put("BR", "04:F3", "04:D1", "04:D2")
        t.run(2.4)
        t.put("BR", "04:F3")                      # turn と river を取り
        t.run(1.0)
        t.put("BR", "04:F3", "04:E2")             # river を Ad に差し直す
        assert _board(t.run(4.0)) == [("Ad", 5, "6h")]
        t.put("BR", "04:F3", "04:E2", "04:E1")    # 新しい turn Ah
        assert _board(t.run(6.0)) == [("Ah", 4, "7c")]

    def test_flop_card_redealt_on_a_far_reader_after_the_turn(self, tmp_path: Path):
        """turn のあとで flop の 1 枚を離れたリーダーの上へ差し直すと次の位置（5 枚目）に入る。river が
        来た時点で、それを flop の位置へ移し、river を 5 枚目にする（turn は 4 枚目のまま）。"""
        t = Table(tmp_path)
        dealt = _deal_flop(t)
        t.put("BR", "04:F3", "04:D1")             # turn 7c
        dealt += t.run(2.4)
        t.put("BL", "04:F1")                      # flop の Tc を外し
        t.run(1.0)
        t.put("BR", "04:F3", "04:D1", "04:E1")    # 右端のリーダーの上に Ah を置いた
        appended = t.run(2.4)
        assert _board(appended) == [("Ah", 5, None)]
        t.run(3.0)
        t.put("BR", "04:F3", "04:D1", "04:E1", "04:D2")   # river
        river = t.run(2.4)
        assert _board(river) == [("6h", 5, "Ah"), ("Ah", 2, "Tc")]

        engine = _engine(tmp_path)
        for ev in [*dealt, *appended, *river]:
            engine._process_rfid_event(ev)                        # noqa: SLF001
        assert engine._board_cards == ["5d", "Ah", "2h", "7c", "6h"]  # noqa: SLF001

    def test_stray_card_on_a_full_board_waits_then_overflows_when_the_card_is_back(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ):
        """5 枚埋まったボードの札（読みにくい位置 = 1 枚の差し直しの対象外）がたまたま読めていない間に
        無関係な札（回収した札など）が載ったら、その札が戻るまで待ち、戻ったら 6 枚目として WARN（差し替えない）。"""
        t = Table(tmp_path)
        _deal_flop(t)
        t.put("BR", "04:F3", "04:D1")
        t.run(2.4)
        t.put("BR", "04:F3")
        t.run(2.1)                                # turn は読めなくなったことがある
        t.put("BR", "04:F3", "04:D1", "04:D2")
        t.run(2.4)
        t.put("BR", "04:F3", "04:D2")             # turn がまた読めなくなった（載ったまま）
        t.run(1.0)
        t.put("BR", "04:F3", "04:D2", "04:E1")    # 無関係な札が載った
        assert t.run(2.4) == []
        t.put("BR", "04:F3", "04:D1", "04:D2", "04:E1")   # turn がまた読めた
        with caplog.at_level(logging.WARNING, logger="rfid.reader_thread"):
            events = t.run(0.9)
        assert ("Ah", None, None) in _board(events)
        assert any("5 枚を超えました" in r.getMessage() for r in caplog.records)

    def test_sixth_card_without_a_missing_card_is_an_overflow(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ):
        t = Table(tmp_path)
        _deal_flop(t)
        t.put("BR", "04:F3", "04:D1", "04:D2")
        t.run(2.4)
        t.put("BR", "04:F3", "04:D1", "04:D2", "04:E1")
        with caplog.at_level(logging.WARNING, logger="rfid.reader_thread"):
            assert _board(t.run(2.4)) == [("Ah", None, None)]
        assert any("5 枚を超えました" in r.getMessage() for r in caplog.records)

    def test_sixth_card_with_two_missing_cards_is_an_overflow(self, tmp_path: Path):
        """消えた札が 2 枚以上だと、どれを抜くか決められないので詰めない。"""
        t = Table(tmp_path)
        _deal_flop(t)
        t.put("BR", "04:F3", "04:D1", "04:D2")
        t.run(2.4)
        t.put("BL")                               # 5d と Tc が消えた
        t.run(40.0)
        t.put("BR", "04:F3", "04:D1", "04:D2", "04:E1")
        assert _board(t.run(2.4)) == [("Ah", None, None)]

    def test_window_none_turns_off_single_card_swaps(self, tmp_path: Path):
        t = Table(tmp_path, window=None)
        _deal_flop(t)
        t.put("BL", "04:F1")
        t.run(1.0)
        t.put("BL", "04:F1", "04:E1")
        assert _board(t.run(8.0)) == [("Ah", 4, None)]

    def test_dropout_under_the_next_card_is_undone_when_the_card_comes_back(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ):
        """同じリーダーの札が初めて読めなくなった直後にそこへ次の札が置かれ、3 秒以上戻らないと
        差し直しと区別できない（差し替えとして記録する）。札が戻ってきて次の札も載っていれば、
        取り除いていなかったのだから差し替えを取り消す。"""
        t = Table(tmp_path)
        dealt = _deal_flop(t)
        t.put("BL", "04:F1", "04:D1")             # Tc が読めなくなった所に turn
        swapped = t.run(8.0)
        assert _board(swapped) == [("7c", 2, "Tc")]
        t.put("BL", "04:F1", "04:F2", "04:D1")    # Tc がまた読めた
        with caplog.at_level(logging.INFO, logger="rfid.reader_thread"):
            restored = t.run(2.4)
        assert _board(restored) == [("Tc", 2, "7c"), ("7c", 4, None)]
        assert any(r.getMessage().startswith("差し替えを取り消しました: Tc が 2 枚目に戻りました")
                   for r in caplog.records)

        engine = _engine(tmp_path)
        with caplog.at_level(logging.WARNING, logger="integration.engine"):
            for ev in [*dealt, *swapped, *restored]:
                engine._process_rfid_event(ev)                    # noqa: SLF001
        assert engine._board_cards == ["5d", "Tc", "2h", "7c"]    # noqa: SLF001
        assert engine._board_dealt_at[2] == dealt[1].timestamp    # noqa: SLF001  Tc を配った時刻
        assert engine._board_dealt_at[4] == swapped[0].timestamp  # noqa: SLF001  turn を置いた時刻
        assert not any("複数あります" in r.getMessage() for r in caplog.records)

    def test_turn_that_is_hard_to_read_is_not_swapped_by_the_river(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ):
        """店舗の実卓（2026-09-24 23:09〜23:10）: turn を 9s → Ts に差し直した。Ts はリーダーの境目で
        途切れながら読め、river の Js を置いたときも読めていなかった。Js が Ts を差し替えて、戻った
        Ts が 5 枚目になり turn と river が逆になった。途切れたことのある Ts は差し直しの対象にしない。"""
        t = Table(tmp_path)
        dealt = _deal_flop(t)
        t.put("BM", "04:D1")                      # 9s（= 7c）を中と右のリーダーが読む
        t.put("BR", "04:F3", "04:D1")
        dealt += t.run(2.4)
        assert _board(dealt)[-1] == ("7c", 4, None)
        t.put("BM")                               # 9s を取り
        t.put("BR", "04:F3")
        t.run(1.2)
        t.put("BR", "04:F3", "04:E1")             # Ts（= Ah）を置いたが
        t.run(0.3)
        t.put("BR", "04:F3")                      # 載ったまま読めなくなった
        t.run(12.6)
        t.put("BR", "04:F3", "04:E1")             # 12.7 秒後にまた読めた
        swapped = t.run(2.4)
        assert _board(swapped) == [("Ah", 4, "7c")]
        t.run(4.5)
        t.put("BR", "04:F3")                      # Ts がまた読めなくなった（15 秒）
        t.run(7.0)
        t.put("BR", "04:F3", "04:D2")             # river の Js（= 6h）
        with caplog.at_level(logging.INFO, logger="rfid.reader_thread"):
            river = t.run(2.4)
        assert _board(river) == [("6h", 5, None)]
        msg = next(r.getMessage() for r in caplog.records if "5 枚目にしました" in r.getMessage())
        assert "Ah（4 枚目・左から 3 台目・" in msg and "読み直し 1 回）" in msg
        t.put("BR", "04:F3", "04:E1", "04:D2")    # Ts がまた読めた
        back = t.run(2.4)
        assert set(_board(back)) == {("Ah", 4, None)}

        engine = _engine(tmp_path)
        for ev in [*dealt, *swapped, *river, *back]:
            engine._process_rfid_event(ev)                        # noqa: SLF001
        assert engine._board_cards == ["5d", "Tc", "2h", "Ah", "6h"]  # noqa: SLF001

    def test_river_that_replaced_an_unread_turn_is_undone(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ):
        """turn が確定後に初めて読めなくなった所へ river を置くと、差し直しと区別できない。turn が
        戻ってきて river も載っていれば取り消す（turn = 4 枚目、river = 5 枚目。時刻もそれぞれ）。"""
        t = Table(tmp_path)
        dealt = _deal_flop(t)
        t.put("BR", "04:F3", "04:D1")
        turn = t.run(2.4)
        assert _board(turn) == [("7c", 4, None)]
        t.put("BR", "04:F3")                      # turn が読めなくなった（載ったまま）
        t.run(1.0)
        t.put("BR", "04:F3", "04:D2")             # river
        swapped = t.run(4.0)
        assert _board(swapped) == [("6h", 4, "7c")]
        t.put("BR", "04:F3", "04:D1", "04:D2")    # turn がまた読めた
        restored = t.run(2.4)
        assert _board(restored) == [("7c", 4, "6h"), ("6h", 5, None)]

        engine = _engine(tmp_path)
        with caplog.at_level(logging.WARNING, logger="integration.engine"):
            for ev in [*dealt, *turn, *swapped, *restored]:
                engine._process_rfid_event(ev)                    # noqa: SLF001
        assert engine._board_cards == ["5d", "Tc", "2h", "7c", "6h"]  # noqa: SLF001
        assert engine._board_dealt_at[4] == turn[0].timestamp     # noqa: SLF001
        assert engine._board_dealt_at[5] == swapped[0].timestamp  # noqa: SLF001
        assert engine._hand_needs_review is True                  # noqa: SLF001
        assert not any("複数あります" in r.getMessage() for r in caplog.records)

    def test_swapping_back_to_the_first_card_is_a_swap_not_an_undo(self, tmp_path: Path):
        """差し直した札を取り、最初の札を戻した（差し替えた札は卓に無い）= 通常の差し直し。"""
        t = Table(tmp_path)
        _deal_flop(t)
        t.put("BR", "04:F3", "04:D1")
        t.run(2.4)
        t.put("BR", "04:F3")
        t.run(1.0)
        t.put("BR", "04:F3", "04:E1")
        assert _board(t.run(4.0)) == [("Ah", 4, "7c")]
        t.put("BR", "04:F3")
        t.run(1.0)
        t.put("BR", "04:F3", "04:D1")
        assert _board(t.run(4.0)) == [("7c", 4, "Ah")]

    def test_swapped_card_turning_up_on_a_far_reader_is_not_an_undo(self, tmp_path: Path):
        """戻ってきた札が元の場所から離れたリーダーで読めたら、動かした札 = 取り消さない。"""
        t = Table(tmp_path)
        _deal_flop(t)
        t.put("BL", "04:F1", "04:D1")             # Tc が読めなくなった所に turn
        assert _board(t.run(8.0)) == [("7c", 2, "Tc")]
        t.put("BR", "04:F3", "04:F2")             # Tc が右端のリーダーに現れた
        assert _board(t.run(2.4)) == [("Tc", 4, None)]

    def test_swapped_card_put_back_after_the_next_street_is_not_reordered(self, tmp_path: Path):
        """差し替えた札の後に別の札を配っていたら、戻ってきた札で並べ直さない（回収した札をボードの
        上に置いた、などと区別できない）。新しい札として扱う（従来どおり）。"""
        t = Table(tmp_path)
        _deal_flop(t)
        t.put("BL", "04:F1")                      # Tc を取り
        t.run(1.0)
        t.put("BL", "04:F1", "04:E1")             # 同じ場所に Ah
        assert _board(t.run(2.4)) == [("Ah", 2, "Tc")]
        t.put("BR", "04:F3", "04:D1")             # turn
        assert _board(t.run(2.4)) == [("7c", 4, None)]
        t.put("BM", "04:F2")                      # 回収した Tc をボードの上に置いた
        assert _board(t.run(2.4)) == [("Tc", 5, None)]

    def test_board_presence_lists_cards_taken_off_the_board(self, tmp_path: Path):
        """卓モニタの「外れた」表示用。一瞬の読み落ちは出さず、差し替わった札は消える。"""
        t = Table(tmp_path)
        _deal_flop(t)
        assert t.thread.board_presence()["absent"] == {}
        t.put("BL", "04:F1")
        t.run(0.9)
        assert t.thread.board_presence()["absent"] == {}         # gap 以内は出さない
        t.run(1.2)
        assert set(t.thread.board_presence()["absent"]) == {"Tc"}
        t.put("BL", "04:F1", "04:E1")
        t.run(2.4)
        assert t.thread.board_presence()["absent"] == {}         # 差し替わった

    def test_board_presence_lists_cards_being_confirmed(self, tmp_path: Path):
        """置いた札が読めていれば、数える前から「確認中」として見える（差し直しなら swap）。"""
        t = Table(tmp_path)
        t.put("BL", "04:F1")
        t.run(0.9)
        pending = t.thread.board_presence()["pending"]
        assert set(pending) == {"5d"} and pending["5d"]["swap"] is False
        assert pending["5d"]["since"] == pytest.approx(1000.3)
        t.run(1.5)
        assert t.thread.board_presence()["pending"] == {}        # 数えた
        t.put("BL", "04:F1", "04:F2", "04:F3")
        t.run(2.4)
        t.put("BL", "04:F1", "04:E1", "04:F3")    # Tc を取ってすぐ Ah
        t.run(2.4)                                # Ah は Tc が 3 秒消えるのを待っている
        assert t.thread.board_presence()["pending"]["Ah"]["swap"] is True
        t.run(1.5)
        assert t.thread.board_presence()["pending"] == {}

    def test_card_read_on_and_off_still_counts(self, tmp_path: Path):
        """リーダーの境目などで 2 秒ずつ途切れながら読める札も、数え直しを繰り返さずに数える。"""
        t = Table(tmp_path)
        events = []
        for present, seconds in ((True, 0.6), (False, 2.1), (True, 0.6), (False, 2.1), (True, 0.6)):
            t.put("BR", *(["04:D1"] if present else []))
            events += t.run(seconds)
        # 確定は 1 回（1 枚目）。確定後の途切れは同じ位置での再発火になる（engine は無視する）。
        assert set(_board(events)) == {("7c", 1, None)}
        assert _board(events)[0] == ("7c", 1, None)

    def test_log_says_when_a_board_card_was_first_read(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ):
        """置いてから読めるまでの遅れを切り分けられるよう、見え始めた時刻と途切れを INFO で残す。"""
        t = Table(tmp_path)
        with caplog.at_level(logging.INFO, logger="rfid.reader_thread"):
            t.put("BR", "04:D1")
            t.run(0.6)
            t.put("BR")
            t.run(3.3)                            # 3 秒を超えて読めなかった
            t.put("BR", "04:D1")
            t.run(0.3)
        messages = [r.getMessage() for r in caplog.records]
        assert "ボードに札 7c が載りました（左から 3 台目）" in messages
        # 他の board reader は 1 poll 遅れて札を失うので、最後に見えたのは外した次の poll（3.3 秒前）
        assert any(m.startswith("ボードの札 7c を読み直しました（3.3 秒読めなかった") for m in messages)

    def test_main_wires_the_production_defaults(self):
        import main
        kwargs = main._rfid_tracking_kwargs({})                                 # noqa: SLF001
        assert kwargs["redeal_window_sec"] == 30.0
        assert kwargs["redeal_confirm_sec"] == 3.0
        assert main._rfid_tracking_kwargs(                                      # noqa: SLF001
            {"redeal_window_sec": None})["redeal_window_sec"] is None


class TestManualCorrectionsAndNewHand:
    def test_explicit_board_correction_frees_the_slot(self, tmp_path: Path):
        t = Table(tmp_path)
        t.put("BL", "04:F1", "04:F2", "04:F3")
        t.run(2.4)
        t.put("BL", "04:F1", "04:F3")
        t.thread.forget_board_position(2)
        t.put("BL", "04:F1", "04:E1", "04:F3")
        assert _board(t.run(2.4)) == [("Ah", 2, None)]

    def test_explicit_seat_correction_rereads_at_once(self, tmp_path: Path):
        t = Table(tmp_path)
        t.put("S1", "04:A1", "04:A2")
        t.run(0.3)
        t.thread.forget_seat_cards(1)
        t.put("S1", "04:E1", "04:E2")
        events = t.run(0.3)
        assert sorted(e.card for e in events) == ["Ad", "Ah"]
        assert all(e.replaces is None for e in events)

    def test_new_hand_forgets_owners_and_muck(self, tmp_path: Path):
        t = Table(tmp_path)
        t.put("S1", "04:A1", "04:A2")
        t.run(0.3)
        t.put("S1")
        t.put("BL", "04:A1")
        t.run(0.6)
        t.put("BL")
        t.run(2.0)
        assert t.mucked_at(1) is not None
        t.thread.reset_for_new_hand()
        assert t.mucked_at(1) is None
        t.put("BL", "04:A1")                      # 前のハンドの手札が次のハンドのボードに来てもよい
        assert _board(t.run(2.4)) == [("As", 1, None)]

    def test_new_hand_forgets_rereads_and_swaps(self, tmp_path: Path):
        """「途中で読めなくなった札」「差し替えた札」はハンドごと。次のハンドの判断に持ち越さない。"""
        t = Table(tmp_path)
        _deal_flop(t)
        t.put("BR", "04:F3", "04:D1")
        t.run(2.4)
        t.put("BR", "04:F3")
        t.run(2.1)                                # 7c が途切れた（このハンドでは差し直しの対象外）
        t.put("BR", "04:F3", "04:D1")
        t.run(0.6)
        t.put("BL")
        t.put("BR")
        t.thread.reset_for_new_hand()
        _deal_flop(t)
        t.put("BR", "04:F3", "04:D1")
        t.run(2.4)
        t.put("BR", "04:F3")
        t.run(1.0)
        t.put("BR", "04:F3", "04:E1")
        assert _board(t.run(4.0)) == [("Ah", 4, "7c")]     # 新しいハンドでは差し直しになる


# ――― engine 側（差し替え / 席の移動 / ボード位置の差し替え）―――


def _engine(tmp_path: Path) -> IntegrationThread:
    gs = GameStateManager(
        [PlayerState(seat=i + 1, name=f"P{i + 1}", stack=100) for i in range(3)], sb=1, bb=2
    )
    return IntegrationThread(
        audio_queue=make_audio_queue(), game_state=gs,
        json_writer=JsonWriter(tmp_path, "flow"), stop_event=threading.Event(),
    )


def _seat(t: IntegrationThread, seat: int, card: str, replaces: str | None = None,
          ts: float = 100.0) -> None:
    t._process_rfid_event(RFIDEvent(  # noqa: SLF001
        tag_id=card, card=card, reader_id=f"seat_{seat}", role="seat", seat=seat,
        timestamp=ts, raw_tag_id=card, replaces=replaces,
    ))


def _board_ev(t: IntegrationThread, index: int, card: str, ts: float) -> None:
    t._process_rfid_event(RFIDEvent(  # noqa: SLF001
        tag_id=card, card=card, reader_id="board_0", role="board", seat=None,
        timestamp=ts, raw_tag_id=card, board_index=index,
    ))


class TestEngine:
    def test_replacement_swaps_the_hole_card_and_marks_review(self, tmp_path: Path):
        t = _engine(tmp_path)
        _seat(t, 1, "As")
        _seat(t, 1, "Kd")
        assert t._hand_needs_review is False                  # noqa: SLF001
        _seat(t, 1, "Ah", replaces="Kd")
        assert t._hole_cards[1] == ["As", "Ah"]               # noqa: SLF001
        assert t._hand_needs_review is True                   # noqa: SLF001

    def test_moved_card_leaves_its_old_seat(self, tmp_path: Path):
        t = _engine(tmp_path)
        for seat, card in ((1, "As"), (1, "Kd"), (2, "Qh"), (2, "Jc")):
            _seat(t, seat, card)
        _seat(t, 2, "As", replaces="Jc")
        assert t._hole_cards[1] == ["Kd"]                     # noqa: SLF001
        assert t._hole_cards[2] == ["Qh", "As"]               # noqa: SLF001
        assert t._hand_needs_review is True                   # noqa: SLF001

    def test_board_replacement_takes_the_new_cards_time(self, tmp_path: Path):
        t = _engine(tmp_path)
        _board_ev(t, 1, "5d", ts=100.0)
        _board_ev(t, 1, "5d", ts=150.0)                       # 同じ札の再発火は時刻を変えない
        assert t._board_dealt_at[1] == 100.0                  # noqa: SLF001
        assert t._hand_needs_review is False                  # noqa: SLF001
        _board_ev(t, 1, "Ah", ts=200.0)                       # 配り直しで別の札
        assert t._board_cards == ["Ah"]                       # noqa: SLF001
        assert t._board_dealt_at[1] == 200.0                  # noqa: SLF001
        assert t._hand_needs_review is True                   # noqa: SLF001


# ――― 記録（events.jsonl）と卓状態 ―――


class TestRecordingAndTableState:
    def test_replaces_round_trips_and_is_omitted_when_absent(self):
        ev = RFIDEvent(tag_id="04:E1", card="Ah", reader_id="reader_0", role="seat", seat=1,
                       timestamp=1.0, raw_tag_id="04:E1", replaces="Kd")
        env = event_to_envelope(ev)
        assert env["replaces"] == "Kd"
        assert event_from_envelope(env).replaces == "Kd"
        plain = event_to_envelope(RFIDEvent(
            tag_id="04:A1", card="As", reader_id="reader_0", role="seat", seat=1,
            timestamp=1.0, raw_tag_id="04:A1",
        ))
        assert "replaces" not in plain                        # 旧 replay・golden と同じ形

    def test_envelope_with_replaces_matches_the_schema(self):
        jsonschema = pytest.importorskip("jsonschema")
        schema = json.loads(
            (ROOT / "docs/contracts/schemas/reconstruction_event.schema.json").read_text(encoding="utf-8")
        )
        assert schema["version"] == "0.6"
        env = event_to_envelope(RFIDEvent(
            tag_id="04:E1", card="Ah", reader_id="reader_3", role="board", seat=None,
            timestamp=1.0, raw_tag_id="04:E1", board_index=1, replaces="5d",
        ))
        jsonschema.validate(env, schema)

    def test_table_state_shows_mucked_seats(self):
        state = build_table_state(
            session_id="s", hand_id=1, now=100.0, updated_at="t", seats=[1, 2],
            hole_cards={1: ["As", "Kd"], 2: ["Qh", "Jc"]},
            presence={
                1: {"present": False, "absent_since": 90.0, "mucked_at": 95.0},
                2: {"present": True, "absent_since": None, "mucked_at": 80.0},
            },
            board=[], board_timeline=[],
        )
        s1, s2 = state.seats
        assert s1.mucked is True
        assert s2.mucked is False                             # いま載っている席はマック表示にしない
        assert state.to_dict()["mucked_seats"] == [1]

    def test_monitor_text_says_muck(self):
        from tools.table_monitor import _format_text
        text = _format_text({
            "session_id": "s", "hand_id": 1, "updated_at": "t", "age_sec": 0.1,
            "rfid_street": "preflop", "engine_street": "preflop", "button_seat": None, "board": [],
            "seats": [{"seat": 1, "present": False, "mucked": True, "likely_folded": False,
                       "dealt_in": True, "away_sec": 3.0, "cards": ["As", "Kd"], "position": ""}],
        })
        assert "マック" in text

    def test_table_state_shows_board_cards_taken_off(self):
        state = build_table_state(
            session_id="s", hand_id=1, now=100.0, updated_at="t", seats=[1],
            hole_cards={}, presence={}, board=["5d", "Tc", "2h"], board_timeline=[],
            board_absent_since={"Tc": 96.0, "Ks": 90.0},     # Ks はボードに無い（差し替え済み）
        )
        assert state.board_away_sec == {"Tc": 4.0}
        assert state.to_dict()["board_away_sec"] == {"Tc": 4.0}
        assert state.board == ["5d", "Tc", "2h"]              # 記録は変えない（表示だけ）

    def test_monitor_text_says_taken_off(self):
        from tools.table_monitor import _format_text
        text = _format_text({
            "session_id": "s", "hand_id": 1, "updated_at": "t", "age_sec": 0.1,
            "rfid_street": "flop", "engine_street": "flop", "button_seat": None,
            "board": ["5d", "Tc", "2h"], "board_away_sec": {"Tc": 4.0}, "seats": [],
        })
        assert "5d Tc(外れた 4.0s) 2h" in text

    def test_engine_publishes_board_presence(self, tmp_path: Path):
        from output.table_state_writer import TableStateWriter

        gs = GameStateManager([PlayerState(seat=1, name="P1", stack=100)], sb=1, bb=2)
        writer = TableStateWriter(tmp_path, "bp")
        presence = {"absent": {"5d": 495.0}, "pending": {"Ah": {"since": 498.8, "swap": True}}}
        t = IntegrationThread(
            audio_queue=make_audio_queue(), game_state=gs,
            json_writer=JsonWriter(tmp_path, "bp"), stop_event=threading.Event(),
            clock=lambda: 500.0, table_state_writer=writer, board_presence=lambda: presence,
        )
        _board_ev(t, 1, "5d", ts=490.0)
        state = json.loads(writer.snapshot_path.read_text(encoding="utf-8"))
        assert state["board_away_sec"] == {"5d": 5.0}
        assert state["board_pending"] == [{"card": "Ah", "sec": 1.2, "swap": True}]

        def boom():
            raise RuntimeError("reader gone")

        t._board_presence = boom                              # noqa: SLF001
        _board_ev(t, 2, "Tc", ts=491.0)
        state = json.loads(writer.snapshot_path.read_text(encoding="utf-8"))
        assert state["board"] == ["5d", "Tc"] and state["board_away_sec"] == {}
        assert state["board_pending"] == []

    def test_table_state_lists_cards_being_confirmed(self):
        state = build_table_state(
            session_id="s", hand_id=1, now=100.0, updated_at="t", seats=[1],
            hole_cards={}, presence={}, board=["5d", "Tc", "2h"], board_timeline=[],
            board_pending={"Ah": {"since": 99.0, "swap": True}, "7c": {"since": 98.5, "swap": False},
                           "Tc": {"since": 99.5, "swap": False}},   # Tc は既にボードにある
        )
        assert state.to_dict()["board_pending"] == [
            {"card": "7c", "sec": 1.5, "swap": False}, {"card": "Ah", "sec": 1.0, "swap": True},
        ]

    def test_monitor_text_says_being_confirmed(self):
        from tools.table_monitor import _format_text
        text = _format_text({
            "session_id": "s", "hand_id": 1, "updated_at": "t", "age_sec": 0.1,
            "rfid_street": "flop", "engine_street": "flop", "button_seat": None,
            "board": ["5d"], "board_pending": [
                {"card": "Ac", "sec": 1.2, "swap": False}, {"card": "3s", "sec": 0.4, "swap": True},
            ], "seats": [],
        })
        assert "5d [Ac 確認中 1.2s] [3s 差し直し確認中]" in text
