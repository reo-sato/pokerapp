"""core/win_session.py — このプロセスが動いている Windows のセッション（コンソール / RDP / 切断, ADR-0073 段階 0）。

店舗 PC は自動サインインしたセッション（コンソール = PC につないだ画面）で動く。同じユーザーで RDP につなぐと、
そのセッションがそのまま RDP のセッションになり、× で閉じると「切断」になる（コンソールには戻らない）。マイクの
行き先はセッションの種類で変わることがあるので、ロガーの状態（iPad）とマイクの確認の道具が使う。
"""
from __future__ import annotations

import sys

# WTS_CONNECTSTATE_CLASS（wtsapi32）
_WTS_STATES = {0: "active", 1: "connected", 2: "connect_query", 3: "shadow", 4: "disconnected", 5: "idle",
               6: "listen", 7: "reset", 8: "down", 9: "init"}
STATE_JA = {"active": "使用中", "disconnected": "切断", "connected": "接続中"}


def session_info() -> dict:
    """このプロセスのセッション: `{session, console, remote, state}`。Windows 以外は `{"platform": ...}`。"""
    if sys.platform != "win32":
        return {"platform": sys.platform}
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    user32 = ctypes.windll.user32
    wtsapi32 = ctypes.windll.wtsapi32
    sid = wintypes.DWORD()
    kernel32.ProcessIdToSessionId(kernel32.GetCurrentProcessId(), ctypes.byref(sid))
    console = int(kernel32.WTSGetActiveConsoleSessionId())
    remote = bool(user32.GetSystemMetrics(0x1000))          # SM_REMOTESESSION
    state = None
    buf = ctypes.c_void_p()
    size = wintypes.DWORD()
    # WTSQuerySessionInformationW(WTS_CURRENT_SERVER_HANDLE, WTS_CURRENT_SESSION, WTSConnectState = 8)
    if wtsapi32.WTSQuerySessionInformationW(None, wintypes.DWORD(0xFFFFFFFF), 8, ctypes.byref(buf), ctypes.byref(size)):
        try:
            state = _WTS_STATES.get(ctypes.cast(buf, ctypes.POINTER(ctypes.c_int)).contents.value)
        finally:
            wtsapi32.WTSFreeMemory(buf)
    return {"session": int(sid.value), "console": int(sid.value) == console, "remote": remote, "state": state}


def session_kind(info: dict) -> str:
    """`console` / `rdp` / `disconnected` / `other` / `unknown`（Windows 以外）。"""
    if not isinstance(info, dict) or "platform" in info:
        return "unknown"
    if info.get("state") == "disconnected":
        return "disconnected"
    if info.get("remote"):
        return "rdp"
    if info.get("console"):
        return "console"
    return "other"


def describe_session(info: dict) -> str:
    """セッションを日本語で 1 語に（コンソール / RDP / 切断）。"""
    kind = session_kind(info)
    if kind == "unknown":
        return f"Windows 以外（{(info or {}).get('platform')}）"
    return {"disconnected": "切断（RDP を閉じたまま）", "rdp": "RDP でつないでいる",
            "console": "コンソール（RDP なし）"}.get(kind) or f"その他（{STATE_JA.get(info.get('state'), info.get('state'))}）"
