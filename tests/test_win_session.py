"""このプロセスの Windows のセッションの種類（`core/win_session.py`, ADR-0073 段階 0）。"""
from __future__ import annotations

import sys

from core.win_session import describe_session, session_info, session_kind


def test_kinds_and_words():
    assert session_kind({"session": 1, "console": True, "remote": False, "state": "active"}) == "console"
    assert session_kind({"session": 1, "console": False, "remote": True, "state": "active"}) == "rdp"
    assert session_kind({"session": 1, "console": False, "remote": False, "state": "disconnected"}) == "disconnected"
    assert session_kind({"session": 2, "console": False, "remote": False, "state": "active"}) == "other"
    assert session_kind({"platform": "linux"}) == session_kind(None) == "unknown"
    assert describe_session({"session": 1, "console": True, "remote": False, "state": "active"}) == "コンソール（RDP なし）"
    assert describe_session({"session": 1, "console": False, "remote": True, "state": "active"}) == "RDP でつないでいる"
    assert describe_session({"session": 1, "console": False, "remote": False, "state": "disconnected"}) == \
        "切断（RDP を閉じたまま）"
    assert describe_session({"platform": "linux"}) == "Windows 以外（linux）"


def test_session_info_outside_windows():
    if sys.platform != "win32":
        assert session_info() == {"platform": sys.platform}
