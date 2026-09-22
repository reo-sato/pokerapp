"""tests/test_position_evidence.py

ISSUE-0032 / 仕様 §7・FR-26 — **ポジション名の読み上げ**を actor 推定の明示証拠にする。

ディーラーは「シート3、コール」だけでなく「BTN、コール」とも読み上げる（仕様 §7 が推奨）。
席番号と同じ扱いで actor を決められること、そして席番号が優先されること、卓に無いポジションが
無害に無視されること（= 手番が動かない）を固定する。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from audio.recognizer import parse_action
from core.event_queue import make_audio_queue
from core.events import AudioEvent
from core.game_state import GameStateManager, PlayerState
from integration.engine import IntegrationThread
from output.json_writer import JsonWriter


def _players(n: int) -> list[PlayerState]:
    return [PlayerState(seat=i + 1, name=f"P{i + 1}", stack=10000) for i in range(n)]


def _pk(n: int = 6):
    pytest.importorskip("pokerkit")
    from core.poker_engine import PokerkitGameState

    gs = PokerkitGameState(_players(n), sb=100, bb=200)
    gs.new_hand()
    return gs


def _thread(gs, tmp_path: Path, sid: str):
    captured: list = []
    t = IntegrationThread(
        audio_queue=make_audio_queue(), game_state=gs,
        json_writer=JsonWriter(tmp_path, sid),
        on_action=captured.append, stop_event=threading.Event(),
    )
    return t, captured


class TestParseActionCarriesPosition:
    def test_position_is_attached_to_the_event(self):
        ev = parse_action("BTN、コール")
        assert ev is not None
        assert ev.action == "call"
        assert ev.position == "BTN"
        assert ev.seat is None

    def test_seat_and_position_can_coexist(self):
        ev = parse_action("シート3 ボタン レイズ 600")
        assert ev.seat == 3
        assert ev.position == "BTN"

    def test_plain_action_has_no_position(self):
        ev = parse_action("チェック")
        assert ev is not None and ev.position is None

    def test_position_words_do_not_swallow_the_amount(self):
        """"ビッグブラインド" の "ブラインド" が金額パースを壊さない。"""
        ev = parse_action("ビッグブラインド レイズ 600")
        assert ev.action == "raise" and ev.amount == 600 and ev.position == "BB"


class TestPositionResolvesActor:
    def test_named_position_acts_like_an_explicit_seat(self, tmp_path: Path):
        """ボタン 6 の 6-handed: UTG=3 が手番。"CO"（= 5）を名指すと 2 席 fold 合成して 5 が actor。"""
        gs = _pk(6)
        assert gs.position_map() == {1: "SB", 2: "BB", 3: "UTG", 4: "HJ", 5: "CO", 6: "BTN"}
        assert gs.get_current_player() == 3
        t, cap = _thread(gs, tmp_path, "pos1")
        t._handle_audio_event(AudioEvent("call", 0, time.time(), "CO コール", position="CO"))
        acted = [r for r in cap if r.action == "call"]
        assert len(acted) == 1
        assert acted[0].seat == 5
        assert acted[0].position == "CO"
        # 到達のために UTG(3) / HJ(4) が silent fold 合成される
        assert [r.seat for r in cap if r.action == "fold"] == [3, 4]

    def test_explicit_seat_wins_over_position(self, tmp_path: Path):
        """席番号は最も直接的な証拠。両方あれば席番号を採る。"""
        gs = _pk(6)
        t, cap = _thread(gs, tmp_path, "pos2")
        t._handle_audio_event(
            AudioEvent("call", 0, time.time(), "シート4 CO コール", seat=4, position="CO")
        )
        assert [r for r in cap if r.action == "call"][0].seat == 4

    def test_position_matching_the_prior_is_not_a_conflict(self, tmp_path: Path):
        gs = _pk(6)
        t, cap = _thread(gs, tmp_path, "pos3")
        t._handle_audio_event(
            AudioEvent("call", 0, time.time(), "UTG コール", position="UTG", confidence=0.9)
        )
        rec = cap[-1]
        assert rec.seat == 3 and rec.needs_review is False
        assert not [r for r in cap if r.action == "fold"]

    def test_absent_position_is_ignored(self, tmp_path: Path):
        """卓に無いポジション（6-handed の UTG+2）は無視 = engine の手番のまま・合成もしない。"""
        gs = _pk(6)
        t, cap = _thread(gs, tmp_path, "pos4")
        t._handle_audio_event(
            AudioEvent("call", 0, time.time(), "UTG+2 コール", position="UTG+2", confidence=0.9)
        )
        rec = cap[-1]
        assert rec.seat == 3            # prior のまま
        assert rec.needs_review is False
        assert not [r for r in cap if r.action == "fold"]

    def test_position_follows_the_button_on_the_next_hand(self, tmp_path: Path):
        """同じ "BTN" でもハンドが変われば別の席を指す（回転が効いていることの固定）。

        解決そのものを見る。手番を実際にそこまで進められるかは SILENT_FOLD_CAP の話で、
        ここで確かめたいのは「ポジション名 → 席」がボタンに追随することだから。
        """
        gs = _pk(6)
        t, _ = _thread(gs, tmp_path, "pos5")
        ev = AudioEvent("call", 0, time.time(), "BTN コール", position="BTN")
        assert t._sensed_seat(ev) == gs.button_seat

        first_btn = gs.button_seat
        gs.end_hand(1)
        gs.new_hand()
        assert gs.button_seat != first_btn
        assert t._sensed_seat(ev) == gs.button_seat


class TestLegacyIgnoresPosition:
    def test_position_has_no_effect_without_a_button(self, tmp_path: Path):
        """legacy はボタンを持たないので position は解決されない（挙動不変）。"""
        gs = GameStateManager(_players(3), 100, 200)
        gs.new_hand()
        t, cap = _thread(gs, tmp_path, "legacy-pos")
        t._handle_audio_event(AudioEvent("call", 200, time.time(), "BTN コール", position="BTN"))
        rec = cap[-1]
        assert rec.seat == 1          # legacy の単純ラウンドロビン先頭
        assert rec.position == ""
