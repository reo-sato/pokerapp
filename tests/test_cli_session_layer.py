"""tests/test_cli_session_layer.py

ADR-0059: ハンドロガー（`--cli`）で席とお客さんの対応を記録する。お客さん向け画面（viewer API）は
`sessions.json` の席の記録からお客さんのハンドを探すので、CLI でも GUI と同じく記録する。

- 起動時に入力した名前を player registry に結び付け（無ければ作る）、session を作る。
- ハンドごとに席 → player を `sessions.json` に書き、ハンドの記録に player_id を載せる。
- `name <席> <名前>` で席替え（次のハンドから）、`name <席> -` で空席にする。`seat 3 call` は従来どおり
  読み上げ文（英語の席表現）で、席替えにはしない。
- `session_layer.enabled=false`（既定）は従来どおり（timestamp の session_id、記録なし）。
"""
from __future__ import annotations

import json
import queue
import threading
import time
from pathlib import Path

import pytest

import core.player_repository as player_repository
import core.session_repository as session_repository
import main
from core.event_queue import make_audio_queue
from core.events import AudioEvent
from core.game_state import GameStateManager, PlayerState
from core.player_repository import PlayerRepository
from core.session_repository import SessionRepository
from integration.engine import IntegrationThread
from output.json_writer import JsonWriter


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """players.json / sessions.json をテスト用の場所へ向ける（リポジトリ直下を汚さない）。"""
    monkeypatch.setattr(player_repository, "_DEFAULT_PLAYER_DB", tmp_path / "players.json")
    monkeypatch.setattr(session_repository, "_DEFAULT_SESSION_DB", tmp_path / "sessions.json")
    return tmp_path


def _cfg(enabled: bool) -> dict:
    return {
        "audio": {"enabled": False},
        "rfid": {"enabled": False},
        "engine": {"backend": "legacy"},
        "table_state": {"enabled": False},
        "session_layer": {"enabled": enabled},
    }


def _run_cli(monkeypatch: pytest.MonkeyPatch, cfg: dict, answers: list[str]) -> None:
    """`run_cli` を入力の列で動かす。コマンドの前に、前のコマンドが処理されるのを待つ。"""
    audio_q = make_audio_queue()
    monkeypatch.setattr("core.config.load_config", lambda path=None: cfg)
    monkeypatch.setattr("core.event_queue.make_audio_queue", lambda: audio_q)
    pending = list(answers)

    def fake_input(prompt: str = "") -> str:
        if prompt == "> ":                     # コマンド: 前のコマンドの処理を待つ
            deadline = time.time() + 5
            while not audio_q.empty() and time.time() < deadline:
                time.sleep(0.01)
            time.sleep(0.15)
        return pending.pop(0)

    monkeypatch.setattr("builtins.input", fake_input)
    main.run_cli()


def _setup(names: list[str], log_dir: Path) -> list[str]:
    """セッション設定の入力（席数・名前・スタック・SB/BB・ボタン・ログ保存先）。"""
    answers = [str(len(names))]
    for name in names:
        answers += [name, "1000"]
    return answers + ["5", "10", "", str(log_dir)]


def _hands(log_dir: Path) -> tuple[str, list[dict]]:
    [path] = [p for p in log_dir.glob("*.json") if not p.name.endswith(".table_state.json")]
    data = json.loads(path.read_text(encoding="utf-8"))
    return path.stem, data["hands"] if isinstance(data, dict) else data


class TestCliSessionLayer:
    def test_names_typed_at_startup_are_recorded_per_hand(
        self, data_dir: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        logs = data_dir / "logs"
        _run_cli(monkeypatch, _cfg(True),
                 _setup(["太郎", "", "花子"], logs) + ["n", "w 1", "q"])

        session_id, hands = _hands(logs)
        players = {p.display_name: p.player_id for p in PlayerRepository().list_players()}
        assert set(players) == {"太郎", "花子"}             # 空 Enter の席（Player2）は作らない
        repo = SessionRepository()
        assert repo.get_session(session_id).blinds == {"sb": 5, "bb": 10}
        assert repo.resolve_seat_map_for_hand(session_id, 1) == {
            1: players["太郎"], 3: players["花子"],
        }
        recorded = {p["seat"]: (p["name"], p.get("player_id")) for p in hands[0]["players"]}
        assert recorded == {
            1: ("太郎", players["太郎"]), 2: ("Player2", None), 3: ("花子", players["花子"]),
        }

    def test_name_command_changes_the_customer_from_the_next_hand(
        self, data_dir: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        logs = data_dir / "logs"
        _run_cli(monkeypatch, _cfg(True),
                 _setup(["太郎", "次郎"], logs)
                 + ["n", "name 2 三郎", "w 1", "n", "w 1", "name 1 -", "n", "w 2", "q"])

        session_id, hands = _hands(logs)
        players = {p.display_name: p.player_id for p in PlayerRepository().list_players()}
        repo = SessionRepository()
        # ハンドの途中の席替えは、そのハンドには効かない（名前も player も揃ったまま）
        assert repo.resolve_seat_map_for_hand(session_id, 1)[2] == players["次郎"]
        assert [p["name"] for p in hands[0]["players"]] == ["太郎", "次郎"]
        assert repo.resolve_seat_map_for_hand(session_id, 2) == {
            1: players["太郎"], 2: players["三郎"],
        }
        assert [p["name"] for p in hands[1]["players"]] == ["太郎", "三郎"]
        # 空席にした席は結び付けない
        assert repo.resolve_seat_map_for_hand(session_id, 3) == {2: players["三郎"]}
        assert [p["name"] for p in hands[2]["players"]] == ["Player1", "三郎"]

    def test_quitting_closes_the_session(
        self, data_dir: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        # お客さんの画面で「進行中」のまま残さない
        logs = data_dir / "logs"
        _run_cli(monkeypatch, _cfg(True), _setup(["太郎", "次郎"], logs) + ["n", "w 1", "q"])
        session_id, _ = _hands(logs)
        session = SessionRepository().get_session(session_id)
        assert session.status == "closed" and session.ended_at

    def test_same_name_is_the_same_customer_across_sessions(
        self, data_dir: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        _run_cli(monkeypatch, _cfg(True), _setup(["太郎", "次郎"], data_dir / "a") + ["q"])
        _run_cli(monkeypatch, _cfg(True), _setup([" 太郎 ", "花子"], data_dir / "b") + ["q"])
        names = sorted(p.display_name for p in PlayerRepository().list_players())
        assert names == ["太郎", "次郎", "花子"]
        assert len(SessionRepository().list_sessions()) == 2

    @pytest.mark.parametrize("enabled", [True, False])
    def test_seat_n_utterances_still_act(
        self, data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
        enabled: bool,
    ):
        # `seat 2 call` は英語の席表現の読み上げ文（parse_action が席 2 のコールと解釈する）。
        # 席替えのコマンドに取られて名前が "call" になってはいけない
        logs = data_dir / "logs"
        _run_cli(monkeypatch, _cfg(enabled),
                 _setup(["太郎", "次郎"], logs) + ["n", "seat 2 call", "w 1", "q"])
        assert "→ call (席2)" in capsys.readouterr().out
        _, hands = _hands(logs)
        assert [p["name"] for p in hands[0]["players"]] == ["太郎", "次郎"]

    def test_disabled_keeps_the_timestamp_session_and_writes_nothing(
        self, data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
    ):
        logs = data_dir / "logs"
        _run_cli(monkeypatch, _cfg(False),
                 _setup(["太郎", "次郎"], logs) + ["n", "name 2 三郎", "w 1", "q"])
        session_id, hands = _hands(logs)
        assert session_id.endswith("_session1")
        assert "player_id" not in hands[0]["players"][0]
        assert not (data_dir / "sessions.json").exists()
        assert not (data_dir / "players.json").exists()
        assert "session_layer.enabled=true" in capsys.readouterr().out


class TestNameCommandParsing:
    @pytest.mark.parametrize("parts, expected", [
        (["name", "3", "山田"], (3, "山田")),
        (["name", "3", "山田", "太郎"], (3, "山田 太郎")),
        (["name", "3", "-"], (3, None)),
        (["name", "3"], None),
        (["name", "x", "山田"], None),
    ])
    def test_parse(self, parts: list[str], expected):
        assert main._parse_name_command(parts) == expected


class TestEngineRenameSeat:
    """engine は席の名前の変更を integration スレッドで適用する（ハンドの途中なら次のハンドから）。"""

    def _engine(self, tmp_path: Path) -> tuple[IntegrationThread, GameStateManager]:
        gs = GameStateManager([PlayerState(1, "A", 100), PlayerState(2, "B", 100)], sb=1, bb=2)
        eng = IntegrationThread(
            audio_queue=queue.Queue(), game_state=gs,
            json_writer=JsonWriter(tmp_path, "s"), stop_event=threading.Event(),
        )
        return eng, gs

    def _ev(self, action: str, **kw) -> AudioEvent:
        return AudioEvent(action=action, amount=0, timestamp=time.time(), raw_text=kw.pop("raw", ""), **kw)

    def test_between_hands_applies_at_once(self, tmp_path: Path):
        eng, gs = self._engine(tmp_path)
        eng._handle_audio_event(self._ev("rename_seat", raw="Z", seat=2))  # noqa: SLF001
        assert gs.get_player_name(2) == "Z"

    def test_mid_hand_waits_for_the_next_hand(self, tmp_path: Path):
        eng, gs = self._engine(tmp_path)
        eng._handle_audio_event(self._ev("new_hand"))                        # noqa: SLF001
        eng._handle_audio_event(self._ev("rename_seat", raw="Z", seat=2))   # noqa: SLF001
        assert gs.get_player_name(2) == "B"
        eng._handle_audio_event(self._ev("winner", raw="シート1 ウィナー"))  # noqa: SLF001
        assert gs.get_player_name(2) == "B"
        eng._handle_audio_event(self._ev("new_hand"))                        # noqa: SLF001
        assert gs.get_player_name(2) == "Z"

    def test_unknown_seat_is_ignored(self, tmp_path: Path):
        eng, gs = self._engine(tmp_path)
        eng._handle_audio_event(self._ev("rename_seat", raw="Z", seat=9))   # noqa: SLF001
        assert (gs.get_player_name(1), gs.get_player_name(2)) == ("A", "B")


class TestRepositoryHelpers:
    def test_find_or_create_reuses_names_and_follows_merges(self, tmp_path: Path):
        repo = PlayerRepository(tmp_path / "p.json")
        a = repo.find_or_create(" 太郎 ")
        assert repo.find_or_create("太郎").player_id == a.player_id
        b = repo.create_player("たろう")
        repo.merge_players(a.player_id, b.player_id)
        assert repo.find_or_create("たろう").player_id == a.player_id

    def test_find_or_create_sees_players_made_by_another_process(self, tmp_path: Path):
        path = tmp_path / "p.json"
        mine, other = PlayerRepository(path), PlayerRepository(path)
        made = other.create_player("花子")
        assert mine.find_or_create("花子").player_id == made.player_id
        assert len(PlayerRepository(path).list_players()) == 1

    def test_reload_if_changed_picks_up_other_writers(self, tmp_path: Path):
        players = PlayerRepository(tmp_path / "p.json")
        reader = SessionRepository(tmp_path / "s.json", player_repo=PlayerRepository(tmp_path / "p.json"))
        writer = SessionRepository(tmp_path / "s.json", player_repo=players)
        assert reader.reload_if_changed() is False
        session = writer.create_session(label="x")
        pid = players.create_player("太郎").player_id
        writer.assign_seat(session.session_id, 1, 1, pid)
        assert reader.reload_if_changed() is True
        assert reader.resolve_seat_map_for_hand(session.session_id, 1) == {1: pid}
        assert reader.player_repo.get(pid).display_name == "太郎"
        assert reader.reload_if_changed() is False

    def test_reload_keeps_data_when_the_file_cannot_be_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        path = tmp_path / "p.json"
        repo = PlayerRepository(path)
        repo.create_player("太郎")
        PlayerRepository(path).create_player("花子")          # 別プロセスが書いた
        monkeypatch.setattr(player_repository, "read_json_file", lambda p: None)  # 書き換え中で読めない
        assert repo.reload_if_changed() is False
        assert [p.display_name for p in repo.list_players()] == ["太郎"]
        monkeypatch.undo()
        assert repo.reload_if_changed() is True
        assert sorted(p.display_name for p in repo.list_players()) == ["太郎", "花子"]


class TestAtomicIoRetries:
    """Windows では別プロセスが開いている間は置き換え・読み込みが一時的に失敗する。"""

    def test_replace_is_retried_on_sharing_violation(self, tmp_path: Path, monkeypatch):
        import core.atomic_io as atomic_io

        real_replace, calls = atomic_io.os.replace, []

        def flaky_replace(src, dst):
            calls.append(1)
            if len(calls) < 3:
                raise PermissionError("in use")
            real_replace(src, dst)

        monkeypatch.setattr(atomic_io.os, "replace", flaky_replace)
        monkeypatch.setattr(atomic_io, "_RETRY_SEC", 0)
        atomic_io.atomic_write_json(tmp_path / "x.json", {"a": 1})
        assert json.loads((tmp_path / "x.json").read_text(encoding="utf-8")) == {"a": 1}
        assert len(calls) == 3

    def test_read_is_retried_on_transient_errors(self, tmp_path: Path, monkeypatch):
        import core.atomic_io as atomic_io

        path = tmp_path / "x.json"
        path.write_text('{"a": 1}', encoding="utf-8")
        real_open, calls = Path.open, []

        def flaky_open(self, *args, **kwargs):
            if self == path and len(calls) < 2:
                calls.append(1)
                raise PermissionError("in use")
            return real_open(self, *args, **kwargs)

        monkeypatch.setattr(Path, "open", flaky_open)
        monkeypatch.setattr(atomic_io, "_RETRY_SEC", 0)
        assert atomic_io.read_json_file(path) == {"a": 1}


class TestArbitrarySeats:
    """卓の空いている席を飛ばして座るとき、好きな番号の席を使える（例 2・5・8 番）。"""

    @pytest.mark.parametrize("raw, expected", [
        ("6", [1, 2, 3, 4, 5, 6]),            # 人数 = 1 番から順（従来どおり）
        ("2 4 5 8", [2, 4, 5, 8]),
        ("8 2 5", [2, 5, 8]),                 # 並びは席番号順に
        ("２　４　８", [2, 4, 8]),             # 全角
        ("2、5、7", [2, 5, 7]),
        ("2,5", [2, 5]),
    ])
    def test_seat_list(self, raw: str, expected: list[int]):
        assert main._parse_seat_list(raw) == expected   # noqa: SLF001

    @pytest.mark.parametrize("raw", ["", "1", "10", "2 2", "0 3", "3 10", "a", "2 b"])
    def test_invalid_seat_list(self, raw: str):
        assert main._parse_seat_list(raw) is None   # noqa: SLF001

    def test_hands_are_played_on_the_chosen_seats(
        self, data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
    ):
        logs = data_dir / "logs"
        answers = ["2 5 8", "太郎", "1000", "", "1000", "花子", "1000", "5", "10",
                   "4", "5", str(logs),              # 4 は使わない席 → 聞き直し → 5
                   "n", "name 3 次郎", "name 8 次郎", "w 5", "n", "w 8", "q"]
        _run_cli(monkeypatch, _cfg(True), answers)
        out = capsys.readouterr().out
        assert "1ハンド目のボタン席 (2/5/8, 空Enterで8)" not in out   # プロンプトは input 側
        assert "使う席（2/5/8）のどれかを入力してください" in out
        assert "席3 はこの卓にありません" in out
        session_id, hands = _hands(logs)
        assert [p["seat"] for p in hands[0]["players"]] == [2, 5, 8]
        assert [p["name"] for p in hands[0]["players"]] == ["太郎", "Player5", "花子"]
        assert [p["name"] for p in hands[1]["players"]] == ["太郎", "Player5", "次郎"]
        players = {p.display_name: p.player_id for p in PlayerRepository().list_players()}
        repo = SessionRepository()
        assert repo.resolve_seat_map_for_hand(session_id, 1) == {2: players["太郎"], 8: players["花子"]}
        assert repo.resolve_seat_map_for_hand(session_id, 2) == {2: players["太郎"], 8: players["次郎"]}
