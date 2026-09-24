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

    def test_card_that_dropped_out_before_needs_the_same_reader_and_6_seconds(self, tmp_path: Path):
        """確定後に一度読めなくなって戻った札（読めにくい場所）は、消えても 6 秒待つ。"""
        t = Table(tmp_path)
        _deal_flop(t)
        t.put("BM", "04:D1")
        t.run(2.4)
        t.put("BM")
        t.run(2.1)                                # 2 秒ほど読めなかった
        t.put("BM", "04:D1")
        t.run(0.6)
        t.put("BM")                               # 取った
        t.run(1.0)
        t.put("BM", "04:E1")
        assert t.run(2.4) == []                   # 3 秒では差し替えない
        assert _board(t.run(4.0)) == [("Ah", 4, "7c")]

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
        """時間窓の外で flop の 1 枚を差し直すと次の位置に入るが、6 枚目が来た時点で抜いて詰める。"""
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
        assert _board(third) == [                 # 後ろの位置から送る
            ("6h", 5, "7c"), ("7c", 4, "Ah"), ("Ah", 3, "2h"), ("2h", 2, "Tc"), ("Tc", 1, "5d"),
        ]

        engine = _engine(tmp_path)
        with caplog.at_level(logging.WARNING, logger="integration.engine"):
            for ev in [*dealt, *first, *second, *third]:
                engine._process_rfid_event(ev)                        # noqa: SLF001
        assert engine._board_cards == ["Tc", "2h", "Ah", "7c", "6h"]  # noqa: SLF001
        assert engine._hand_needs_review is True                      # noqa: SLF001
        assert not any("複数あります" in r.getMessage() for r in caplog.records)

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

    def test_known_limit_dropout_under_the_next_card(self, tmp_path: Path):
        """既知の制約: 同じリーダーの札が読めなくなった直後にそこへ次の札が置かれ、3 秒以上戻らないと
        差し直しと区別できない。差し替えとして記録し（engine は needs_review）、札が戻れば次の位置に入る
        （札の集合と枚数は正しく、並びだけが入れ替わる）。"""
        t = Table(tmp_path)
        _deal_flop(t)
        t.put("BL", "04:F1", "04:D1")             # Tc が読めなくなった所に turn
        assert _board(t.run(8.0)) == [("7c", 2, "Tc")]
        t.put("BL", "04:F1", "04:F2", "04:D1")    # Tc がまた読めた
        assert _board(t.run(2.4)) == [("Tc", 4, None)]

    def test_board_presence_lists_cards_taken_off_the_board(self, tmp_path: Path):
        """卓モニタの「外れた」表示用。一瞬の読み落ちは出さず、差し替わった札は消える。"""
        t = Table(tmp_path)
        _deal_flop(t)
        assert t.thread.board_presence() == {}
        t.put("BL", "04:F1")
        t.run(0.9)
        assert t.thread.board_presence() == {}                   # gap 以内は出さない
        t.run(1.2)
        assert set(t.thread.board_presence()) == {"Tc"}
        t.put("BL", "04:F1", "04:E1")
        t.run(2.4)
        assert t.thread.board_presence() == {}                   # 差し替わった

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
        assert schema["version"] == "0.4"
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
        absent = {"5d": 495.0}
        t = IntegrationThread(
            audio_queue=make_audio_queue(), game_state=gs,
            json_writer=JsonWriter(tmp_path, "bp"), stop_event=threading.Event(),
            clock=lambda: 500.0, table_state_writer=writer, board_presence=lambda: absent,
        )
        _board_ev(t, 1, "5d", ts=490.0)
        state = json.loads(writer.snapshot_path.read_text(encoding="utf-8"))
        assert state["board_away_sec"] == {"5d": 5.0}

        def boom():
            raise RuntimeError("reader gone")

        t._board_presence = boom                              # noqa: SLF001
        _board_ev(t, 2, "Tc", ts=491.0)
        state = json.loads(writer.snapshot_path.read_text(encoding="utf-8"))
        assert state["board"] == ["5d", "Tc"] and state["board_away_sec"] == {}
