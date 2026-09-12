"""tests/test_phase_d2_wiring.py

Phase D (#7) D2a — rules-aware 経路の結線（apply_corrections ライブ適用 + actor 競合検出）。

- pokerkit backend: 合法手射影が ActionRecord に反映され、明示席の競合が needs_review を立てる。
- legacy backend: 空 legal_context により従来経路（_handle_legacy_action）へ分岐し挙動不変。

silent-fold 合成（fold_through 結線）と派生 confidence（D3）は後続増分のため本テストは対象外。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from core.event_queue import make_audio_queue
from core.events import AudioEvent, RFIDEvent
from core.game_state import GameStateManager, PlayerState
from core.poker_engine import PokerkitGameState
from integration.engine import IntegrationThread
from output.json_writer import JsonWriter


def _players(n: int) -> list[PlayerState]:
    return [PlayerState(seat=i + 1, name=f"P{i + 1}", stack=10000) for i in range(n)]


def _thread(gs, tmp_path: Path, sid: str):
    captured: list = []
    t = IntegrationThread(
        audio_queue=make_audio_queue(), game_state=gs,
        json_writer=JsonWriter(tmp_path, sid),
        on_action=captured.append, stop_event=threading.Event(),
    )
    return t, captured


def _pk(n: int = 3) -> PokerkitGameState:
    pytest.importorskip("pokerkit")
    gs = PokerkitGameState(_players(n), sb=100, bb=200)
    gs.new_hand()
    return gs


class TestRulesAwareWiring:
    def test_call_uses_state_amount_not_heard(self, tmp_path: Path):
        gs = _pk(3)
        t, cap = _thread(gs, tmp_path, "pk1")
        actor = gs.get_current_player()
        t._handle_audio_event(AudioEvent("call", 9999, time.time(), "コール"))
        rec = cap[-1]
        assert rec.seat == actor
        assert rec.action == "call"
        assert rec.amount != 9999 and rec.amount > 0   # heard 無視・状態の call 額
        assert rec.needs_review is False

    def test_check_facing_bet_becomes_call_review(self, tmp_path: Path):
        gs = _pk(3)
        t, cap = _thread(gs, tmp_path, "pk2")
        # プリフロップ先頭 actor は BB に直面 → "check" 非合法 → call へ射影 + review
        t._handle_audio_event(AudioEvent("check", 0, time.time(), "チェック"))
        rec = cap[-1]
        assert rec.action == "call"
        assert rec.needs_review is True

    def test_explicit_seat_triggers_silent_fold_synthesis(self, tmp_path: Path):
        # D2b: 明示発話席が prior の 1 つ先（cap 内）なら間の席を silent fold 合成し actor を移す。
        # 3-handed 手番順は [3, 1, ...]：prior=seat3、明示=seat1 → seat3 を fold 合成し seat1 が actor。
        gs = _pk(3)
        t, cap = _thread(gs, tmp_path, "pk3")
        prior = gs.get_current_player()  # seat 3
        t._handle_audio_event(AudioEvent("call", 0, time.time(), "シート1 コール", seat=1))
        folds = [r for r in cap if r.action == "fold"]
        assert [r.seat for r in folds] == [prior]   # prior の silent fold を合成
        assert all(r.needs_review for r in folds)    # 合成 fold は要レビュー
        main = cap[-1]
        assert main.seat == 1 and main.action == "call"
        assert main.needs_review is True             # 競合（合成）→ review

    def test_explicit_seat_match_no_conflict(self, tmp_path: Path):
        gs = _pk(3)
        t, cap = _thread(gs, tmp_path, "pk4")
        actor = gs.get_current_player()
        t._handle_audio_event(
            AudioEvent("call", 0, time.time(), f"シート{actor} コール", seat=actor)
        )
        rec = cap[-1]
        assert rec.seat == actor
        assert rec.needs_review is False


class TestLegacyRoutingUnchanged:
    def test_legacy_records_raw_action(self, tmp_path: Path):
        gs = GameStateManager(_players(2), sb=100, bb=200)
        gs.new_hand()
        t, cap = _thread(gs, tmp_path, "lg1")
        seat = gs.get_current_player()
        t._handle_audio_event(AudioEvent("bet", 500, time.time(), "ベット500"))
        rec = cap[-1]
        assert rec.seat == seat
        assert rec.action == "bet"      # legacy は射影しない（生 action）
        assert rec.amount == 500        # heard そのまま
        assert rec.needs_review is False


class TestRfidIsNotActorEvidence:
    """ISSUE-0033: RFID の seat 読みは actor を動かさない（配布と行動を区別できないため）。

    RFID が観測するのは「その席に**カードがある**」であって「その席が**行動した**」ではない。
    ホールカードの配布は数秒で最大 16 件の検出を生み、持ち上げた札を置き直しても 1 件出る。
    カメラ（chip motion = 行動の観測）を廃止した結果、存在検出が「物理証拠」の座に繰り上がって
    いたのが誤りだった（ADR-0045）。RFID は**同席の裏付け**としてのみ使う。
    """

    def _rfid(self, seat: int, ts: float, card: str = "Ah") -> RFIDEvent:
        return RFIDEvent(
            tag_id=card, card=card, timestamp=ts, raw_tag_id=card,
            reader_id=f"seat_{seat}", role="seat", seat=seat, board_index=None,
        )

    def test_card_dealt_to_another_seat_does_not_move_the_actor(self, tmp_path: Path):
        """配布で seat 1 のカードが出ても、席の言及が無い発話は prior が担う。"""
        gs = _pk(3)
        t, cap = _thread(gs, tmp_path, "rfid1")
        prior = gs.get_current_player()          # seat 3
        now = time.time()
        t._process_rfid_event(self._rfid(1, now - 0.2))     # noqa: SLF001 — 配布
        t._handle_audio_event(AudioEvent("call", 0, now, "コール"))   # noqa: SLF001
        assert [r.seat for r in cap] == [prior]  # fold 合成なし・actor も動かない
        assert cap[-1].action == "call" and cap[-1].needs_review is False

    def test_same_seat_rfid_still_corroborates(self, tmp_path: Path):
        """同席の読みは裏付けとして残る（confidence が上がる）。"""
        gs = _pk(3)
        t, cap = _thread(gs, tmp_path, "rfid2")
        actor = gs.get_current_player()
        now = time.time()
        t._process_rfid_event(self._rfid(actor, now - 0.2))          # noqa: SLF001
        t._handle_audio_event(AudioEvent("call", 0, now, "コール"))   # noqa: SLF001
        rec = cap[-1]
        assert rec.seat == actor and rec.source["rfid"] is True

    def test_explicit_seat_still_wins(self, tmp_path: Path):
        """明示発話席は従来どおり actor を動かす（RFID が別席を指していても）。"""
        gs = _pk(3)
        t, cap = _thread(gs, tmp_path, "rfid3")
        prior = gs.get_current_player()          # seat 3
        now = time.time()
        t._process_rfid_event(self._rfid(2, now - 0.2))              # noqa: SLF001 — 無関係な検出
        t._handle_audio_event(  # noqa: SLF001
            AudioEvent("call", 0, now, "シート1 コール", seat=1)
        )
        folds = [r for r in cap if r.action == "fold"]
        assert [r.seat for r in folds] == [prior]
        assert cap[-1].seat == 1
