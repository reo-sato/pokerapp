"""tools/console_probe.py — RDP の外（自動サインインしたコンソール）でマイクを使えるかを測る（ADR-0073 段階 0）。

iPad だけでロガーを起動するには、ロガーが RDP の外で録音できなければならない。Windows では:

- 自動サインインした直後のセッションは「コンソール」（PC につないだ画面 = HDMI のダミー）。
- 同じユーザーで RDP につなぐと、そのセッションがそのまま RDP のセッションになる（マイクの行き先が接続元の音声の設定で
  変わることがある）。
- RDP を × で閉じると「切断」になり、コンソールには戻らない（`tscon` で戻せる）。

この道具は、それぞれの状態のあいだ録音を続け、5 秒ごとに「どの状態か・マイクを開けているか・声の大きさ」を記録する。
ロガーと同じく、読めなくなったら PyAudio を作り直して開き直す。マイクの一覧は 30 秒ごとに別のプロセスで数え直す
（PortAudio は起動したときの一覧しか持たない = あとから起動するロガーに見える一覧）。

あわせて、子のプロセスを 1 つ起動して、本体（タスク）を止めたときに子が残るかも記録する（見張り役が子のロガーを持つ
形の確認, ADR-0073 D0）。

使い方（店舗 PC。手順は docs/console-probe.md）:

    installer\\console_probe_task.ps1          # タスク PokerConsoleProbe に登録して起動（いまから 60 分 + 次のサインイン）
    python tools/console_probe.py report      # 記録をまとめて表示（logs/console_probe/）
    python tools/console_probe.py run --seconds 600   # 手で動かす（RDP の中の確認）

記録は `logs/console_probe/<日時>.jsonl`（本体）と `<日時>.child.jsonl`（子）。音声そのものは保存しない（大きさだけ）。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PROBE_DIR = ROOT / "logs" / "console_probe"
ROW_SEC = 5.0             # 記録の間隔
DEVICES_SEC = 30.0        # マイクの一覧を数え直す間隔
CHUNK = 1600              # 0.1 秒（16 kHz）
REOPEN_FIRST_SEC = 1.0
REOPEN_MAX_SEC = 10.0
VOICE_RMS = 300.0         # 声とみなす大きさ（ロガーの audio.speech_rms の既定と同じ）
FRESH_SEC = 1.0           # 一覧を数えるときに、マイクを新しく開いて聞く秒数
STALL_SEC = 3.0           # 読み取りがこれだけ返らなければ「固まった」と記録する

# ───────────────────────── Windows のセッション ─────────────────────────


from core.win_session import describe_session, session_info  # noqa: E402,F401 — ロガーの状態と共通


# ───────────────────────── マイク ─────────────────────────


def _audio_cfg() -> dict:
    from tools.audio_check import load_audio_config

    try:
        return load_audio_config()
    except Exception:  # noqa: BLE001 — 設定が読めなくても既定で測る
        return {}


def device_summary(cfg: Optional[dict] = None) -> dict:
    """いまのマイクの一覧（このプロセスで PyAudio を作って数える）。"""
    import pyaudio  # type: ignore[import]

    from audio.devices import choose_device, list_input_devices, only_wdm_ks, parse_device_names

    cfg = cfg if cfg is not None else _audio_cfg()
    rate = int(cfg.get("sample_rate", 16000))
    pa = pyaudio.PyAudio()
    try:
        devices = list_input_devices(pa, pyaudio, rate)
        names = parse_device_names(cfg.get("device_name"))
        chosen = choose_device(devices, names) if names else next(
            (d for d in devices if d.index == int(cfg.get("device_id", 0))), None)
        fresh = fresh_listen(pa, pyaudio, chosen.index, rate) if chosen is not None else {"fresh_error": "マイクが一覧に無い"}
    finally:
        pa.terminate()
    return {"inputs": len(devices), "wdm_ks_only": bool(devices) and only_wdm_ks(devices),
            "chosen": chosen.name if chosen is not None else None,
            "chosen_index": chosen.index if chosen is not None else None,
            "default": next((d.name for d in devices if d.default), None), **fresh}


def fresh_listen(pa: Any, pyaudio_mod: Any, index: int, rate: int, seconds: float = FRESH_SEC) -> dict:
    """マイクを新しく開いて `seconds` 秒読み、いちばん大きい音を返す（`fresh_rms`）。開けなければ `fresh_error`。

    途中の録音が無音のとき、「開き直せば聞こえる」のか「このセッションでは聞こえない」のかを分けるため。
    """
    try:
        stream = pa.open(format=pyaudio_mod.paInt16, channels=1, rate=rate, input=True,
                         input_device_index=index, frames_per_buffer=CHUNK)
    except Exception as e:  # noqa: BLE001 — 開けなかった理由を記録する
        return {"fresh_error": f"{type(e).__name__}: {e}"[:200]}
    try:
        top = 0.0
        for _ in range(max(1, int(seconds * rate / CHUNK))):
            top = max(top, rms(stream.read(CHUNK, exception_on_overflow=False)))
        return {"fresh_rms": round(top)}
    except Exception as e:  # noqa: BLE001
        return {"fresh_error": f"{type(e).__name__}: {e}"[:200]}
    finally:
        try:
            stream.stop_stream()
            stream.close()
        except Exception:  # noqa: BLE001
            pass


def devices_in_new_process(timeout: float = 20.0) -> dict:
    """別のプロセスで一覧を数える（あとから起動するロガーに見える一覧）。"""
    try:
        out = subprocess.run([sys.executable, str(Path(__file__).resolve()), "devices"], capture_output=True,
                             text=True, timeout=timeout, cwd=str(ROOT), **_no_window())
        if out.stdout.strip():
            return json.loads(out.stdout.strip().splitlines()[-1])
        last = [line for line in out.stderr.splitlines() if line.strip()]
        return {"error": (last[-1] if last else f"終了コード {out.returncode}")[:200]}
    except Exception as e:  # noqa: BLE001 — 数えられなかったことも記録する
        return {"error": str(e)[:200]}


class Mic:
    """ロガーと同じ開き方のマイク（名前で選ぶ・読めなくなったら PyAudio を作り直して開き直す）。"""

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.pa: Any = None
        self.stream: Any = None
        self.name: Optional[str] = None
        self.error: Optional[str] = None

    def open(self) -> bool:
        self.close()
        try:
            import pyaudio  # type: ignore[import]

            from audio.devices import choose_device, list_input_devices, parse_device_names

            self.pa = pyaudio.PyAudio()
            rate = int(self.cfg.get("sample_rate", 16000))
            names = parse_device_names(self.cfg.get("device_name"))
            index = int(self.cfg.get("device_id", 0))
            if names:
                chosen = choose_device(list_input_devices(self.pa, pyaudio, rate), names)
                if chosen is None:
                    self.error = "設定した名前のマイクが一覧にありません"
                    return False
                index = chosen.index
            self.name = str(self.pa.get_device_info_by_index(index).get("name", ""))
            self.stream = self.pa.open(format=pyaudio.paInt16, channels=1, rate=rate, input=True,
                                       input_device_index=index, frames_per_buffer=CHUNK)
            self.error = None
            return True
        except Exception as e:  # noqa: BLE001 — 開けなかった理由を記録する
            self.error = f"{type(e).__name__}: {e}"[:200]
            self.close()
            return False

    def read(self) -> bytes:
        return self.stream.read(CHUNK, exception_on_overflow=False)

    def close(self) -> None:
        for step in (lambda: self.stream and self.stream.stop_stream(), lambda: self.stream and self.stream.close(),
                     lambda: self.pa and self.pa.terminate()):
            try:
                step()
            except Exception:  # noqa: BLE001
                pass
        self.stream = None
        self.pa = None


def rms(data: bytes) -> float:
    from audio.recorder import _calc_rms

    return float(_calc_rms(data))


# ───────────────────────── 記録 ─────────────────────────


def _no_window() -> dict:
    return {"creationflags": 0x08000000} if sys.platform == "win32" else {}     # CREATE_NO_WINDOW


def _append(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))   # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return code.value == 259                                               # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def run_probe(seconds: float, out: Path, *, mic: Any, sessions: Callable[[], dict] = session_info,
              devices: Callable[[], dict] = devices_in_new_process, clock: Callable[[], float] = time.time,
              sleep: Callable[[float], None] = time.sleep, child: Optional[Callable[[], Any]] = None) -> int:
    """`seconds` 秒、録音しながら `ROW_SEC` ごとに 1 行書く。書いた行の数を返す。"""
    start = clock()
    proc = child() if child is not None else None
    _append(out, {"t": start, "kind": "start", "pid": os.getpid(), "child_pid": getattr(proc, "pid", None),
                  "argv": sys.argv[1:], "stdin": sys.stdin is not None, "stderr": sys.stderr is not None,
                  "session": sessions()})
    rows = 0
    next_row = start + ROW_SEC
    next_devices = start
    dev: dict = {}
    window_max, voiced, reads, errors, reopened = 0.0, 0, 0, 0, 0
    delay = REOPEN_FIRST_SEC
    is_open = mic.open()
    retry_at = clock() + delay
    last_read = [clock()]
    done = threading.Event()

    def watch() -> None:
        # 読み取り（stream.read）が返らないと本体は行を書けない。固まったことを別のスレッドで記録する
        while not done.wait(ROW_SEC):
            age = clock() - last_read[0]
            if is_open and age > STALL_SEC:
                _append(out, {"t": clock(), "kind": "stall", "since_read": round(age, 1), "session": sessions()})

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    while clock() - start < seconds:
        now = clock()
        if now >= next_devices:
            dev = {**devices(), "at": round(now, 1)}
            next_devices = now + DEVICES_SEC
        if is_open:
            try:
                data = mic.read()
                last_read[0] = clock()
                value = rms(data)
                reads += 1
                window_max = max(window_max, value)
                voiced += value >= VOICE_RMS
            except Exception as e:  # noqa: BLE001 — 切れた: ロガーと同じく開き直す
                errors += 1
                mic.error = f"{type(e).__name__}: {e}"[:200]
                mic.close()
                is_open = False
                delay = REOPEN_FIRST_SEC
                retry_at = now + delay
        else:
            if now >= retry_at:
                is_open = mic.open()
                if is_open:
                    reopened += 1
                    delay = REOPEN_FIRST_SEC
                else:
                    delay = min(delay * 2, REOPEN_MAX_SEC)
                    retry_at = now + delay
            last_read[0] = clock()
            sleep(0.2)
        if clock() >= next_row:
            _append(out, {"t": clock(), "kind": "row", "session": sessions(), "open": is_open, "mic": mic.name,
                          "error": None if is_open else mic.error, "reads": reads, "rms_max": round(window_max),
                          "voiced": voiced, "read_errors": errors, "reopened": reopened, "devices": dev,
                          "child_alive": _pid_alive(proc.pid) if proc is not None else None})
            rows += 1
            window_max, voiced, reads, errors, reopened = 0.0, 0, 0, 0, 0
            next_row += ROW_SEC
    done.set()
    mic.close()
    _append(out, {"t": clock(), "kind": "end", "session": sessions()})
    return rows


def run_child(seconds: float, out: Path, parent: int, clock: Callable[[], float] = time.time,
              sleep: Callable[[float], None] = time.sleep) -> None:
    """子のプロセス: `ROW_SEC` ごとに「本体が生きているか」を書く。本体が止まっても `seconds` までは動く。"""
    start = clock()
    _append(out, {"t": start, "kind": "child_start", "pid": os.getpid(), "parent": parent, "session": session_info()})
    while clock() - start < seconds:
        _append(out, {"t": clock(), "kind": "child", "parent_alive": _pid_alive(parent)})
        sleep(ROW_SEC)
    _append(out, {"t": clock(), "kind": "child_end"})


# ───────────────────────── まとめ ─────────────────────────


def _hm(t: float) -> str:
    return datetime.fromtimestamp(t).strftime("%H:%M:%S")


def _read(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def summarize(rows: list[dict], child_rows: Optional[list[dict]] = None) -> list[str]:
    """記録を「状態の区間」ごとにまとめる: いつからいつまで・どのセッションか・マイクを開けたか・声があったか。"""
    lines: list[str] = []
    head = next((r for r in rows if r.get("kind") == "start"), None)
    if head:
        lines.append(f"開始 {_hm(head['t'])}  {describe_session(head.get('session') or {})}"
                     f"  （標準入力 {'あり' if head.get('stdin') else 'なし'}・標準エラー {'あり' if head.get('stderr') else 'なし'}）")
    seg: Optional[dict] = None

    def flush() -> None:
        if seg is None:
            return
        mic = (f"マイクを開けている（{seg['mic']}）" if seg["open"]
               else f"マイクを開けない（{seg['error'] or '理由不明'}）")
        voice = (f"声あり {seg['voiced_rows']} 回・最大 {seg['rms_max']}" if seg["voiced_rows"]
                 else f"声なし（最大 {seg['rms_max']}）")
        extra = []
        if seg["read_errors"]:
            extra.append(f"読み取りの失敗 {seg['read_errors']}")
        if seg["reopened"]:
            extra.append(f"開き直し {seg['reopened']}")
        dev = seg["devices"] or {}
        if dev.get("error"):
            extra.append(f"一覧を数えられない（{dev['error'][:60]}）")
        else:
            if dev.get("wdm_ks_only"):
                extra.append("一覧が WDM-KS だけ（RDP の音声が接続元に回っている）")
            if dev and not dev.get("chosen"):
                extra.append("新しく起動すると設定のマイクが見つからない")
        if seg["fresh_tries"]:
            if seg["fresh_voice"]:
                extra.append(f"新しく開くと: 声あり（{seg['fresh_voice']}/{seg['fresh_tries']} 回・最大 {seg['fresh_max']}）")
            elif seg["fresh_open"]:
                extra.append(f"新しく開くと: 無音（最大 {seg['fresh_max']}）")
            else:
                extra.append(f"新しく開くと: 開けない（{seg['fresh_error'] or '理由不明'}）")
        lines.append(f"{_hm(seg['from'])}〜{_hm(seg['to'])}  {seg['where']}: {mic}・{voice}"
                     + (f"（{'・'.join(extra)}）" if extra else ""))

    fresh_seen: set = set()
    for r in rows:
        if r.get("kind") != "row":
            continue
        where = describe_session(r.get("session") or {})
        key = (where, bool(r.get("open")))
        if seg is None or seg["key"] != key:
            flush()
            seg = {"key": key, "where": where, "open": bool(r.get("open")), "mic": r.get("mic"),
                   "error": r.get("error"), "from": r["t"], "to": r["t"], "voiced_rows": 0, "rms_max": 0,
                   "read_errors": 0, "reopened": 0, "devices": r.get("devices"),
                   "fresh_tries": 0, "fresh_open": 0, "fresh_voice": 0, "fresh_max": 0, "fresh_error": None}
        seg["to"] = r["t"]
        seg["voiced_rows"] += 1 if r.get("voiced") else 0
        seg["rms_max"] = max(seg["rms_max"], int(r.get("rms_max") or 0))
        seg["read_errors"] += int(r.get("read_errors") or 0)
        seg["reopened"] += int(r.get("reopened") or 0)
        if r.get("devices"):
            seg["devices"] = r["devices"]
            dev = r["devices"]
            stamp = id(dev) if dev.get("at") is None else dev.get("at")
            if ("fresh_rms" in dev or "fresh_error" in dev) and stamp not in fresh_seen:
                fresh_seen.add(stamp)                 # 30 秒ごとの 1 回を 1 回と数える（同じ結果が 6 行に入る）
                seg["fresh_tries"] += 1
                if "fresh_rms" in dev:
                    seg["fresh_open"] += 1
                    seg["fresh_max"] = max(seg["fresh_max"], int(dev["fresh_rms"]))
                    seg["fresh_voice"] += 1 if dev["fresh_rms"] >= VOICE_RMS else 0
                else:
                    seg["fresh_error"] = dev["fresh_error"]
        if not r.get("open") and r.get("error"):
            seg["error"] = r["error"]
    flush()
    stalls = [r for r in rows if r.get("kind") == "stall"]
    if stalls:
        lines.append(f"録音の読み取りが返らなかった: {_hm(stalls[0]['t'])}〜{_hm(stalls[-1]['t'])}（{len(stalls)} 回。"
                     f"{describe_session(stalls[0].get('session') or {})}）")
    crash = next((r for r in rows if r.get("kind") == "crash"), None)
    if crash:
        lines.append(f"落ちた {_hm(crash['t'])}: {crash.get('error', '')[:300]}")
    end = next((r for r in rows if r.get("kind") == "end"), None)
    last = max((r["t"] for r in rows if "t" in r), default=None)
    if end:
        lines.append(f"終了 {_hm(end['t'])}（決めた時間まで動いた）")
    elif last is not None:
        lines.append(f"記録は {_hm(last)} で止まった（タスクを止めた・PC を再起動した・落ちた）")
    if child_rows:
        started = next((r for r in child_rows if r.get("kind") == "child_start"), None)
        beats = [r for r in child_rows if r.get("kind") == "child"]
        orphan = [r for r in beats if r.get("parent_alive") is False]
        if started and beats:
            if orphan:
                lines.append(f"子のプロセス: 本体が止まった {_hm(orphan[0]['t'])} のあとも {_hm(beats[-1]['t'])} まで動いた"
                             "（本体を止めても子は残る）")
            elif end is None and last is not None and beats[-1]["t"] <= last + ROW_SEC * 2:
                lines.append(f"子のプロセス: 本体と一緒に {_hm(beats[-1]['t'])} ごろ止まった（本体を止めると子も止まる）")
            else:
                lines.append(f"子のプロセス: {_hm(beats[-1]['t'])} まで動いた（本体はそのあいだ動いていた）")
    return lines


def report(probe_dir: Path = PROBE_DIR, out: Callable[[str], None] = print) -> int:
    files = sorted(p for p in probe_dir.glob("*.jsonl") if not p.name.endswith(".child.jsonl")) if probe_dir.is_dir() else []
    if not files:
        out(f"記録がありません（{probe_dir}）")
        return 1
    for path in files:
        out(f"== {path.name}")
        child = path.with_name(path.stem + ".child.jsonl")
        for line in summarize(_read(path), _read(child) if child.is_file() else None):
            out("  " + line)
    return 0


# ───────────────────────── 入口 ─────────────────────────


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="RDP の外でマイクを使えるかを測る（ADR-0073 段階 0）")
    sub = parser.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="録音しながら 5 秒ごとに記録する")
    r.add_argument("--seconds", type=float, default=3600.0)
    r.add_argument("--no-child", action="store_true", help="子のプロセスを起動しない")
    c = sub.add_parser("child", help="（内部用）子のプロセス")
    c.add_argument("--seconds", type=float, default=3600.0)
    c.add_argument("--parent", type=int, required=True)
    c.add_argument("--out", required=True)
    sub.add_parser("devices", help="（内部用）マイクの一覧を JSON で 1 行")
    sub.add_parser("report", help="記録をまとめて表示")
    args = parser.parse_args(argv)
    if args.cmd == "devices":
        print(json.dumps(device_summary(), ensure_ascii=False))
        return 0
    if args.cmd == "report":
        return report()
    if args.cmd == "child":
        run_child(args.seconds, Path(args.out), args.parent)
        return 0
    out = PROBE_DIR / (datetime.now().strftime("%Y%m%d_%H%M%S") + ".jsonl")
    child_out = out.with_name(out.stem + ".child.jsonl")

    def spawn():
        return subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "child", "--seconds",
                                 str(args.seconds + 600), "--parent", str(os.getpid()), "--out", str(child_out)],
                                cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, **_no_window())

    try:
        rows = run_probe(args.seconds, out, mic=Mic(_audio_cfg()), child=None if args.no_child else spawn)
    except Exception:  # noqa: BLE001 — pythonw では見えないので記録に残す
        _append(out, {"t": time.time(), "kind": "crash", "error": traceback.format_exc()[-1500:]})
        return 1
    if sys.stdout is not None:
        print(f"{rows} 行を書きました: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
