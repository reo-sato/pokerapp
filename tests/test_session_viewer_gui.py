"""tests/test_session_viewer_gui.py

WS2-α: SessionViewerWindow（read-only）のロジックテスト（tkinter 不要）。

customtkinter をモックして構築し、read model 組み立て / 表示整形 / 選択 / refresh /
player_id→display_name 解決 / unknown player の安全表示を検証する。read-only であること
（編集系コマンドを持たない）と、hand logger dashboard / player registry とは別構造である
ことも併せて確認する。
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch


from core.player_repository import PlayerRepository
from core.session_repository import SessionRepository


# ――― セットアップ ―――

def _make_repos(tmp_path: Path):
    players = PlayerRepository(path=tmp_path / "players.json")
    players.create_player("Alice")
    players.create_player("Bob")
    players.create_player("Carol")
    sessions = SessionRepository(path=tmp_path / "sessions.json", player_repo=players)
    return sessions, players


def _pid(players: PlayerRepository, name: str) -> str:
    return next(p.player_id for p in players.list_players() if p.display_name == name)


def _make_window(session_repo: SessionRepository, player_repo: PlayerRepository):
    ctk_mock = MagicMock()
    for attr in (
        "CTkLabel", "CTkFrame", "CTkScrollableFrame", "CTkTextbox",
        "CTkButton", "CTk", "CTkToplevel",
    ):
        getattr(ctk_mock, attr).return_value = MagicMock()

    with patch.dict("sys.modules", {"customtkinter": ctk_mock}):
        from gui.session_viewer import SessionViewerWindow
        win = SessionViewerWindow(session_repo=session_repo, player_repo=player_repo)

    win._set_status = MagicMock()
    return win


# ――― empty / no-data state ―――

class TestEmptyState:
    def test_no_sessions_list_is_empty(self, tmp_path: Path):
        sessions, players = _make_repos(tmp_path)
        win = _make_window(sessions, players)
        assert win._session_list_items() == []
        assert win._selected_id is None

    def test_format_empty_seating_and_hands(self):
        from gui.session_viewer import SessionViewerWindow as W
        assert W._format_seating_text([]) == "（seating はまだありません）"
        assert W._format_hands_text([]) == "（記録された hand はまだありません）"


# ――― session list 要約 ―――

class TestSessionListItems:
    def test_counts_hands_and_assignments(self, tmp_path: Path):
        sessions, players = _make_repos(tmp_path)
        s = sessions.create_session(label="Friday")
        sessions.assign_seat(s.session_id, 1, 3, _pid(players, "Alice"))
        sessions.assign_seat(s.session_id, 1, 5, _pid(players, "Bob"))
        sessions.assign_seat(s.session_id, 2, 6, _pid(players, "Carol"))
        win = _make_window(sessions, players)

        items = win._session_list_items()
        assert len(items) == 1
        assert items[0].label == "Friday"
        assert items[0].status == "open"
        assert items[0].hand_count == 2
        assert items[0].assignment_count == 3


# ――― 選択 → 詳細更新 ―――

class TestSelection:
    def test_select_sets_id_and_builds_detail(self, tmp_path: Path):
        sessions, players = _make_repos(tmp_path)
        s = sessions.create_session(label="Sat", blinds={"sb": 100, "bb": 200})
        sessions.assign_seat(s.session_id, 1, 3, _pid(players, "Alice"))
        sessions.assign_seat(s.session_id, 1, 5, _pid(players, "Bob"))
        win = _make_window(sessions, players)

        win._select_session(s.session_id)
        assert win._selected_id == s.session_id

        detail = win._build_detail(s.session_id)
        assert detail.session.label == "Sat"
        assert detail.session.blinds == {"sb": 100, "bb": 200}
        # hand 1 の 2 席が seat_no 昇順で読める
        assert len(detail.hands) == 1
        hand_id, rows = detail.hands[0]
        assert hand_id == 1
        assert [(r.seat_no, r.display_name) for r in rows] == [(3, "Alice"), (5, "Bob")]

    def test_select_unknown_session_is_safe(self, tmp_path: Path):
        sessions, players = _make_repos(tmp_path)
        win = _make_window(sessions, players)
        win._select_session("does-not-exist")
        # _render_detail が SessionNotFoundError を握り、選択を解除して error 表示する
        assert win._selected_id is None
        win._set_status.assert_called()
        assert win._set_status.call_args.kwargs.get("error") is True


# ――― current seating（最新 hand から導出） ―――

class TestCurrentSeating:
    def test_current_seating_uses_latest_hand(self, tmp_path: Path):
        sessions, players = _make_repos(tmp_path)
        s = sessions.create_session()
        sessions.assign_seat(s.session_id, 1, 3, _pid(players, "Alice"))
        sessions.assign_seat(s.session_id, 2, 6, _pid(players, "Bob"))
        win = _make_window(sessions, players)

        detail = win._build_detail(s.session_id)
        assert [(r.seat_no, r.display_name) for r in detail.current_seating] == [(6, "Bob")]

    def test_summary_reports_seating_presence(self, tmp_path: Path):
        sessions, players = _make_repos(tmp_path)
        s = sessions.create_session()
        win = _make_window(sessions, players)
        detail = win._build_detail(s.session_id)
        summary = win._format_summary_text(detail)
        assert "current seating : なし" in summary
        assert s.session_id in summary


# ――― player_id → display_name 解決 ―――

class TestNameResolution:
    def test_resolve_known_player(self, tmp_path: Path):
        sessions, players = _make_repos(tmp_path)
        win = _make_window(sessions, players)
        assert win._resolve_display_name(_pid(players, "Alice")) == "Alice"

    def test_resolve_unknown_player_returns_label(self, tmp_path: Path):
        sessions, players = _make_repos(tmp_path)
        win = _make_window(sessions, players)
        assert win._resolve_display_name("ff" * 16) == "(unknown)"

    def test_unknown_player_in_persisted_data_renders_safely(self, tmp_path: Path):
        """別ストア由来などで player_id が registry に無くても落ちず、(unknown) 表示。"""
        players = PlayerRepository(path=tmp_path / "players.json")
        players.create_player("Alice")
        db = tmp_path / "sessions.json"
        db.write_text(
            json.dumps(
                {
                    "sessions": [
                        {
                            "session_id": "sess1",
                            "started_at": "2026-06-03T10:00:00",
                            "status": "open",
                            "label": "injected",
                            "hands": {
                                "1": {
                                    "started_at": "2026-06-03T10:00:00",
                                    "seats": [
                                        {"seat_no": 2, "player_id": _pid(players, "Alice")},
                                        {"seat_no": 4, "player_id": "ff" * 16},
                                    ],
                                }
                            },
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        sessions = SessionRepository(path=db, player_repo=players)
        win = _make_window(sessions, players)

        detail = win._build_detail("sess1")
        hand_id, rows = detail.hands[0]
        by_seat = {r.seat_no: r for r in rows}
        assert by_seat[2].display_name == "Alice"
        assert by_seat[4].display_name == "(unknown)"
        # player_id 自体はそのまま保持される
        assert by_seat[4].player_id == "ff" * 16


# ――― refresh = repository 再読込 ―――

class TestRefresh:
    def test_refresh_rereads_from_disk(self, tmp_path: Path):
        sessions, players = _make_repos(tmp_path)
        sessions.create_session(label="s1")
        win = _make_window(sessions, players)
        assert len(win._session_list_items()) == 1

        # 別インスタンス（= 別プロセス相当）が disk に session を追加する
        other = SessionRepository(path=tmp_path / "sessions.json", player_repo=players)
        other.create_session(label="s2")
        # viewer の in-memory repo はまだ古い
        assert len(win._session_repo.list_sessions()) == 1

        win._cmd_refresh()
        # refresh で disk から再読込され、新 session が見える
        assert len(win._session_repo.list_sessions()) == 2
        assert len(win._session_list_items()) == 2

    def test_refresh_drops_selection_if_session_vanishes(self, tmp_path: Path):
        sessions, players = _make_repos(tmp_path)
        s = sessions.create_session(label="will-stay")
        win = _make_window(sessions, players)
        win._select_session(s.session_id)
        assert win._selected_id == s.session_id
        # 既存 session が残っていれば選択は維持される
        win._cmd_refresh()
        assert win._selected_id == s.session_id


# ――― read-only / 別構造であることの確認 ―――

class TestReadOnlyAndSeparation:
    def test_no_mutation_commands(self, tmp_path: Path):
        sessions, players = _make_repos(tmp_path)
        win = _make_window(sessions, players)
        # 作成 / リネーム / 割り当て / 削除 / close の操作を一切持たない
        for forbidden in (
            "_cmd_add", "_cmd_rename", "_cmd_assign", "_cmd_create",
            "_cmd_close", "_cmd_delete", "_cmd_new_hand", "_cmd_winner",
        ):
            assert not hasattr(win, forbidden), f"viewer は {forbidden} を持つべきでない"
        # 許容される唯一の書き込み系操作は refresh のみ
        assert hasattr(win, "_cmd_refresh")

    def test_does_not_depend_on_hand_logger(self, tmp_path: Path):
        sessions, players = _make_repos(tmp_path)
        win = _make_window(sessions, players)
        assert not hasattr(win, "_gs")
        assert not hasattr(win, "_writer")
        assert not hasattr(win, "_audio_queue")
        assert win._session_repo is sessions
        assert win._player_repo is players

    def test_distinct_from_dashboard_and_registry(self):
        from gui.session_viewer import SessionViewerWindow
        from gui.player_registry import PlayerRegistryWindow
        from gui.dashboard import GUIDashboard
        assert SessionViewerWindow is not PlayerRegistryWindow
        assert SessionViewerWindow is not GUIDashboard
        assert SessionViewerWindow.__module__ not in (
            PlayerRegistryWindow.__module__, GUIDashboard.__module__,
        )
