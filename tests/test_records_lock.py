"""`sessions.json` / `players.json` を書く前に読み直す（ADR-0073 D5, `core/records_lock.py`）。

前はロガーが起動時に読んだ内容に席の割当を足して書いていたので、プレー中に iPad（店舗サーバー）で行った変更が次のハンドで
消えた。ここでは「別のプロセス」を同じファイルを開いた別のリポジトリで表し、OS のロックは本物の子のプロセスで確かめる。
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

import core.session_repository as session_repository_mod
from core.player_repository import PlayerRepository
from core.records_lock import lock_path_for, records_lock
from core.session_repository import SessionClosedError, SessionRepository

ROOT = Path(__file__).resolve().parent.parent


def _repos(d: Path) -> SessionRepository:
    players = PlayerRepository(d / "players.json")
    return SessionRepository(d / "sessions.json", player_repo=players)


def test_the_logger_does_not_erase_what_the_ipad_changed(tmp_path):
    """ロガー（起動時に読んだ）が席を書いても、そのあいだに店舗サーバーが足したセッション・設定・お客さんが残る。"""
    logger_side = _repos(tmp_path)
    table = logger_side.create_session(label="卓 1")
    alice = logger_side.player_repo.create_player("Alice")

    server_side = _repos(tmp_path)                                  # 別のプロセス（店舗サーバー）
    other = server_side.create_session(label="会計だけの卓")
    server_side.set_hole_card_visibility(table.session_id, "own_and_shown")
    bob = server_side.player_repo.create_player("Bob")

    logger_side.assign_seat(table.session_id, 1, 1, alice.player_id)     # ロガーのハンドの開始
    logger_side.assign_seat(table.session_id, 1, 2, bob.player_id)       # 店舗サーバーが作ったお客さんも見える
    logger_side.player_repo.find_or_create("Carol")

    after = _repos(tmp_path)
    assert {s.session_id for s in after.list_sessions()} == {table.session_id, other.session_id}
    assert after.get_session(table.session_id).hole_card_visibility == "own_and_shown"
    assert {p.display_name for p in after.player_repo.list_players()} == {"Alice", "Bob", "Carol"}
    assert [a.seat_no for a in after.list_seat_assignments(table.session_id, 1)] == [1, 2]


def test_checks_are_made_again_after_reading(tmp_path):
    """読み直したあとに検査をやり直す: 相手が閉じたセッションには席を割り当てない。"""
    logger_side = _repos(tmp_path)
    table = logger_side.create_session()
    alice = logger_side.player_repo.create_player("Alice")
    _repos(tmp_path).close_session(table.session_id)
    with pytest.raises(SessionClosedError):
        logger_side.assign_seat(table.session_id, 1, 1, alice.player_id)


def test_a_failed_write_is_not_kept_only_in_memory(tmp_path, monkeypatch):
    """書けなかった変更をメモリにだけ残さない: 次の書き込みはファイルの内容から始める。"""
    repo = _repos(tmp_path)
    kept = repo.create_session(label="書けた")
    real = session_repository_mod.atomic_write_json

    def fail_once(path, data):
        monkeypatch.setattr(session_repository_mod, "atomic_write_json", real)
        raise OSError("disk full")

    monkeypatch.setattr(session_repository_mod, "atomic_write_json", fail_once)
    lost = repo.create_session(label="書けなかった")
    repo.create_session(label="次")
    labels = {s.label for s in _repos(tmp_path).list_sessions()}
    assert labels == {"書けた", "次"} and lost.session_id not in {s.session_id for s in repo.list_sessions()}
    assert kept.session_id in {s.session_id for s in repo.list_sessions()}


def test_one_lock_file_for_sessions_and_players(tmp_path):
    assert lock_path_for(tmp_path / "sessions.json") == lock_path_for(tmp_path / "players.json") == \
        (tmp_path / "records.lock").resolve()


def test_nested_in_one_thread_and_serialised_across_threads(tmp_path):
    f = tmp_path / "sessions.json"
    order: list[str] = []
    with records_lock(f) as outer:
        with records_lock(f, tmp_path / "players.json") as inner:     # 入れ子（同じロック）
            assert outer and inner

        def other() -> None:
            with records_lock(f):
                order.append("other")

        t = threading.Thread(target=other)
        t.start()
        time.sleep(0.2)
        order.append("first")
    t.join(timeout=5)
    assert order == ["first", "other"]


def test_another_process_waits_for_the_lock(tmp_path):
    """OS のロック: 別のプロセスが持っているあいだは取れない（上限まで待つ）。放せば取れる。"""
    f = tmp_path / "sessions.json"
    holder = subprocess.Popen([sys.executable, "-c", textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {str(ROOT)!r})
        from core.records_lock import records_lock
        with records_lock({str(f)!r}):
            print("held", flush=True)
            time.sleep(1.5)
    """)], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        start = time.monotonic()
        with records_lock(f, timeout=0.3) as got:
            assert got is False and time.monotonic() - start >= 0.25     # 取れない = ロックなしで進める（記録を止めない）
        with records_lock(f, timeout=10) as got:
            assert got is True                                              # 相手が放したら取れる
    finally:
        holder.wait(timeout=10)
