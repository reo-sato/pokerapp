"""リバーの前の「ショーダウン」の言い間違い（オーナー 2026-10-28。10/26 ハンド 24: 誤ってショーダウンと言ったあと、
リバーを配ってアクションが続いた）。宣言どおりすぐ閉じるが、閉じる前の状態を残し、あとから賭けの語が話されたら言い間違いと
みて戻す。札が出るだけ（ランアウト）は言い間違いにしない。"""
from __future__ import annotations

import pytest

pytest.importorskip("pokerkit")

from core.events import RFIDEvent  # noqa: E402
from core.game_state import PlayerState  # noqa: E402
from integration.replay import replay_events  # noqa: E402
from tests.test_rfid_folds import _Table  # noqa: E402

HOLES2 = {4: ["6s", "Qc"], 5: ["4s", "7c"]}


def _board(tb: _Table, index: int, card: str) -> None:
    ev = RFIDEvent(tag_id=card, card=card, reader_id="b", role="board", seat=None,
                   timestamp=tb.now, raw_tag_id=card, board_index=index)
    tb.recorder.record(ev)
    tb.t._process_rfid_event(ev)   # noqa: SLF001


def _to_the_turn(tb: _Table) -> None:
    tb.deal(HOLES2)
    tb.say("コール チェック")
    for i, card in enumerate(["2c", "7d", "9h"], start=1):
        _board(tb, i, card)
    tb.tick(tb.now + 1)
    tb.say("チェック チェック")
    _board(tb, 4, "Kd")
    tb.tick(tb.now + 1)


def test_a_river_card_after_it_makes_it_a_slip(tmp_path):
    tb = _Table(tmp_path, seats=(4, 5))
    _to_the_turn(tb)
    tb.say("ショーダウン")                        # ターンで言い間違い
    assert tb.t._pending_showdown is not None and tb.t._showdown_declared   # noqa: SLF001
    assert any("閉じていないベッティング" in n for n in tb.notices)          # いったん宣言どおり閉じる
    tb.tick(tb.now + 2)
    tb.say("チェック")
    assert tb.t._pending_showdown is None and not tb.t._showdown_declared  # noqa: SLF001 — 賭けの語 = 言い間違い
    assert any("言い間違い" in n for n in tb.notices) and tb.t._hand_needs_review   # noqa: SLF001
    tb.say("チェック")
    _board(tb, 5, "3s")
    tb.tick(tb.now + 1)
    tb.say("ベット 1200 コール")
    assert [a[2] for a in tb.played() if a[0] == "river"] == ["bet", "call"]   # リバーのアクションが続く


def test_a_runout_alone_is_not_a_slip(tmp_path):
    """オールインでなくても、ショーダウンと言ってから残りの札を出すことはある（ランアウト）。札だけでは戻さない。"""
    tb = _Table(tmp_path, seats=(4, 5))
    _to_the_turn(tb)
    tb.say("ショーダウン")
    _board(tb, 5, "3s")
    tb.tick(tb.now + 1)
    assert tb.t._showdown_declared and not any("言い間違い" in n for n in tb.notices)   # noqa: SLF001


def test_a_word_spoken_before_it_is_not_a_slip(tmp_path):
    """認識が遅れて届いた、ショーダウンより前に話した語は言い間違いの証拠にしない。"""
    tb = _Table(tmp_path, seats=(4, 5))
    _to_the_turn(tb)
    tb.say("ショーダウン")
    tb.say("チェック", spoken_at=tb.now - 5)
    assert tb.t._showdown_declared and not any("言い間違い" in n for n in tb.notices)   # noqa: SLF001


def test_without_anything_after_it_closes_as_said(tmp_path):
    tb = _Table(tmp_path, seats=(4, 5))
    _to_the_turn(tb)
    tb.say("ショーダウン")
    tb.cards = {}
    tb.deal({4: ["As", "Ad"], 5: ["Ks", "Kd"]})   # 次の配布で閉じる
    hand = tb.hands[0]
    assert any(a.reason and "implied_before_showdown" in a.reason for a in hand.actions)


def test_the_replay_decides_the_same(tmp_path):
    tb = _Table(tmp_path, seats=(4, 5))
    _to_the_turn(tb)
    tb.say("ショーダウン")
    tb.tick(tb.now + 2)
    tb.say("チェック")
    _board(tb, 5, "3s")
    tb.tick(tb.now + 1)
    tb.say("チェック チェック")
    tb.tick(tb.now + 12)
    tb.cards = {}
    tb.deal({4: ["As", "Ad"], 5: ["Ks", "Kd"]})
    live = tb.hands[0]
    replayed = replay_events(
        tb.recorder.events, backend="pokerkit",
        players=[PlayerState(seat=s, name=f"P{s}", stack=10000) for s in (4, 5)],
        sb=100, bb=200, session_id="replay", out_dir=tmp_path / "replay",
        auto_new_hand=True, auto_winner=True, rfid_folds=True,
    )
    assert ([(a.street, a.seat, a.action, a.amount) for a in replayed[0].actions]
            == [(a.street, a.seat, a.action, a.amount) for a in live.actions])
