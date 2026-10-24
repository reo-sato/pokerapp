"""tools/session_setup_ui.py

起動時の卓の設定画面（PC のブラウザ, ADR-0071）。ハンドロガー（`main.py --cli` / GUI）が起動すると開き、「始める」を
押すまで待つ。端末で一つずつ打つ代わり（オーナー 2026-10-24「cli に全部手打ちするのめんどくさい」）。

- 項目: モード（リングゲーム / トーナメント）、リングゲームは SB / BB、トーナメントはストラクチャー・始めるレベル・
  そのレベルの経過分（途中から再開）、使う席・名前・持ち点（一括 + 席ごと）、1 ハンド目のボタン席。
- **前回の設定を覚える**: 「始める」で `logs/last_session_setup.json` に保存し、次はその値で開く。トーナメントの時計は
  ロガーが `logs/tournament_clock.json` に保存するので、起動し直しても「前回の時計から続ける」で同じレベル・経過から。
- 同じ画面で**ストラクチャーの編集**（`core/blind_structure.BlindStructureStore`）と、始めたあとの**時計**（一時停止・
  次のレベルなど。ロガーの queue に `clock` の入力として積む = CLI の `clock` と同じ経路）。
- 待ち受けは `127.0.0.1` だけ（店舗 PC は RDP の中のブラウザで開く）。番号が使われていれば次の番号。

検証（`build_session_config`）は端末の入力（`main._prompt_session_config`）と同じ規則: 席は 1〜9 の 2 席以上・持ち点は
1 以上・SB ≤ BB・ボタンは使う席。
"""
from __future__ import annotations

import json
import logging
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.atomic_io import atomic_write_json  # noqa: E402
from core.blind_structure import (  # noqa: E402
    BlindStructureStore,
    StructureValidationError,
    TournamentClock,
    format_blinds,
)

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8794
LAST_SETUP_FILE = "last_session_setup.json"
CLOCK_FILE = "tournament_clock.json"
# 前回の時計から続けられるのは、保存からこの時間まで（前の日の時計を続けない）
CLOCK_RESUME_MAX_SEC = 16 * 3600
_MAX_BODY = 256 * 1024
_MAX_NAME = 40

DEFAULT_SETUP: dict = {
    "mode": "ring",
    "seats": [1, 2, 3, 4, 5, 6],
    "names": {},
    "stacks": {},
    "default_stack": 20000,
    "sb": 100, "bb": 200,
    "structure": "",
    "level": 1,
    "elapsed_min": 0,
    "button": None,
    "log_dir": "./logs",
}


class SetupError(ValueError):
    def __init__(self, errors: list[dict]) -> None:
        super().__init__("; ".join(e["message"] for e in errors))
        self.errors = errors


def _int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().translate(_FULLWIDTH).lstrip("-").isdigit():
        return int(value.strip().translate(_FULLWIDTH))
    return None


_FULLWIDTH = str.maketrans("０１２３４５６７８９", "0123456789")


def build_session_config(raw: dict, store: BlindStructureStore, now: Optional[float] = None) -> dict:
    """画面の入力を検証して、ロガーの卓の設定にする（`main._prompt_session_config` の戻り値 + `table`）。

    違反は `SetupError`（`errors` = [{"field", "message"}]、画面がその項目の横に出す）。
    """
    errors: list[dict] = []

    def err(field: str, message: str) -> None:
        errors.append({"field": field, "message": message})

    mode = raw.get("mode")
    if mode not in ("ring", "tournament"):
        err("mode", "モードを選んでください")
    seats_raw = raw.get("seats") or []
    seats = sorted({s for s in (_int(x) for x in seats_raw) if s is not None})
    if len(seats) < 2 or len(seats) != len(seats_raw) or not all(1 <= s <= 9 for s in seats):
        err("seats", "使う席を 2 つ以上選んでください（1〜9）")
    names = raw.get("names") or {}
    stacks = raw.get("stacks") or {}
    default_stack = _int(raw.get("default_stack"))
    players = []
    for seat in seats:
        name = str(names.get(str(seat)) or "").strip()
        if len(name) > _MAX_NAME:
            err(f"name_{seat}", f"席{seat}: 名前は {_MAX_NAME} 文字までです")
        stack = _int(stacks.get(str(seat)))
        if stack is None:
            stack = default_stack
        if stack is None or stack <= 0:
            err(f"stack_{seat}", f"席{seat}: 持ち点は 1 以上の整数にしてください")
            stack = 0
        players.append({"seat": seat, "name": name or f"Player{seat}", "stack": stack, "named": bool(name)})

    table: dict = {"mode": mode}
    sb = bb = 0
    if mode == "ring":
        sb, bb = _int(raw.get("sb")), _int(raw.get("bb"))
        if sb is None or sb <= 0:
            err("sb", "SB は 1 以上の整数にしてください")
        if bb is None or bb <= 0:
            err("bb", "BB は 1 以上の整数にしてください")
        elif sb is not None and sb > bb:
            err("sb", "SB は BB 以下にしてください")
        table.update({"sb": sb, "bb": bb, "ante": 0})
    elif mode == "tournament":
        structure = store.get(str(raw.get("structure") or ""))
        if structure is None:
            err("structure", "ストラクチャーを選んでください（無ければ「ストラクチャーの編集」で作ります）")
        else:
            level = _int(raw.get("level"))
            try:
                elapsed_min = float(raw.get("elapsed_min") or 0)
            except (TypeError, ValueError):
                elapsed_min = -1
            if level is None or structure.playing_index(level) is None:
                err("level", "始めるレベルを選んでください")
            elif elapsed_min < 0:
                err("elapsed_min", "経過分は 0 以上にしてください")
            else:
                clock = TournamentClock.start(structure, now if now is not None else time.time(),
                                              level=level, elapsed_sec=elapsed_min * 60)
                lv = clock.blinds
                sb, bb = lv.sb, lv.bb
                table.update({"sb": sb, "bb": bb, "ante": lv.ante, "structure": structure.to_dict(),
                              "level": level, "elapsed_min": elapsed_min})

    button = _int(raw.get("button"))
    if button is None and seats:
        button = seats[-1]
    if seats and button not in seats:
        err("button", "1 ハンド目のボタン席は、使う席のどれかにしてください")
    log_dir = str(raw.get("log_dir") or "./logs").strip() or "./logs"
    if errors:
        raise SetupError(errors)
    # 内部表現は「次の new_hand で 1 つ進める前の席」（`main._prompt_session_config` と同じ）
    prior = seats[(seats.index(button) - 1) % len(seats)]
    return {"players": players, "sb": sb, "bb": bb, "button_seat": prior, "first_button": button,
            "log_dir": log_dir, "table": table}


def load_last_setup(log_dir: Path) -> dict:
    """前回の設定（無ければ既定）。"""
    try:
        data = json.loads((Path(log_dir) / LAST_SETUP_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULT_SETUP)
    return {**DEFAULT_SETUP, **data} if isinstance(data, dict) else dict(DEFAULT_SETUP)


def save_last_setup(log_dir: Path, raw: dict) -> None:
    keep = {k: raw.get(k) for k in DEFAULT_SETUP}
    try:
        atomic_write_json(Path(log_dir) / LAST_SETUP_FILE, keep)
    except OSError:
        logger.exception("前回の設定を保存できませんでした")


def save_clock(log_dir: Path, session_id: str, clock: TournamentClock, now: Optional[float] = None) -> None:
    """時計の状態を保存する（`logs/<sid>.tournament.json` と、起動し直したとき用の `logs/tournament_clock.json`）。"""
    data = {**clock.to_dict(), "session_id": session_id, "saved_at": now if now is not None else time.time()}
    for name in (f"{session_id}.tournament.json", CLOCK_FILE):
        try:
            atomic_write_json(Path(log_dir) / name, data)
        except OSError:
            logger.exception("時計を保存できませんでした: %s", name)


def resumable_clock(log_dir: Path, now: Optional[float] = None) -> Optional[dict]:
    """前回の時計を続けたときの、いまのレベルと経過分（無い・古い・読めなければ None）。"""
    now = time.time() if now is None else now
    try:
        data = json.loads((Path(log_dir) / CLOCK_FILE).read_text(encoding="utf-8"))
        if now - float(data.get("saved_at") or 0) > CLOCK_RESUME_MAX_SEC:
            return None
        clock = TournamentClock.from_dict(data)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, StructureValidationError):
        return None
    clock.tick(now)
    lv = clock.blinds
    # ブレイク中は、その前のレベルの始めからの経過（始めると時計がブレイクまで進む）
    start = clock.structure.playing_index(lv.level)
    elapsed = clock.elapsed(now) + sum(x.duration_sec for x in clock.structure.levels[start:clock.index])
    return {"structure": clock.structure.name, "level": lv.level,
            "elapsed_min": round(elapsed / 60, 1),
            "paused": clock.paused, "is_break": clock.current.is_break,
            "text": f"{clock.structure.name} {clock.current.label}"
                    f"（{format_blinds(lv.sb, lv.bb, lv.ante)}）{int(clock.elapsed(now) // 60)} 分経過"
                    + ("・止めていた" if clock.paused else "")}


def clock_status(log_dir: Path, session_id: Optional[str], now: Optional[float] = None) -> Optional[dict]:
    """始めたセッションの時計の表示（ロガーが保存した状態から数える）。"""
    if not session_id:
        return None
    try:
        data = json.loads((Path(log_dir) / f"{session_id}.tournament.json").read_text(encoding="utf-8"))
        clock = TournamentClock.from_dict(data)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, StructureValidationError):
        return None
    now = time.time() if now is None else now
    status = clock.status(now)
    status["remaining_sec"] = round(clock.remaining(now), 1)
    status["levels"] = [lv.to_dict() for lv in clock.structure.levels]
    return status


class SetupApp:
    """設定画面の状態（ロガーの main スレッドが `wait` で待つ）。"""

    def __init__(self, store: BlindStructureStore, log_dir: Path) -> None:
        self.store = store
        self.log_dir = Path(log_dir)
        self.result: Optional[dict] = None
        self.done = threading.Event()
        self.session_id: Optional[str] = None
        self._send_clock: Optional[Callable[[str], None]] = None

    def attach_session(self, session_id: str, send_clock: Optional[Callable[[str], None]]) -> None:
        """始めたあと: 時計の表示と操作をつなぐ（操作はロガーの queue に積む関数）。"""
        self.session_id = session_id
        self._send_clock = send_clock

    def setup_payload(self) -> dict:
        last = load_last_setup(self.log_dir)
        return {
            "last": last,
            "structures": [s.to_dict() for s in self.store.list_structures()],
            "resume": resumable_clock(self.log_dir),
            "started": self.done.is_set(),
            "session_id": self.session_id,
        }

    def start(self, raw: dict) -> dict:
        if self.done.is_set():
            raise SetupError([{"field": "", "message": "もう始めています"}])
        config = build_session_config(raw, self.store)
        save_last_setup(Path(config["log_dir"]), raw)
        self.result = config
        self.done.set()
        return config

    def clock(self, cmd: str) -> None:
        if self._send_clock is None:
            raise SetupError([{"field": "", "message": "時計はまだありません（トーナメントで始めたあとに使えます）"}])
        self._send_clock(cmd)


class SetupServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, address: tuple[str, int], app: SetupApp) -> None:
        self.app = app
        super().__init__(address, _Handler)


class _Handler(BaseHTTPRequestHandler):
    server: SetupServer
    server_version = "SessionSetup/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:      # noqa: A003
        logger.debug("%s - " + fmt, self.address_string(), *args)

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: Any) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _body(self) -> Any:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length <= 0 or length > _MAX_BODY:
            return None
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None

    def do_GET(self) -> None:          # noqa: N802
        path = urlsplit(self.path).path
        app = self.server.app
        if path in ("/", "/index.html"):
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/setup":
            self._json(200, app.setup_payload())
        elif path == "/api/clock":
            status = clock_status(app.log_dir if app.result is None else Path(app.result["log_dir"]), app.session_id)
            self._json(200, {"clock": status, "session_id": app.session_id})
        else:
            self._json(404, {"message": "not found"})

    def do_POST(self) -> None:         # noqa: N802
        path = urlsplit(self.path).path
        app = self.server.app
        body = self._body()
        if not isinstance(body, dict):
            self._json(400, {"errors": [{"field": "", "message": "入力を読めません"}]})
            return
        try:
            if path == "/api/start":
                config = app.start(body)
                self._json(200, {"ok": True, "table": config["table"]})
            elif path == "/api/structures":
                saved = app.store.save(body.get("structure") or {}, previous_name=body.get("previous_name") or None)
                self._json(200, {"ok": True, "structure": saved.to_dict(),
                                 "structures": [s.to_dict() for s in app.store.list_structures()]})
            elif path == "/api/structures/delete":
                app.store.delete(str(body.get("name") or ""))
                self._json(200, {"ok": True, "structures": [s.to_dict() for s in app.store.list_structures()]})
            elif path == "/api/clock":
                app.clock(str(body.get("cmd") or "status"))
                self._json(200, {"ok": True})
            else:
                self._json(404, {"errors": [{"field": "", "message": "not found"}]})
        except SetupError as e:
            self._json(400, {"errors": e.errors})
        except StructureValidationError as e:
            self._json(400, {"errors": [{"field": "structure", "message": str(e)}]})
        except OSError as e:
            self._json(500, {"errors": [{"field": "", "message": f"保存できません: {e}"}]})


def start_server(app: SetupApp, host: str = "127.0.0.1", port: int = DEFAULT_PORT,
                 tries: int = 10) -> SetupServer:
    """待ち受けを始める（番号が使われていれば次の番号）。"""
    last_error: Optional[OSError] = None
    for p in range(port, port + tries):
        try:
            server = SetupServer((host, p), app)
        except OSError as e:
            last_error = e
            continue
        threading.Thread(target=server.serve_forever, name="SessionSetupUI", daemon=True).start()
        return server
    raise OSError(f"設定画面の待ち受けを始められません（{port}〜{port + tries - 1}）: {last_error}")


def _typed_line() -> Optional[str]:
    """端末に 1 行打たれていれば返す（待たない）。Windows は msvcrt、それ以外は select。"""
    if sys.platform == "win32":
        import msvcrt

        buf = getattr(_typed_line, "_buf", "")
        while msvcrt.kbhit():
            ch = msvcrt.getwch()
            if ch in ("\r", "\n"):
                print()
                _typed_line._buf = ""          # type: ignore[attr-defined]
                return buf
            if ch == "\b":
                buf = buf[:-1]
                print("\b \b", end="", flush=True)
            else:
                buf += ch
                print(ch, end="", flush=True)
        _typed_line._buf = buf                 # type: ignore[attr-defined]
        return None
    import select

    try:
        ready, _, _ = select.select([sys.stdin], [], [], 0)
    except (OSError, ValueError):
        return None
    return sys.stdin.readline().strip() if ready else None


def wait_for_setup(app: SetupApp, url: str, *, open_browser: bool = True,
                   poll_sec: float = 0.2) -> Optional[dict]:
    """ブラウザの「始める」を待つ。端末で `t` を打てば None（端末の入力で設定する）。"""
    print(f"卓の設定はブラウザの画面で: {url}  （端末で入力するなら t と Enter）", flush=True)
    if open_browser:
        try:
            import webbrowser

            webbrowser.open(url)
        except Exception:  # noqa: BLE001 — 開けなくても URL は出している
            logger.exception("ブラウザを開けませんでした")
    while not app.done.wait(poll_sec):
        line = _typed_line()
        if line is not None and line.strip().lower() == "t":
            return None
    return app.result


PAGE = r"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>卓の設定</title>
<style>
 :root { --bg:#f6f7f9; --panel:#fff; --line:#d6d9de; --text:#1d2228; --muted:#68707a; --accent:#1f6feb; --err:#c62828; --ok:#1b7f3b; }
 * { box-sizing:border-box; }
 body { margin:0; background:var(--bg); color:var(--text); font:15px/1.5 system-ui,-apple-system,"Hiragino Sans","Yu Gothic UI",sans-serif; }
 main { max-width:880px; margin:0 auto; padding:16px; }
 h1 { font-size:20px; margin:4px 0 12px; }
 nav { display:flex; gap:6px; margin-bottom:12px; flex-wrap:wrap; }
 nav button { border:1px solid var(--line); background:var(--panel); padding:8px 14px; border-radius:8px; cursor:pointer; font-size:15px; }
 nav button.on { background:var(--accent); color:#fff; border-color:var(--accent); }
 section { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:14px 16px; margin-bottom:12px; }
 h2 { font-size:16px; margin:0 0 10px; }
 label { display:inline-flex; align-items:center; gap:6px; margin-right:14px; }
 input[type=number], input[type=text], select { font-size:15px; padding:6px 8px; border:1px solid var(--line); border-radius:6px; }
 input[type=number] { width:110px; }
 .seatbtns { display:flex; gap:6px; flex-wrap:wrap; }
 .seatbtns button { width:46px; height:42px; font-size:17px; border:1px solid var(--line); border-radius:8px; background:#fff; cursor:pointer; }
 .seatbtns button.on { background:var(--accent); color:#fff; border-color:var(--accent); }
 table { border-collapse:collapse; width:100%; }
 td, th { padding:4px 6px; text-align:left; border-bottom:1px solid #eceef1; }
 th { color:var(--muted); font-weight:500; font-size:13px; }
 .err { color:var(--err); font-size:13px; display:block; }
 .muted { color:var(--muted); font-size:13px; }
 .big { font-size:17px; padding:10px 26px; border-radius:8px; border:0; background:var(--accent); color:#fff; cursor:pointer; }
 .big:disabled { background:#9aa4b1; }
 .small { padding:4px 10px; border:1px solid var(--line); background:#fff; border-radius:6px; cursor:pointer; }
 .ok { color:var(--ok); }
 .clock { font-size:44px; font-weight:700; font-variant-numeric:tabular-nums; }
 .row { display:flex; gap:12px; align-items:center; flex-wrap:wrap; margin:6px 0; }
 tr.brk td { background:#f1f4f8; color:var(--muted); }
 tr.cur td { background:#e7f0ff; }
</style></head>
<body><main>
<h1>卓の設定</h1>
<nav>
 <button data-tab="setup" class="on">卓の設定</button>
 <button data-tab="edit">ストラクチャーの編集</button>
 <button data-tab="clock">時計</button>
</nav>
<div id="tab-setup"></div>
<div id="tab-edit" hidden></div>
<div id="tab-clock" hidden></div>
</main>
<script>
"use strict";
let S = null;             // 画面の入力
let structures = [];
let resume = null;
let started = false;
let errors = {};
let editing = null;       // 編集中のストラクチャー
let editPrev = null;
let editErr = "";
let editOk = "";

const $ = (sel, root=document) => root.querySelector(sel);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const fmt = (sb, bb, ante) => `${sb}/${bb}` + (ante ? `（アンテ ${ante}）` : "");

async function api(path, body) {
  const opt = body === undefined ? {} : {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)};
  const res = await fetch(path, opt);
  const data = await res.json().catch(() => ({}));
  return {ok: res.ok, data};
}

async function load() {
  const {data} = await api("/api/setup");
  S = JSON.parse(JSON.stringify(data.last));
  S.names = S.names || {}; S.stacks = S.stacks || {};
  structures = data.structures || [];
  resume = data.resume;
  started = data.started;
  if (S.mode === "tournament" && !structures.some(s => s.name === S.structure) && structures.length) S.structure = structures[0].name;
  renderSetup(); renderEdit(); renderClock();
}

function structure() { return structures.find(s => s.name === S.structure); }
function playing(st) { let n = 0; return (st?.levels || []).map(lv => lv.break ? {...lv, level:null} : {...lv, level:++n}); }
function errHtml(field) { return errors[field] ? `<span class="err">${esc(errors[field])}</span>` : ""; }

function renderSetup() {
  const root = $("#tab-setup");
  if (S.mode === "tournament" && !structure() && structures.length) { S.structure = structures[0].name; S.level = 1; }
  const seats = S.seats || [];
  const st = structure();
  const levels = playing(st);
  const button = seats.includes(S.button) ? S.button : seats[seats.length - 1];
  root.innerHTML = `
  ${started ? `<section><b class="ok">セッションを始めました。</b> <span class="muted">この画面は「時計」のタブで時計の操作に使えます。</span></section>` : ""}
  <section><h2>モード</h2>
    <label><input type="radio" name="mode" value="ring" ${S.mode==="ring"?"checked":""}> リングゲーム</label>
    <label><input type="radio" name="mode" value="tournament" ${S.mode==="tournament"?"checked":""}> トーナメント</label>
    ${errHtml("mode")}
  </section>
  ${S.mode === "ring" ? `
  <section><h2>ブラインド</h2>
    <label>SB <input type="number" id="sb" min="1" value="${esc(S.sb)}"></label>
    <label>BB <input type="number" id="bb" min="1" value="${esc(S.bb)}"></label>
    ${errHtml("sb")}${errHtml("bb")}
    <div class="muted">リングゲームはアンテなし。途中で変えるときはロガーの端末で blinds &lt;SB&gt; &lt;BB&gt;。</div>
  </section>` : `
  <section><h2>ストラクチャー</h2>
    ${structures.length ? `<label><select id="structure">${structures.map(s =>
      `<option ${s.name===S.structure?"selected":""}>${esc(s.name)}</option>`).join("")}</select></label>` :
      `<span class="err">ストラクチャーがありません。「ストラクチャーの編集」で作ってください。</span>`}
    ${errHtml("structure")}
    ${st ? `<div class="row">
      <label>始めるレベル <select id="level">${levels.filter(l => !l.break).map(l =>
        `<option value="${l.level}" ${l.level==S.level?"selected":""}>レベル ${l.level}（${fmt(l.sb,l.bb,l.ante)}）</option>`).join("")}</select></label>
      <label>そのレベルの経過 <input type="number" id="elapsed" min="0" step="0.5" value="${esc(S.elapsed_min||0)}"> 分</label>
      ${errHtml("level")}${errHtml("elapsed_min")}</div>` : ""}
    ${resume ? `<div class="row"><button class="small" id="resume">前回の時計から続ける</button>
      <span class="muted">${esc(resume.text)}</span></div>` : ""}
    ${st ? `<table><tr><th>レベル</th><th>SB/BB</th><th>アンテ</th><th>分</th></tr>${levels.map(l => l.break ?
      `<tr class="brk"><td>ブレイク</td><td></td><td></td><td>${l.minutes}</td></tr>` :
      `<tr class="${l.level==S.level?"cur":""}"><td>${l.level}</td><td>${l.sb}/${l.bb}</td><td>${l.ante||"—"}</td><td>${l.minutes}</td></tr>`).join("")}</table>` : ""}
  </section>`}
  <section><h2>席（使う席を押す）</h2>
    <div class="seatbtns">${[1,2,3,4,5,6,7,8,9].map(n =>
      `<button data-seat="${n}" class="${seats.includes(n)?"on":""}">${n}</button>`).join("")}</div>
    ${errHtml("seats")}
    <div class="row"><label>持ち点（一括） <input type="number" id="default_stack" min="1" value="${esc(S.default_stack)}"></label>
      <button class="small" id="apply_stack">全席にこの持ち点</button>
      ${st && st.starting_stack ? `<span class="muted">ストラクチャーの始めの持ち点 ${st.starting_stack}</span>` : ""}</div>
    <table><tr><th>席</th><th>名前（空 = PlayerN）</th><th>持ち点（空 = 一括の値）</th></tr>
    ${seats.map(n => `<tr><td>${n}</td>
      <td><input type="text" data-name="${n}" value="${esc(S.names[n]||"")}" maxlength="40">${errHtml("name_"+n)}</td>
      <td><input type="number" data-stack="${n}" min="1" value="${esc(S.stacks[n]??"")}">${errHtml("stack_"+n)}</td></tr>`).join("")}
    </table>
  </section>
  <section><h2>1 ハンド目のボタン席</h2>
    <select id="button">${seats.map(n => `<option ${n===button?"selected":""}>${n}</option>`).join("")}</select>
    ${errHtml("button")}
    <span class="muted">以降はハンドごとに回ります。</span>
  </section>
  <section>
    <button class="big" id="start" ${started?"disabled":""}>始める</button>
    <span id="start_msg" class="err">${esc(errors[""]||"")}</span>
  </section>`;

  root.querySelectorAll('input[name=mode]').forEach(el => el.onchange = () => {
    S.mode = el.value;
    if (S.mode === "tournament" && !structure() && structures.length) S.structure = structures[0].name;
    renderSetup();
  });
  const bind = (id, key, num=true) => { const el = $("#"+id, root); if (el) el.oninput = () => { S[key] = num ? el.value : el.value; }; };
  bind("sb", "sb"); bind("bb", "bb"); bind("elapsed", "elapsed_min"); bind("default_stack", "default_stack");
  const sel = $("#structure", root); if (sel) sel.onchange = () => { S.structure = sel.value; S.level = 1; S.elapsed_min = 0;
    const s = structure(); if (s && s.starting_stack) S.default_stack = s.starting_stack; renderSetup(); };
  const lvl = $("#level", root); if (lvl) lvl.onchange = () => { S.level = Number(lvl.value); renderSetup(); };
  const btn = $("#button", root); btn.onchange = () => { S.button = Number(btn.value); };
  root.querySelectorAll("[data-seat]").forEach(el => el.onclick = () => {
    const n = Number(el.dataset.seat);
    S.seats = S.seats.includes(n) ? S.seats.filter(x => x !== n) : [...S.seats, n].sort((a,b) => a-b);
    renderSetup();
  });
  root.querySelectorAll("[data-name]").forEach(el => el.oninput = () => { S.names[el.dataset.name] = el.value; });
  root.querySelectorAll("[data-stack]").forEach(el => el.oninput = () => { S.stacks[el.dataset.stack] = el.value; });
  const ap = $("#apply_stack", root); ap.onclick = () => { S.stacks = {}; renderSetup(); };
  const rs = $("#resume", root); if (rs) rs.onclick = () => {
    S.structure = resume.structure; S.level = resume.level; S.elapsed_min = resume.elapsed_min; renderSetup(); };
  $("#start", root).onclick = start;
}

async function start() {
  if (S.button == null || !S.seats.includes(S.button)) S.button = S.seats[S.seats.length - 1];
  const {ok, data} = await api("/api/start", S);
  errors = {};
  if (!ok) { (data.errors || []).forEach(e => { errors[e.field] = e.message; }); renderSetup(); return; }
  started = true; renderSetup(); renderClock();
}

// ――― ストラクチャーの編集 ―――

function blankStructure() { return {name:"", starting_stack:20000, levels:[{sb:100, bb:200, ante:0, minutes:20}]}; }

function renderEdit() {
  const root = $("#tab-edit");
  if (!editing) { editing = structures[0] ? JSON.parse(JSON.stringify(structures[0])) : blankStructure(); editPrev = structures[0]?.name || null; }
  let n = 0;
  root.innerHTML = `<section>
    <div class="row"><select id="pick"><option value="">（新しく作る）</option>${structures.map(s =>
      `<option ${s.name===editPrev?"selected":""}>${esc(s.name)}</option>`).join("")}</select></div>
    <div class="row"><label>名前 <input type="text" id="ename" maxlength="60" value="${esc(editing.name)}"></label>
      <label>始めの持ち点 <input type="number" id="estack" min="1" value="${esc(editing.starting_stack ?? "")}"></label></div>
    <table><tr><th>レベル</th><th>SB</th><th>BB</th><th>アンテ（BB の人, 0 = なし）</th><th>分</th><th></th></tr>
    ${editing.levels.map((l, i) => l.break ? `<tr class="brk"><td>ブレイク</td><td></td><td></td><td></td>
      <td><input type="number" data-i="${i}" data-k="minutes" min="1" value="${esc(l.minutes)}"></td><td>${rowBtns(i)}</td></tr>` :
      `<tr><td>${++n}</td>${["sb","bb","ante","minutes"].map(k =>
      `<td><input type="number" data-i="${i}" data-k="${k}" min="${k==="ante"?0:1}" value="${esc(l[k] ?? (k==="ante"?0:""))}"></td>`).join("")}
      <td>${rowBtns(i)}</td></tr>`).join("")}
    </table>
    <div class="row"><button class="small" id="addlv">＋ レベル</button><button class="small" id="addbr">＋ ブレイク</button></div>
    <div class="row"><button class="big" id="esave">保存</button>
      ${editPrev ? `<button class="small" id="ecopy">複製して新しく作る</button>
        <button class="small" id="edel">このストラクチャーを消す</button>` : ""}
      <span class="err">${esc(editErr)}</span><span class="ok">${esc(editOk)}</span></div>
    <div class="muted">名前を変えて保存すると名前が変わります（元のまま残すときは「複製して新しく作る」）。</div>
  </section>`;
  const pick = $("#pick", root); pick.onchange = () => {
    const s = structures.find(x => x.name === pick.value);
    editing = s ? JSON.parse(JSON.stringify(s)) : blankStructure(); editPrev = s ? s.name : null; editErr = editOk = ""; renderEdit(); };
  $("#ename", root).oninput = e => { editing.name = e.target.value; };
  $("#estack", root).oninput = e => { editing.starting_stack = e.target.value === "" ? null : Number(e.target.value); };
  root.querySelectorAll("input[data-i]").forEach(el => el.oninput = () => {
    editing.levels[Number(el.dataset.i)][el.dataset.k] = el.value === "" ? "" : Number(el.value); });
  root.querySelectorAll("button[data-op]").forEach(el => el.onclick = () => {
    const i = Number(el.dataset.i), L = editing.levels;
    if (el.dataset.op === "up" && i > 0) [L[i-1], L[i]] = [L[i], L[i-1]];
    if (el.dataset.op === "down" && i < L.length - 1) [L[i+1], L[i]] = [L[i], L[i+1]];
    if (el.dataset.op === "del") L.splice(i, 1);
    renderEdit(); });
  $("#addlv", root).onclick = () => {
    const last = [...editing.levels].reverse().find(l => !l.break) || {sb:50, bb:100, minutes:20};
    editing.levels.push({sb:last.sb*2, bb:last.bb*2, ante:last.ante ? last.bb*2 : 0, minutes:last.minutes}); renderEdit(); };
  $("#addbr", root).onclick = () => { editing.levels.push({break:true, minutes:10}); renderEdit(); };
  $("#esave", root).onclick = async () => {
    const {ok, data} = await api("/api/structures", {structure: editing, previous_name: editPrev});
    if (!ok) { editErr = (data.errors || []).map(e => e.message).join(" / "); editOk = ""; renderEdit(); return; }
    if (S.structure === editPrev) S.structure = data.structure.name;
    structures = data.structures; editing = JSON.parse(JSON.stringify(data.structure)); editPrev = editing.name;
    editErr = ""; editOk = "保存しました";
    renderEdit(); if (!started) renderSetup(); };
  const cp = $("#ecopy", root); if (cp) cp.onclick = () => {
    editing = {...JSON.parse(JSON.stringify(editing)), name: editing.name + "（コピー）"}; editPrev = null;
    editErr = ""; editOk = "複製しました。名前を変えて保存してください"; renderEdit(); };
  const del = $("#edel", root); if (del) del.onclick = async () => {
    if (!confirm(`「${editPrev}」を消しますか？`)) return;
    const {data} = await api("/api/structures/delete", {name: editPrev});
    structures = data.structures || []; editing = null; editErr = editOk = ""; renderEdit(); if (!started) renderSetup(); };
}
function rowBtns(i) { return `<button class="small" data-op="up" data-i="${i}">↑</button> <button class="small" data-op="down" data-i="${i}">↓</button> <button class="small" data-op="del" data-i="${i}">✕</button>`; }

// ――― 時計 ―――

let clockData = null, clockFetched = 0;
function mmss(sec) { sec = Math.max(0, Math.floor(sec)); return `${Math.floor(sec/60)}:${String(sec%60).padStart(2,"0")}`; }

async function pollClock() {
  const {data} = await api("/api/clock");
  clockData = data.clock; clockFetched = Date.now();
  renderClock();
}
function renderClock() {
  const root = $("#tab-clock");
  if (!started) { root.innerHTML = `<section class="muted">「始める」のあとに使えます（トーナメントのとき）。</section>`; return; }
  const c = clockData;
  if (!c) { root.innerHTML = `<section class="muted">トーナメントの時計はありません（リングゲーム）。</section>`; return; }
  root.innerHTML = `<section>
    <div class="muted">${esc(c.structure)}</div>
    <div class="row"><b style="font-size:20px">${esc(c.label)}</b> <span style="font-size:20px">${fmt(c.sb, c.bb, c.ante)}</span></div>
    <div class="clock"><span id="clockval">${mmss(remainingNow())}</span>${c.paused ? ' <span class="muted" style="font-size:18px">止まっています</span>' : ""}</div>
    <div class="muted">${c.next ? `次: レベル ${c.next.level}（${fmt(c.next.sb, c.next.bb, c.next.ante)}）` : "最後のレベル"}</div>
    <div class="row">
      ${c.paused ? `<button class="small" data-cmd="resume">再開</button>` : `<button class="small" data-cmd="pause">一時停止</button>`}
      <button class="small" data-cmd="prev">前のレベル</button><button class="small" data-cmd="next">次のレベル</button>
      <label>レベル <input type="number" id="setlv" min="1" style="width:70px"></label>
      <label>経過 <input type="number" id="setmin" min="0" step="0.5" style="width:70px"> 分</label>
      <button class="small" id="setgo">移す</button>
    </div>
    <div class="muted">ブラインドは次のハンドから変わります（ハンドの途中では変えません）。</div>
  </section>`;
  root.querySelectorAll("[data-cmd]").forEach(el => el.onclick = () => clockCmd(el.dataset.cmd));
  $("#setgo", root).onclick = () => { const lv = $("#setlv", root).value; if (!lv) return;
    clockCmd(`set ${lv} ${$("#setmin", root).value || 0}`); };
}
function remainingNow() {
  const c = clockData; if (!c) return 0;
  return c.paused ? c.remaining_sec : c.remaining_sec - (Date.now() - clockFetched) / 1000;
}
async function clockCmd(cmd) { await api("/api/clock", {cmd}); setTimeout(pollClock, 400); }

document.querySelectorAll("nav button").forEach(b => b.onclick = () => {
  document.querySelectorAll("nav button").forEach(x => x.classList.toggle("on", x === b));
  ["setup","edit","clock"].forEach(t => { $("#tab-"+t).hidden = t !== b.dataset.tab; });
  if (b.dataset.tab === "clock") pollClock();
});
setInterval(() => {
  if ($("#tab-clock").hidden || !started) return;
  if (Date.now() - clockFetched > 5000 && !document.activeElement?.closest?.("#tab-clock")) { pollClock(); return; }
  const el = $("#clockval"); if (el) el.textContent = mmss(remainingNow());
}, 1000);
load();
</script></body></html>
"""
