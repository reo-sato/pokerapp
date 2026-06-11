"""tests/test_phase_m3_seat_selection.py

Phase M3 (= E3, ISSUE-0006) — seat 選択の main.py 結線と mid-session seat change。

検査対象:
- IntegrationThread: seat_assign イベントが次ハンド開始時に seat_player_map / 表示名へ反映される
  （現行ハンドには影響しない）。割当解除（raw_text 空）。空 map から後追いで有効化。
- GameStateManager / PokerkitGameState: set_player_name（additive）。
- main._prompt_session_config(player_repo): registry 選択 / 新規登録 / 割当なし / 重複拒否。
- main._maybe_close_session: y で close、それ以外は no-op。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from core.event_queue import make_audio_queue
from core.events import AudioEvent
from core.game_state import GameStateManager, PlayerState
from core.player_repository import PlayerRepository
from core.session_repository import SessionRepository
from integration.engine import IntegrationThread
from output.json_writer import JsonWriter


def _players(n: int = 2) -> list[PlayerState]:
    return [PlayerState(seat=i + 1, name=f"P{i + 1}", stack=10000) for i in range(n)]


def _registry_and_session(tmp_path: Path):
    prepo = PlayerRepository(path=tmp_path / "players.json")
    alice = prepo.create_player("Alice")
    bob = prepo.create_player("Bob")
    carol = prepo.create_player("Carol")
    srepo = SessionRepository(path=tmp_path / "sessions.json", player_repo=prepo)
    session = srepo.create_session(label="m3")
    return prepo, srepo, session, alice, bob, carol


def _thread(tmp_path: Path, srepo, session, seat_map, gs=None, captured=None):
    return IntegrationThread(
        audio_queue=make_audio_queue(),
        game_state=gs or GameStateManager(_players(2), sb=100, bb=200),
        json_writer=JsonWriter(tmp_path, session.session_id),
        on_hand=(captured.append if captured is not None else None),
        stop_event=threading.Event(),
        session_repo=srepo, seat_player_map=seat_map,
    )


def _ev(action: str, raw_text: str = "", seat: int | None = None) -> AudioEvent:
    return AudioEvent(action=action, amount=0, timestamp=time.time(),
                      raw_text=raw_text, seat=seat)


class TestSeatAssignEvent:
    def test_change_applies_from_next_hand(self, tmp_path: Path):
        _, srepo, session, alice, bob, carol = _registry_and_session(tmp_path)
        gs = GameStateManager(_players(2), sb=100, bb=200)
        thread = _thread(tmp_path, srepo, session,
                         {1: alice.player_id, 2: bob.player_id}, gs=gs)

        thread._handle_audio_event(_ev("new_hand"))
        hand1 = gs.hand_id
        # ハンド中に席2を Bob → Carol へ変更（次ハンドから反映されるべき）
        thread._handle_audio_event(_ev("seat_assign", raw_text=carol.player_id, seat=2))
        assert srepo.resolve_seat_map_for_hand(session.session_id, hand1)[2] == bob.player_id
        assert gs.get_player_name(2) == "P2"  # 現行ハンドの表示名は不変

        thread._handle_audio_event(_ev("winner", raw_text="シート1 ウィナー"))
        thread._handle_audio_event(_ev("new_hand"))
        hand2 = gs.hand_id

        seat_map = srepo.resolve_seat_map_for_hand(session.session_id, hand2)
        assert seat_map[1] == alice.player_id
        assert seat_map[2] == carol.player_id
        assert gs.get_player_name(2) == "Carol"  # 表示名も次ハンドで更新

    def test_unassign_with_empty_player_id(self, tmp_path: Path):
        _, srepo, session, alice, bob, _ = _registry_and_session(tmp_path)
        thread = _thread(tmp_path, srepo, session,
                         {1: alice.player_id, 2: bob.player_id})
        thread._handle_audio_event(_ev("seat_assign", raw_text="", seat=2))
        thread._handle_audio_event(_ev("new_hand"))
        hand_id = thread._game_state.hand_id
        assert srepo.resolve_seat_map_for_hand(session.session_id, hand_id) == {
            1: alice.player_id,
        }

    def test_activates_from_empty_map(self, tmp_path: Path, ):
        """空 map + session_repo で開始しても、seat_assign 後のハンドから write-through が効く。"""
        _, srepo, session, alice, _, _ = _registry_and_session(tmp_path)
        captured: list = []
        thread = _thread(tmp_path, srepo, session, {}, captured=captured)
        # 未割当のままの hand: player_id キーなし（従来動作）
        thread._handle_audio_event(_ev("new_hand"))
        thread._handle_audio_event(_ev("winner", raw_text="シート1 ウィナー"))
        assert all("player_id" not in p for p in captured[-1].to_dict()["players"])

        thread._handle_audio_event(_ev("seat_assign", raw_text=alice.player_id, seat=1))
        thread._handle_audio_event(_ev("new_hand"))
        thread._handle_audio_event(_ev("winner", raw_text="シート1 ウィナー"))
        pmap = {p["seat"]: p.get("player_id") for p in captured[-1].to_dict()["players"]}
        assert pmap[1] == alice.player_id

    def test_ignored_when_session_layer_disabled(self, tmp_path: Path):
        gs = GameStateManager(_players(2), sb=100, bb=200)
        thread = IntegrationThread(
            audio_queue=make_audio_queue(), game_state=gs,
            json_writer=JsonWriter(tmp_path, "legacy-session"),
            stop_event=threading.Event(),
        )
        thread._handle_audio_event(_ev("seat_assign", raw_text="f" * 32, seat=1))
        thread._handle_audio_event(_ev("new_hand"))
        assert thread._seat_player_map == {}
        assert gs.get_player_name(1) == "P1"


class TestSetPlayerName:
    def test_legacy_backend(self):
        gs = GameStateManager(_players(2), sb=100, bb=200)
        gs.set_player_name(1, "Alice")
        assert gs.get_player_name(1) == "Alice"
        with pytest.raises(ValueError):
            gs.set_player_name(9, "X")

    def test_pokerkit_backend(self):
        pytest.importorskip("pokerkit")
        from core.poker_engine import create_game_state

        gs = create_game_state("pokerkit", _players(2), sb=100, bb=200)
        gs.set_player_name(1, "Alice")
        assert gs.get_player_name(1) == "Alice"


class TestPromptSeatPlayers:
    """main._prompt_session_config の registry 選択分岐（input を差し替えて検証）。"""

    def _run_prompt(self, monkeypatch, prepo, answers: list[str]) -> dict:
        import main as main_mod

        it = iter(answers)
        monkeypatch.setattr("builtins.input", lambda *_: next(it))
        return main_mod._prompt_session_config(player_repo=prepo)

    def test_select_create_and_skip(self, tmp_path: Path, monkeypatch):
        prepo = PlayerRepository(path=tmp_path / "players.json")
        alice = prepo.create_player("Alice")
        cfg = self._run_prompt(monkeypatch, prepo, [
            "3",            # 席数
            "1", "10000",   # 席1: registry 1 番 (Alice) + スタック
            "n", "Dave", "20000",  # 席2: 新規登録 Dave + スタック
            "", "Guest", "30000",  # 席3: 割当なし → 自由入力名 + スタック
            "100", "200",   # SB/BB
            "",             # log_dir
        ])
        dave = next(p for p in prepo.list_players() if p.display_name == "Dave")
        assert cfg["seat_players"] == {1: alice.player_id, 2: dave.player_id}
        assert [p["name"] for p in cfg["players"]] == ["Alice", "Dave", "Guest"]

    def test_duplicate_assignment_rejected(self, tmp_path: Path, monkeypatch):
        prepo = PlayerRepository(path=tmp_path / "players.json")
        alice = prepo.create_player("Alice")
        bob = prepo.create_player("Bob")
        cfg = self._run_prompt(monkeypatch, prepo, [
            "2",
            "1", "10000",        # 席1: Alice
            "1",                 # 席2: Alice → 重複で再入力
            "2", "10000",        # 席2: Bob
            "100", "200", "",
        ])
        assert cfg["seat_players"] == {1: alice.player_id, 2: bob.player_id}


class TestViewerEndToEnd:
    """M3 の目的そのもの: hand logger の write-through 出力が viewer read model で読める。"""

    def test_hands_flow_into_viewer_read_models(self, tmp_path: Path):
        from api.read_models import list_player_hands, list_player_sessions

        _, srepo, session, alice, bob, carol = _registry_and_session(tmp_path)
        gs = GameStateManager(_players(2), sb=100, bb=200)
        log_dir = tmp_path / "logs"
        thread = IntegrationThread(
            audio_queue=make_audio_queue(), game_state=gs,
            json_writer=JsonWriter(log_dir, session.session_id),
            stop_event=threading.Event(),
            session_repo=srepo, seat_player_map={1: alice.player_id, 2: bob.player_id},
        )
        # hand 1: Alice + Bob → hand 2: Bob 離席, Carol 着席
        thread._handle_audio_event(_ev("new_hand"))
        thread._handle_audio_event(_ev("winner", raw_text="シート1 ウィナー"))
        thread._handle_audio_event(_ev("seat_assign", raw_text=carol.player_id, seat=2))
        thread._handle_audio_event(_ev("new_hand"))
        thread._handle_audio_event(_ev("winner", raw_text="シート2 ウィナー"))

        alice_sessions = list_player_sessions(alice.player_id, srepo)
        assert [s["session_id"] for s in alice_sessions] == [session.session_id]
        assert alice_sessions[0]["hands_played"] == 2

        bob_hands = list_player_hands(bob.player_id, session.session_id, srepo, log_dir)
        carol_hands = list_player_hands(carol.player_id, session.session_id, srepo, log_dir)
        assert len(bob_hands) == 1 and len(carol_hands) == 1
        assert bob_hands[0]["hand_id"] != carol_hands[0]["hand_id"]
        # seat change 後の hand では席2の帰属/表示名が Carol
        carol_row = next(p for p in carol_hands[0]["players"] if p["seat"] == 2)
        assert carol_row["player_id"] == carol.player_id
        assert carol_row["name"] == "Carol"


class TestMaybeCloseSession:
    def test_close_on_yes(self, tmp_path: Path, monkeypatch):
        import main as main_mod

        prepo = PlayerRepository(path=tmp_path / "players.json")
        srepo = SessionRepository(path=tmp_path / "sessions.json", player_repo=prepo)
        session = srepo.create_session()
        monkeypatch.setattr("builtins.input", lambda *_: "y")
        main_mod._maybe_close_session(srepo, session.session_id)
        assert srepo.get_session(session.session_id).status == "closed"

    def test_keep_open_on_no(self, tmp_path: Path, monkeypatch):
        import main as main_mod

        prepo = PlayerRepository(path=tmp_path / "players.json")
        srepo = SessionRepository(path=tmp_path / "sessions.json", player_repo=prepo)
        session = srepo.create_session()
        monkeypatch.setattr("builtins.input", lambda *_: "")
        main_mod._maybe_close_session(srepo, session.session_id)
        assert srepo.get_session(session.session_id).status == "open"
