"""真のアクションのボタン（卓 UI で選んだ席 = 卓で実際に置いたボタン）でロガーのボタンを直す（制御 `truth_button`,
`ground_truth.feed_button`, オーナー 2026-11-19「入れる（設定で on/off）」）。

店舗 2026-11-19 2e598110 ハンド 48〜50: ディーラーがボタンを置き違え、実際のボタン（8・5・4）がロガーのボタン（4・5・6）と
ずれた（ハンド 49 のメモ「ボタンの位置異常」）。ずれた手番で声のアクションが別の席に付いた。
"""
from __future__ import annotations

import pytest

pytest.importorskip("pokerkit")

from core.control_queue import (  # noqa: E402
    ControlCommandLog, command_to_audio_event, parse_truth_button, truth_button_text,
)
from core.events import AudioEvent  # noqa: E402
from core.game_state import PlayerState  # noqa: E402
from integration.replay import replay_events  # noqa: E402
from tests.test_rfid_folds import _Table  # noqa: E402


NEXT = [{4: ["2s", "3s"], 5: ["4d", "5d"], 6: ["8h", "9h"]}, {4: ["2c", "3c"], 5: ["4h", "5h"], 6: ["8s", "9s"]}]


def _fold_out(tb: _Table, first: int, second: int) -> None:
    tb.lift(first)
    tb.tick(tb.now + 3.5)
    tb.lift(second)
    tb.tick(tb.now + 20.0)                          # 残った 1 人の勝ち（確定まで待つ）
    assert not tb.t._hand_open                      # noqa: SLF001


def _next_hand(tb: _Table, k: int) -> None:
    for seat in tb.seats:
        tb.lift(seat)                               # 片付け
    tb.tick(tb.now + 5.0)
    tb.deal(NEXT[k])


def _truth_button(tb: _Table, hand_id: int, seat: int) -> None:
    ev = AudioEvent(action="truth_button", amount=0, timestamp=tb.now, seat=seat,
                    raw_text=truth_button_text(hand_id, seat))
    tb.recorder.record(ev)
    tb.t._handle_audio_event(ev)      # noqa: SLF001


def test_the_text_round_trips_and_the_control_is_translated(tmp_path):
    assert parse_truth_button(truth_button_text(49, 5)) == (49, 5)
    log = ControlCommandLog(tmp_path / "s.control.jsonl")
    log.append("truth_button", {"hand_id": 49, "seat": 5})
    log.append("truth_button", {"hand_id": 49, "seat": 12})        # 卓に無い席は捨てる
    commands, _ = log.read_from(0)
    events = [command_to_audio_event(c, lambda: 1.0) for c in commands]
    assert events[0].action == "truth_button" and events[0].seat == 5 and events[1] is None


def test_the_hand_in_progress_is_rebuttoned(tmp_path):
    tb = _Table(tmp_path)
    tb.deal()                                       # ボタン 席6
    assert tb.gs.button_seat == 6
    _truth_button(tb, tb.gs.hand_id, 4)
    assert tb.gs.button_seat == 4 and any("ボタンを席4 に直して" in n for n in tb.notices)


def test_the_next_hand_follows_the_truth_of_the_previous_hand(tmp_path):
    """前のハンドの真のボタンが届いたとき、次のハンドが進行中なら、そこから 1 つ進めた席に直す。"""
    tb = _Table(tmp_path)
    tb.deal()
    first = tb.gs.hand_id
    _fold_out(tb, 6, 4)                             # 席5 の勝ち
    _next_hand(tb, 0)                               # ボタンは 6 → 4
    assert tb.gs.button_seat == 4
    _truth_button(tb, first, 4)                     # 実際は前のハンドのボタンが 4 → いまは 5
    assert tb.gs.button_seat == 5


def test_between_hands_the_next_button_moves_from_the_truth(tmp_path):
    tb = _Table(tmp_path)
    tb.deal()
    _fold_out(tb, 6, 4)
    _truth_button(tb, tb.gs.hand_id, 5)
    assert any("次のハンドのボタンを進めます" in n for n in tb.notices)
    _next_hand(tb, 0)
    assert tb.gs.button_seat == 6                   # 5 の次


def test_an_older_hand_only_tells(tmp_path):
    tb = _Table(tmp_path)
    tb.deal()
    _fold_out(tb, 6, 4)
    _next_hand(tb, 0)                               # ボタン 4: 手番は 4 → 5
    _fold_out(tb, 4, 5)
    _next_hand(tb, 1)
    before = tb.gs.button_seat
    _truth_button(tb, 1, 5)
    assert tb.gs.button_seat == before and any("直しません" in n for n in tb.notices)


def test_replay_is_the_same(tmp_path):
    tb = _Table(tmp_path)
    tb.deal()
    _truth_button(tb, tb.gs.hand_id, 4)            # ボタン 4: 手番は 4 → 5 → 6
    _fold_out(tb, 4, 5)
    replayed = replay_events(
        tb.recorder.events, backend="pokerkit",
        players=[PlayerState(seat=s, name=f"P{s}", stack=10000) for s in (4, 5, 6)],
        sb=100, bb=200, session_id="replay", out_dir=tmp_path / "replay",
        auto_new_hand=True, auto_winner=True, rfid_folds=True,
    )
    assert replayed[0].button_seat == tb.hands[0].button_seat == 4
    assert [(a.seat, a.action) for a in replayed[0].actions] == [(a.seat, a.action) for a in tb.hands[0].actions]


def test_the_truth_ui_sends_only_a_different_button(tmp_path):
    from tools.ground_truth_ui import feed_button_mode, send_truth_button

    assert send_truth_button(tmp_path, "s", 49, {"button_seat": 5}, {"button_seat": 5}) is None
    assert send_truth_button(tmp_path, "s", 49, {}, {"button_seat": 5}) is None
    assert send_truth_button(tmp_path, "s", 48, {"button_seat": 8}, {"button_seat": 4}) == 8
    commands, _ = ControlCommandLog(tmp_path / "s.control.jsonl").read_from(0)
    assert [(c.type, c.args) for c in commands] == [("truth_button", {"hand_id": 48, "seat": 8})]
    cfg = tmp_path / "config.json"
    cfg.write_text('{"ground_truth": {"feed_button": true}}', encoding="utf-8")
    assert feed_button_mode(cfg) and not feed_button_mode(tmp_path / "none.json")
