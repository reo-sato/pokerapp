"""全員降りたことにして閉じたあとのボード（`board_after_end`）と、手札を続けて読めない席の知らせ（店舗 2026-10-26）。

10/26 の 2bbd480e: 席 7 をロガーに足せず、席 3 のリーダーが手札を読めなくなって、ハンド 32〜37 はボードの前に
「全員降りた」で閉じた。ボードのリーダーは読めていたが、閉じたあとの札はハンドに入らなかった。
"""
from __future__ import annotations

import json

import pytest

pytest.importorskip("pokerkit")

from core.events import RFIDEvent  # noqa: E402
from core.game_state import PlayerState  # noqa: E402
from core.hand_log import input_board  # noqa: E402
from integration.engine import FOLDOUT_CONFIRM_SEC, UNREAD_SEAT_HANDS  # noqa: E402
from integration.replay import replay_events  # noqa: E402
from tests.test_rfid_folds import HOLES, _Table  # noqa: E402


def _board(tb: _Table, index: int, card: str) -> None:
    ev = RFIDEvent(tag_id=card, card=card, reader_id="b", role="board", seat=None,
                   timestamp=tb.now, raw_tag_id=card, board_index=index)
    tb.recorder.record(ev)
    tb.t._process_rfid_event(ev)   # noqa: SLF001


def _foldout(tb: _Table) -> None:
    tb.deal()
    tb.lift(6)
    tb.tick(tb.now + 3.5)
    tb.lift(4)
    tb.tick(tb.now + 3.5 + FOLDOUT_CONFIRM_SEC)
    (hand,) = tb.hands
    assert hand.winner_source == "fold" and hand.board == []


def _stored(tmp_path) -> dict:
    return json.loads((tmp_path / "folds.json").read_text(encoding="utf-8"))["hands"][-1]


def test_a_flop_after_a_foldout_is_kept_on_the_closed_hand(tmp_path):
    tb = _Table(tmp_path)
    _foldout(tb)
    for i, card in enumerate(["9s", "4s", "Js"], start=1):
        _board(tb, i, card)
        tb.tick(tb.now + 0.6)
    tb.tick(tb.now + 20)
    _board(tb, 4, "7s")
    (hand,) = tb.hands
    assert hand.board == [] and hand.winner_seat == 5          # ライブの勝者・ボードは変えない
    assert hand.board_after_end == ["9s", "4s", "Js", "7s"]
    assert _stored(tmp_path)["board_after_end"] == ["9s", "4s", "Js", "7s"]
    assert input_board(_stored(tmp_path)) == ["9s", "4s", "Js", "7s"]
    notes = [n for n in tb.notices if "置かれました" in n]
    assert len(notes) == 1 and "ハンド 1" in notes[0] and "フロップ" in notes[0]


def test_it_is_the_same_on_replay(tmp_path):
    tb = _Table(tmp_path)
    _foldout(tb)
    for i, card in enumerate(["9s", "4s", "Js"], start=1):
        _board(tb, i, card)
        tb.tick(tb.now + 0.6)
    tb.cards = {}
    tb.deal({4: ["As", "Ad"], 5: ["Ks", "Kd"], 6: ["Qs", "Qd"]})   # 次の配布で数えるのをやめる
    _board(tb, 1, "2c")
    replayed = replay_events(
        tb.recorder.events, backend="pokerkit",
        players=[PlayerState(seat=s, name=f"P{s}", stack=10000) for s in (4, 5, 6)],
        sb=100, bb=200, session_id="replay", out_dir=tmp_path / "replay",
        auto_new_hand=True, auto_winner=True, rfid_folds=True,
    )
    assert replayed[0].board_after_end == tb.hands[0].board_after_end == ["9s", "4s", "Js"]


def test_a_sweep_of_cards_is_not_a_board(tmp_path):
    """片付け・ウォッシュ（1 秒のうちに 4 枚以上）は数えない。"""
    tb = _Table(tmp_path)
    _foldout(tb)
    for i, card in enumerate(["9s", "4s", "Js", "7s"], start=1):
        _board(tb, i, card)
        tb.tick(tb.now + 0.1)
    assert tb.hands[0].board_after_end is None
    assert "board_after_end" not in _stored(tmp_path)


def test_two_cards_are_not_a_flop(tmp_path):
    tb = _Table(tmp_path)
    _foldout(tb)
    _board(tb, 1, "9s")
    _board(tb, 2, "4s")
    assert tb.hands[0].board_after_end is None


def test_input_board_prefers_the_longer_board():
    assert input_board({"board": ["9s", "4s", "Js"], "board_after_end": ["9s", "4s", "Js", "7s"]}) == [
        "9s", "4s", "Js", "7s"]
    assert input_board({"board": ["9s", "4s", "Js", "7s"]}) == ["9s", "4s", "Js", "7s"]


def test_a_seat_whose_cards_are_not_read_is_noticed(tmp_path):
    """ほかの席は手札 2 枚が読めたのに、2 ハンド続けて 2 枚そろわない席を知らせる（1 回）。"""
    tb = _Table(tmp_path)
    for n in range(UNREAD_SEAT_HANDS + 1):            # 席6 のリーダーが読めない
        tb.cards = {}
        tb.deal({4: HOLES[4], 5: HOLES[5]} if n % 2 == 0 else {4: ["As", "Ad"], 5: ["Ks", "Kd"]})
        tb.tick(tb.now + 5.5)
        tb.t._check_unread_seats()   # noqa: SLF001
        tb.t._finalize_hand(5)       # noqa: SLF001
        assert len(tb.hands) == n + 1
    notes = [n for n in tb.notices if "リーダーが手札を読めていません" in n]
    assert len(notes) == 1 and notes[0].startswith("席6 ")


def test_the_truth_screens_fill_the_board_from_after_the_end(tmp_path):
    """真のアクション入力（一覧の画面・卓の形の画面）の自動のボードは、閉じたあとに置かれた札まで。"""
    from core.ground_truth_repository import GroundTruthRepository
    from tests.test_tools_ground_truth_ui import SID, _hand
    from tools.ground_truth_ui import hand_detail
    from tools.table_entry import table_hand

    hand = dict(_hand(1), board=[], board_after_end=["9s", "4s", "Js", "7s"])
    (tmp_path / f"{SID}.json").write_text(json.dumps({"session_id": SID, "hands": [hand]}), encoding="utf-8")
    repo = GroundTruthRepository(tmp_path)
    assert hand_detail(tmp_path, SID, 1, repo)["captured"]["board"] == ["9s", "4s", "Js", "7s"]
    assert table_hand(tmp_path, SID, 1, repo)["base"]["board"] == ["9s", "4s", "Js", "7s"]
