"""tests/test_misdeal_correction.py

ADR-0043: ミスディールで一度読ませたカードを外し、正しいカードを読み直す経路。

ハンド内の board 位置は append-only（ISSUE-0026）で「見えなくなっただけでは解放しない」。
一瞬の読み落ちを載せ替えと誤解しないための安全弁なので、**ミスディールは明示コマンドで
解放する**（ディーラーが宣言する事象であって推測対象ではない）。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from core.event_queue import make_audio_queue, make_rfid_queue
from core.events import AudioEvent, RFIDEvent
from core.game_state import GameStateManager, PlayerState
from integration.engine import IntegrationThread
from output.json_writer import JsonWriter
from rfid.card_master import CardMaster
from rfid.reader_thread import RFIDThread


# ――― engine 側（記録の取り消し）―――


def _engine(tmp_path: Path, sid: str):
    gs = GameStateManager(
        [PlayerState(seat=i + 1, name=f"P{i + 1}", stack=100) for i in range(3)], sb=1, bb=2
    )
    corrections: list[tuple[str, int]] = []
    t = IntegrationThread(
        audio_queue=make_audio_queue(), game_state=gs,
        json_writer=JsonWriter(tmp_path, sid), stop_event=threading.Event(),
        on_card_correction=lambda kind, key: corrections.append((kind, key)),
    )
    return t, corrections


def _board(t, index: int, card: str, tag: str = "TAG") -> None:
    t._process_rfid_event(RFIDEvent(  # noqa: SLF001
        tag_id=tag, card=card, timestamp=time.time(), raw_tag_id=tag,
        reader_id="board_0", role="board", seat=None, board_index=index,
    ))


def _seat_card(t, seat: int, card: str, tag: str = "TAG") -> None:
    t._process_rfid_event(RFIDEvent(  # noqa: SLF001
        tag_id=tag, card=card, timestamp=time.time(), raw_tag_id=tag,
        reader_id=f"seat_{seat}", role="seat", seat=seat,
    ))


class TestBoardCorrection:
    def test_wrong_board_card_is_dropped_and_replaced(self, tmp_path: Path):
        t, _ = _engine(tmp_path, "mis1")
        _board(t, 1, "5d", "T1")
        _board(t, 2, "Tc", "T2")
        _board(t, 3, "9s", "T3")
        assert t._board_cards == ["5d", "Tc", "9s"]     # noqa: SLF001

        # 3 枚目がミスディール → 取り消し
        t._handle_audio_event(AudioEvent("correct_board", 3, time.time(), "ボード3 訂正"))  # noqa: SLF001
        assert t._board_cards == ["5d", "Tc"]           # noqa: SLF001

        # 正しいカードが同じ位置に入る（RFID 側が同じ位置を再割り当てする）
        _board(t, 3, "Kh", "T4")
        assert t._board_cards == ["5d", "Tc", "Kh"]     # noqa: SLF001

    def test_correction_marks_hand_for_review(self, tmp_path: Path):
        t, _ = _engine(tmp_path, "mis2")
        _board(t, 1, "5d", "T1")
        assert t._hand_needs_review is False            # noqa: SLF001
        t._handle_audio_event(AudioEvent("correct_board", 1, time.time(), "ボード1 訂正"))  # noqa: SLF001
        assert t._hand_needs_review is True             # noqa: SLF001

    def test_correction_notifies_rfid_hook(self, tmp_path: Path):
        t, corrections = _engine(tmp_path, "mis3")
        _board(t, 2, "Tc", "T2")
        t._handle_audio_event(AudioEvent("correct_board", 2, time.time(), "ボード2 訂正"))  # noqa: SLF001
        assert corrections == [("board", 2)]

    def test_unknown_position_is_a_noop(self, tmp_path: Path):
        t, corrections = _engine(tmp_path, "mis4")
        _board(t, 1, "5d", "T1")
        t._handle_audio_event(AudioEvent("correct_board", 4, time.time(), "ボード4 訂正"))  # noqa: SLF001
        assert t._board_cards == ["5d"]                 # noqa: SLF001
        assert corrections == []                        # 空振りは RFID 側も触らない
        assert t._hand_needs_review is False            # noqa: SLF001

    def test_middle_position_correction_keeps_the_others(self, tmp_path: Path):
        """2 枚目だけ差し替えても 1/3 枚目の位置は動かない（順序が壊れない）。"""
        t, _ = _engine(tmp_path, "mis5")
        _board(t, 1, "5d", "T1")
        _board(t, 2, "Tc", "T2")
        _board(t, 3, "9s", "T3")
        t._handle_audio_event(AudioEvent("correct_board", 2, time.time(), "ボード2 訂正"))  # noqa: SLF001
        assert t._board_cards == ["5d", "9s"]           # noqa: SLF001
        _board(t, 2, "Qh", "T4")
        assert t._board_cards == ["5d", "Qh", "9s"]     # noqa: SLF001


class TestSeatCorrection:
    def test_hole_cards_are_cleared_and_reread(self, tmp_path: Path):
        t, corrections = _engine(tmp_path, "mis6")
        _seat_card(t, 2, "Jd", "T1")
        _seat_card(t, 2, "Jh", "T2")
        assert t._hole_cards[2] == ["Jd", "Jh"]         # noqa: SLF001

        t._handle_audio_event(AudioEvent("correct_seat", 0, time.time(), "シート2 訂正", seat=2))  # noqa: SLF001
        assert 2 not in t._hole_cards                   # noqa: SLF001
        assert corrections == [("seat", 2)]
        assert t._hand_needs_review is True             # noqa: SLF001

        # 読み直した 2 枚が改めて記録される（上限 2 枚に引っかからない）
        _seat_card(t, 2, "As", "T3")
        _seat_card(t, 2, "Kc", "T4")
        assert t._hole_cards[2] == ["As", "Kc"]         # noqa: SLF001

    def test_other_seats_are_untouched(self, tmp_path: Path):
        t, _ = _engine(tmp_path, "mis7")
        _seat_card(t, 1, "2c", "T1")
        _seat_card(t, 2, "3c", "T2")
        t._handle_audio_event(AudioEvent("correct_seat", 0, time.time(), "シート2 訂正", seat=2))  # noqa: SLF001
        assert t._hole_cards[1] == ["2c"]               # noqa: SLF001

    def test_missing_seat_is_a_noop(self, tmp_path: Path):
        t, corrections = _engine(tmp_path, "mis8")
        t._handle_audio_event(AudioEvent("correct_seat", 0, time.time(), "訂正"))  # noqa: SLF001
        assert corrections == []


# ――― RFID 側（位置の解放 + 読み直し）―――


class _FakeBridge:
    def __init__(self, uids: list[str]) -> None:
        self.uids = uids

    def connect(self) -> bool:
        return True

    def read_uids(self) -> list[str]:
        return list(self.uids)

    def disconnect(self) -> None:
        pass


def _rfid_thread(tmp_path: Path, configs: list[dict], bridges: dict[str, _FakeBridge]):
    master = tmp_path / "cards.json"
    master.write_text('{"cards": {}}', encoding="utf-8")
    thread = RFIDThread(
        rfid_queue=make_rfid_queue(), card_master=CardMaster(str(master)),
        reader_configs=configs, stop_event=threading.Event(),
        bridge_factory=lambda name, reader=0: bridges[name],
    )
    return thread


class TestRfidForgetBoardPosition:
    def _with_board(self, tmp_path: Path):
        bridges = {"b0": _FakeBridge([])}
        t = _rfid_thread(tmp_path, [{"name": "b0", "role": "board"}], bridges)
        cfg = {"name": "b0", "role": "board"}
        bridges["b0"].uids = ["U1", "U2", "U3"]
        t._poll_reader(bridges["b0"], cfg, "reader_0")   # noqa: SLF001
        return t, bridges, cfg

    def test_positions_are_assigned_in_detection_order(self, tmp_path: Path):
        t, _, _ = self._with_board(tmp_path)
        assert t._board_indexes == {"U1": 1, "U2": 2, "U3": 3}   # noqa: SLF001

    def test_forget_frees_only_that_position(self, tmp_path: Path):
        t, _, _ = self._with_board(tmp_path)
        assert t.forget_board_position(3) == "U3"
        assert t._board_indexes == {"U1": 1, "U2": 2}            # noqa: SLF001
        # 次に現れた新しいカードが空いた 3 を取る
        assert t._assign_board_index("U9") == 3                  # noqa: SLF001

    def test_freed_middle_position_is_reused(self, tmp_path: Path):
        t, _, _ = self._with_board(tmp_path)
        t.forget_board_position(2)
        assert t._assign_board_index("U9") == 2                  # noqa: SLF001
        assert t._board_indexes["U1"] == 1                       # noqa: SLF001
        assert t._board_indexes["U3"] == 3                       # noqa: SLF001

    def test_forget_drops_debounce_so_present_cards_refire(self, tmp_path: Path):
        """解放と同時にデバウンスを落とすので、載っているカードは再発火して同じ位置に戻る。"""
        t, bridges, cfg = self._with_board(tmp_path)
        t.forget_board_position(3)
        bridges["b0"].uids = ["U1", "U2"]          # ミスディール札は物理的に外した
        t._poll_reader(bridges["b0"], cfg, "reader_0")   # noqa: SLF001
        assert t._board_indexes == {"U1": 1, "U2": 2}     # noqa: SLF001 — 位置は不動
        bridges["b0"].uids = ["U1", "U2", "U7"]    # 正しい札を置く
        t._poll_reader(bridges["b0"], cfg, "reader_0")   # noqa: SLF001
        assert t._board_indexes["U7"] == 3                # noqa: SLF001

    def test_forget_is_idempotent_if_card_not_removed_yet(self, tmp_path: Path):
        """外す前に打っても同じ札が同じ位置に戻るだけ = 外して再実行でやり直せる。"""
        t, bridges, cfg = self._with_board(tmp_path)
        t.forget_board_position(3)
        t._poll_reader(bridges["b0"], cfg, "reader_0")   # noqa: SLF001（まだ U3 が載っている）
        assert t._board_indexes["U3"] == 3                # noqa: SLF001
        t.forget_board_position(3)
        bridges["b0"].uids = ["U1", "U2"]
        t._poll_reader(bridges["b0"], cfg, "reader_0")   # noqa: SLF001
        assert "U3" not in t._board_indexes               # noqa: SLF001

    def test_unknown_position_returns_none(self, tmp_path: Path):
        t, _, _ = self._with_board(tmp_path)
        assert t.forget_board_position(5) is None
        assert t._board_indexes == {"U1": 1, "U2": 2, "U3": 3}   # noqa: SLF001


class TestRfidForgetSeatCards:
    def test_seat_reader_debounce_is_dropped(self, tmp_path: Path):
        bridges = {"s1": _FakeBridge(["A1", "A2"])}
        cfg = {"name": "s1", "role": "seat", "seat": 1}
        t = _rfid_thread(tmp_path, [cfg], bridges)
        t._poll_reader(bridges["s1"], cfg, "reader_0")   # noqa: SLF001
        assert t._last_uids["reader_0"] == {"A1", "A2"}   # noqa: SLF001

        t.forget_seat_cards(1)
        assert t._last_uids["reader_0"] == set()          # noqa: SLF001
        # 物理的に載っている札が改めて読み直される
        t._poll_reader(bridges["s1"], cfg, "reader_0")   # noqa: SLF001
        assert t._last_uids["reader_0"] == {"A1", "A2"}   # noqa: SLF001

    def test_unknown_seat_is_a_noop(self, tmp_path: Path):
        bridges = {"s1": _FakeBridge(["A1"])}
        cfg = {"name": "s1", "role": "seat", "seat": 1}
        t = _rfid_thread(tmp_path, [cfg], bridges)
        t._poll_reader(bridges["s1"], cfg, "reader_0")   # noqa: SLF001
        t.forget_seat_cards(9)                            # 存在しない席
        assert t._last_uids["reader_0"] == {"A1"}         # noqa: SLF001
