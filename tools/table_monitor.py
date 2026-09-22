"""tools/table_monitor.py

卓状態モニタ — **RFID だけから導いた卓の状態**を LAN 上のブラウザ（iPad / スマホ）に表示する。

実プレイ環境での検証用（ADR-0056 D5）:

- **カード読み取り** — 各席の札とボードが読めているか。
- **有効席** — 誰に札が配られ、いま誰の札が卓上にあるか。**「載っている」と「ゲームに残っている」は
  別物**なので、配布 / 在否 / 離席秒数 / fold らしさを分けて出す（ADR-0056 D4）。
- **ストリート遷移** — ボード枚数から導いたストリート（engine のストリートと並べて表示）。
- **反映遅延** — 観測時刻とページ描画時刻の差を画面に出す。

hand logger（`main.py --cli` / GUI）が書く `logs/{session}.table_state.json` を読むだけで、
ゲーム状態は一切触らない（単一書き手 + reload-on-read, ADR-0020 の型）。

使い方:

    python tools/table_monitor.py                       # 最新セッションを自動選択
    python tools/table_monitor.py --port 8790 --host 0.0.0.0   # iPad から見る場合
    python tools/table_monitor.py --session 2026-09-12_140022_session1
    python tools/table_monitor.py --once                # 1 回だけ端末に表示して終了

ブラウザで `http://<PC の IP>:8790/` を開く。1 秒ごとに自動更新する。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional

# リポジトリ直下を import path に入れる（他の tools/ と同じ規約）。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_PAGE = """<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>卓状態モニタ</title>
<style>
 :root { color-scheme: dark; }
 body { margin:0; padding:16px; font:16px/1.5 system-ui,-apple-system,"Hiragino Sans",sans-serif;
        background:#12151a; color:#e8eaed; }
 h1 { font-size:18px; margin:0 0 4px; }
 .meta { color:#9aa0a6; font-size:13px; margin-bottom:16px; }
 .lag { font-weight:700; }
 .lag.ok { color:#7ee787; } .lag.warn { color:#e3b341; } .lag.bad { color:#ff7b72; }
 .board { display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin:8px 0 20px; }
 .card { background:#1e232b; border:1px solid #3a414d; border-radius:8px; padding:10px 12px;
         font-size:20px; font-weight:700; min-width:46px; text-align:center; }
 .card.red { color:#ff7b72; }
 .street { font-size:22px; font-weight:700; margin-right:12px; }
 .seats { display:grid; grid-template-columns:repeat(auto-fill,minmax(180px,1fr)); gap:10px; }
 .seat { background:#1a1f27; border:1px solid #2d333b; border-radius:10px; padding:10px 12px; }
 .seat.out { opacity:.45; }
 .seat.folded { border-color:#6e3b3b; }
 .seat .no { font-size:13px; color:#9aa0a6; }
 .seat .pos { color:#e8eaed; background:#2d333b; border-radius:4px; padding:1px 5px; font-size:11px; }
 .seat .cards { font-size:20px; font-weight:700; margin:4px 0; min-height:28px; }
 .tag { display:inline-block; font-size:11px; padding:1px 7px; border-radius:999px; margin-right:4px; }
 .t-present { background:#1f3a24; color:#7ee787; }
 .t-away { background:#3a2f1f; color:#e3b341; }
 .t-folded { background:#3a1f1f; color:#ff7b72; }
 .t-none { background:#23262b; color:#9aa0a6; }
 .err { color:#ff7b72; }
 table.tl { border-collapse:collapse; margin-top:18px; font-size:13px; color:#9aa0a6; }
 table.tl td { padding:2px 10px 2px 0; }
</style></head>
<body>
<div id="app">読み込み中…</div>
<script>
const RED = new Set(["h","d"]);
function cardHtml(c){
  const red = RED.has((c||"").slice(-1).toLowerCase());
  return `<div class="card${red?" red":""}">${c}</div>`;
}
function lagClass(s){ return s < 3 ? "ok" : (s < 15 ? "warn" : "bad"); }
async function tick(){
  let d;
  try { d = await (await fetch("/state.json?_=" + Date.now())).json(); }
  catch (e) { document.getElementById("app").innerHTML =
      '<p class="err">モニタに接続できません。</p>'; return; }
  if (d.error) { document.getElementById("app").innerHTML =
      `<h1>卓状態モニタ</h1><p class="err">${d.error}</p>`; return; }
  const lag = d.age_sec == null ? null : d.age_sec;
  const seats = (d.seats||[]).map(s => {
    const cls = !s.dealt_in ? "seat out" : (s.likely_folded ? "seat folded" : "seat");
    let tag = '<span class="tag t-none">未配布</span>';
    if (s.present) tag = '<span class="tag t-present">卓上</span>';
    else if (s.likely_folded) tag = `<span class="tag t-folded">fold らしい ${s.away_sec}s</span>`;
    else if (s.dealt_in) tag = `<span class="tag t-away">離れて ${s.away_sec ?? "?"}s</span>`;
    const pos = s.position ? ` <span class="pos">${s.position}</span>` : "";
    return `<div class="${cls}"><div class="no">席 ${s.seat}${pos}</div>
      <div class="cards">${(s.cards||[]).join(" ") || "—"}</div>${tag}</div>`;
  }).join("");
  const tl = (d.board_timeline||[]).map(e =>
    `<tr><td>${e.index} 枚目</td><td>${e.card}</td><td>${e.dealt_at}</td></tr>`).join("");
  document.getElementById("app").innerHTML = `
    <h1>卓状態モニタ — ${d.session_id||""} / ハンド ${d.hand_id}</h1>
    <div class="meta">更新 ${d.updated_at||"—"}
      ${lag==null?"":`／ 反映遅延 <span class="lag ${lagClass(lag)}">${lag.toFixed(1)} 秒</span>`}
      ${d.engine_street?`／ engine: ${d.engine_street}`:""}
      ${d.button_seat?`／ ボタン: 席 ${d.button_seat}`:""}</div>
    <div class="board"><span class="street">${d.rfid_street||""}</span>
      ${(d.board||[]).map(cardHtml).join("") || "<span class='meta'>ボードなし</span>"}</div>
    <div class="seats">${seats}</div>
    ${tl?`<table class="tl"><tr><td colspan="3">ボード配布時刻</td></tr>${tl}</table>`:""}`;
}
tick(); setInterval(tick, 1000);
</script></body></html>
"""


def find_snapshot(log_dir: Path, session: Optional[str]) -> Optional[Path]:
    """対象のスナップショットファイルを決める（未指定なら最新更新のもの）。"""
    if session:
        p = log_dir / f"{session}.table_state.json"
        return p if p.exists() else None
    candidates = sorted(
        log_dir.glob("*.table_state.json"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    return candidates[0] if candidates else None


def read_state(log_dir: Path, session: Optional[str]) -> dict:
    """スナップショットを読んで `age_sec`（反映遅延）を付けて返す。"""
    path = find_snapshot(log_dir, session)
    if path is None:
        return {"error": f"{log_dir} に卓状態がありません。"
                         "hand logger を起動して config の table_state.enabled を確認してください。"}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:      # 書き込み途中は次の tick で読めばよい
        return {"error": f"読み込み中です（{e.__class__.__name__}）"}
    try:
        updated = datetime.fromisoformat(state.get("updated_at", ""))
        state["age_sec"] = max(0.0, (datetime.now() - updated).total_seconds())
    except ValueError:
        state["age_sec"] = None
    return state


def _format_text(state: dict) -> str:
    """端末表示（`--once`）。"""
    if state.get("error"):
        return state["error"]
    lines = [
        f"session {state.get('session_id')} / hand {state.get('hand_id')} "
        f"/ 更新 {state.get('updated_at')} (遅延 {state.get('age_sec')}s)",
        f"street(RFID) {state.get('rfid_street')}  engine {state.get('engine_street')}  "
        f"button {state.get('button_seat') or '—'}  "
        f"board {' '.join(state.get('board') or []) or '—'}",
    ]
    for s in state.get("seats", []):
        if s["present"]:
            tag = "卓上"
        elif s["likely_folded"]:
            tag = f"fold らしい({s['away_sec']}s)"
        elif s["dealt_in"]:
            tag = f"離席({s['away_sec']}s)"
        else:
            tag = "未配布"
        pos = f" {s.get('position') or '':<5}"
        lines.append(f"  席{s['seat']}{pos}: {' '.join(s['cards']) or '—':<8} {tag}")
    return "\n".join(lines)


def serve(log_dir: Path, session: Optional[str], host: str, port: int) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:          # noqa: N802 — BaseHTTPRequestHandler の規約
            if self.path.startswith("/state.json"):
                body = json.dumps(read_state(log_dir, session), ensure_ascii=False).encode()
                ctype = "application/json; charset=utf-8"
            else:
                body = _PAGE.encode()
                ctype = "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:   # アクセスログを黙らせる
            pass

    server = HTTPServer((host, port), Handler)
    print(f"卓状態モニタ: http://{host}:{port}/  (Ctrl+C で終了)")
    if host == "127.0.0.1":
        print("  ※ iPad / スマホから見るには --host 0.0.0.0 を付けて PC の IP を開いてください。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n終了しました。")
    finally:
        server.server_close()


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="RFID 由来の卓状態モニタ（実プレイ環境の検証用）")
    ap.add_argument("--log-dir", default="./logs", help="ログディレクトリ（既定 ./logs）")
    ap.add_argument("--session", help="セッション ID（省略時は最新）")
    ap.add_argument("--host", default="127.0.0.1", help="bind host（iPad から見るなら 0.0.0.0）")
    ap.add_argument("--port", type=int, default=8790, help="bind port（既定 8790）")
    ap.add_argument("--once", action="store_true", help="端末に 1 回表示して終了")
    args = ap.parse_args(argv)

    log_dir = Path(args.log_dir)
    if args.once:
        print(_format_text(read_state(log_dir, args.session)))
        return 0
    serve(log_dir, args.session, args.host, args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
