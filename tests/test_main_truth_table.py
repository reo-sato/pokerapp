"""ロガーを起動したら真のアクション入力の卓の形の画面を開く（`main._open_truth_table`, オーナー 2026-10-26）。"""
from __future__ import annotations

import socket
from pathlib import Path

import main

ROOT = Path(__file__).resolve().parent.parent


class _Tty:
    def isatty(self) -> bool:
        return True


def test_opens_the_table_page_when_the_server_is_up(monkeypatch, capsys):
    opened = []
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    try:
        monkeypatch.setattr(main, "TRUTH_PORT", srv.getsockname()[1])
        monkeypatch.setattr(main.sys, "stdin", _Tty())
        monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))
        main._open_truth_table({})
    finally:
        srv.close()
    assert opened == [f"http://127.0.0.1:{main.TRUTH_PORT}/table"]
    assert "/table" in capsys.readouterr().out


def test_tells_how_to_start_when_the_server_is_down(monkeypatch, capsys):
    opened = []
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    free = s.getsockname()[1]
    s.close()
    monkeypatch.setattr(main, "TRUTH_PORT", free)
    monkeypatch.setattr(main.sys, "stdin", _Tty())
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))
    main._open_truth_table({}, wait_sec=0.2)
    assert opened == [] and "start_truth.cmd" in capsys.readouterr().out


def test_can_be_turned_off_and_skips_piped_input(monkeypatch):
    opened = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))
    monkeypatch.setattr(main.sys, "stdin", _Tty())
    main._open_truth_table({"ground_truth": {"open_table": False}}, wait_sec=0.1)
    assert opened == []


def test_start_logger_starts_the_truth_server_in_another_window():
    text = (ROOT / "start_logger.cmd").read_bytes().decode("ascii")
    assert 'start "ground-truth" "%~dp0start_truth.cmd"' in text
    assert text.index("start_truth.cmd") < text.index("main.py --cli")
