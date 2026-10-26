"""店の PC で真のアクションを入れる画面（卓の形, 2026-10-23）。

オーナー:「店での入力で、ハンドが配られる前に次のハンドの入力欄を出したい・額をテンキーで打ちたい・ベットとレイズ、
チェックとコールは同じボタンでよい・卓の形に並べた席のボタンをクリックして入れる PC の画面を試したい」。

真のアクションの入力画面のサーバー（`tools/ground_truth_ui.py`）が `/table` で出す。

- 開いたときに入れるハンド: ロガーのいちばん新しいハンド（進行中 = `live_hand.json` を含む）の次（まだ配っていない = `pending`）。
  前のハンドが配られた時点で次のハンドに移る（2026-10-26）。進行中・終わったハンドは「← 前」で開ける。
  次のハンドの席・持ち点・ボタン・ブラインドは前のハンドから見込む（前のハンドの真のアクションがあればその収支）。
- 行の手番・ストリート・額は入力画面と同じく pokerkit で流す（`replay_legal`）。手番より先の席を押したら、間の席は
  フォールド（フォールドは省略されることがある = オーナー 2026-10-23）として入れる（`auto`）。
- 保存は入力画面と同じ受け口（`PUT /api/sessions/<sid>/hands/<hid>`）。まだ配っていないハンドはロガーがハンドを
  始めるまで画面が持っていて、始まったら保存する。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from core.positions import position_map
from core.truth_hand import active_seats, apply_rows, materialize_truth, start_state, with_table

# 手番より先の席を押したときに入れるフォールドの上限（1 周 = 席の数より多くはならない）
_MAX_FILL = 12


def _hand_of(log_dir: Path, session_id: str, corr_repo: Any = None) -> tuple[list[dict], Optional[dict]]:
    from tools.ground_truth_ui import _apply_corrections, _hands_of, _load_hand_log, _load_live_hand

    log = _load_hand_log(log_dir, session_id)
    hands = [_apply_corrections(h, session_id, corr_repo) for h in (_hands_of(log) if log else [])]
    return hands, _load_live_hand(log_dir, session_id)


def latest_hand(log_dir: Path, session_id: str, corr_repo: Any = None) -> Optional[dict]:
    """いちばん新しいハンド（進行中のハンドがあればそれ）。"""
    hands, live = _hand_of(log_dir, session_id, corr_repo)
    cands = hands + ([live] if live else [])
    return max(cands, key=lambda h: h["hand_id"]) if cands else None


def _end_stacks(hand: dict, gt: Optional[dict]) -> dict[int, int]:
    """ハンドの終わりの持ち点（真のアクションがあればその収支、無ければ記録の収支。終わっていなければ始めの持ち点）。"""
    if gt is not None:
        truth = materialize_truth(gt, hand)
        if truth is not None:
            return {p["seat"]: int(p["stack_end"]) for p in truth["players"] if p.get("stack_end") is not None}
    table = with_table(hand, (gt or {}).get("players"))
    out = {}
    for p in table.get("players") or []:
        start = p.get("stack_start")
        if not isinstance(p.get("seat"), int) or not isinstance(start, (int, float)):
            continue
        result = p.get("result") if isinstance(p.get("result"), (int, float)) and not hand.get("in_progress") else 0
        out[p["seat"]] = int(start + result)
    return out


def _next_button(seats: list[int], button: Optional[int]) -> Optional[int]:
    """前のボタンの次の席（前のボタンの席が抜けていても、その次の席）。"""
    if not seats:
        return None
    if button is None:
        return max(seats)
    later = [s for s in sorted(seats) if s > button]
    return later[0] if later else min(seats)


def skeleton_after(prev: dict, gt: Optional[dict]) -> dict:
    """前のハンドから見込んだ次のハンド（まだ配っていない）: 持ち点が残る席・ボタン・ブラインド。"""
    stacks = _end_stacks(prev, gt)
    absent = {p.get("seat") for p in (gt or {}).get("players") or [] if isinstance(p, dict) and p.get("absent")}
    names = {p.get("seat"): p.get("name") for p in prev.get("players") or [] if isinstance(p, dict)}
    seats = sorted(s for s, v in stacks.items() if v > 0 and s not in absent)
    button = (gt or {}).get("button_seat") if isinstance((gt or {}).get("button_seat"), int) else prev.get("button_seat")
    return {
        "hand_id": prev["hand_id"] + 1,
        "players": [{"seat": s, "name": names.get(s) or f"席{s}", "stack_start": stacks[s]} for s in seats],
        "blinds": prev.get("blinds"),
        "button_seat": _next_button(seats, button),
        "board": [],
    }


def _base_of(hand: dict) -> dict:
    return {
        "hand_id": hand["hand_id"],
        "players": [{"seat": p["seat"], "name": p.get("name") or f"席{p['seat']}", "stack_start": p.get("stack_start")}
                    for p in hand.get("players") or [] if isinstance(p, dict) and isinstance(p.get("seat"), int)],
        "blinds": hand.get("blinds"),
        "button_seat": hand.get("button_seat"),
        "board": [c for c in hand.get("board") or [] if isinstance(c, str)],
    }


def table_hand(log_dir: Path, session_id: str, hand_id: int, gt_repo: Any, corr_repo: Any = None) -> Optional[dict]:
    """入れるハンドの土台（席・持ち点・ボタン・ブラインド・ボード）と保存した真のアクション。

    `state`: `done`（ロガーが確定した）/ `in_progress`（進行中）/ `pending`（まだ配っていない = 次のハンド）。
    """
    hands, live = _hand_of(log_dir, session_id, corr_repo)
    by_id = {h["hand_id"]: h for h in hands}
    if live is not None:
        by_id.setdefault(live["hand_id"], live)
    gt = gt_repo.get(session_id, hand_id)
    captured = by_id.get(hand_id)
    latest = max(by_id) if by_id else None
    if captured is not None:
        base, state = _base_of(captured), ("in_progress" if captured.get("in_progress") else "done")
    elif latest is not None and hand_id == latest + 1:
        prev_gt = gt_repo.get(session_id, latest)
        base, state = skeleton_after(by_id[latest], prev_gt.hand if prev_gt else None), "pending"
    else:
        return None
    return {"session_id": session_id, "hand_id": hand_id, "state": state, "base": base,
            "latest_id": latest, "ground_truth": gt.hand if gt is not None else None}


def current_hand_id(log_dir: Path, session_id: str, corr_repo: Any = None) -> Optional[int]:
    """いま入れるハンド: いちばん新しいハンドの次（まだ配っていない）。前のハンドが始まった（配られた）時点で次のハンドに
    移る（オーナー 2026-10-26: 前のハンドが終わる = ロガーが勝者を決める・次の配布、まで待たない）。"""
    hands, live = _hand_of(log_dir, session_id, corr_repo)
    ids = [h["hand_id"] for h in hands] + ([live["hand_id"]] if live is not None else [])
    return (max(ids) + 1) if ids else None


def fill_folds(table: dict, rows: list[dict], button: Optional[int]) -> list[dict]:
    """手番より先の席の行の前に、間の席のフォールドを入れる（`auto`）。手番どおりの行はそのまま。"""
    out: list[dict] = []
    try:
        gs = start_state(table, button)
    except (ImportError, ValueError, RuntimeError):
        return list(rows)
    for i, row in enumerate(rows):
        seat = row.get("seat")
        for _ in range(_MAX_FILL):
            ctx = gs.legal_context()
            if ctx.actor_seat is None or ctx.actor_seat == seat or seat not in gs.get_active_seats():
                break
            fold = {"seat": ctx.actor_seat, "action": "fold", "amount": 0, "auto": True}
            _, _, err = apply_rows(gs, [fold])
            if err:
                break
            out.append(fold)
        _, _, err = apply_rows(gs, [row])
        out.append(row)
        if err:
            out.extend(rows[i + 1:])           # 反映できない行から先はそのまま（画面がその行で止まったことを見せる）
            break
    return out


def table_legal(log_dir: Path, session_id: str, hand_id: int, body: Any, gt_repo: Any,
                corr_repo: Any = None) -> tuple[int, dict]:
    """入れた行を流す: 手番より先の席の行の前にフォールドを足し、行ごとのストリート・額と次の手番を返す。"""
    from tools.ground_truth_ui import _get_hand, replay_legal

    if not isinstance(body, dict) or not isinstance(body.get("actions"), list):
        return 400, {"code": "invalid_amount", "message": "actions が要ります"}
    t = table_hand(log_dir, session_id, hand_id, gt_repo, corr_repo)
    if t is None:
        return 404, {"code": "not_found", "message": "ハンドがありません"}
    captured = _get_hand(log_dir, session_id, hand_id, corr_repo) if t["state"] != "pending" else None
    table = with_table(captured if captured is not None else t["base"], body.get("players"))
    button = body.get("button_seat") if isinstance(body.get("button_seat"), int) else None
    rows = [r for r in body["actions"] if isinstance(r, dict)]
    rows = fill_folds(table, rows, button)
    board = body.get("board") if isinstance(body.get("board"), list) else t["base"].get("board") or None
    result = replay_legal(table, rows, board=board, button=button)
    result["rows"] = rows                      # フォールドを足した入力（画面はこれで置き換える）
    result["seats"] = seat_view(table, rows, button)
    return 200, result


def seat_view(table: dict, rows: list[dict], button: Optional[int]) -> list[dict]:
    """席ごとのいまの持ち点・このストリートに出した額・残っているか（画面の席に出す）・ポジション。"""
    try:
        gs = start_state(table, button)
    except (ImportError, ValueError, RuntimeError):
        return []
    _, mucked, _ = apply_rows(gs, rows)
    active = active_seats(gs, mucked)
    stacks = gs.get_stacks()
    seats = sorted(stacks)
    names = position_map(seats, gs.button_seat) if gs.button_seat in seats else {}
    return [{"seat": s, "stack": stacks[s], "committed": gs.committed(s), "active": s in active,
             "mucked": s in mucked, "position": names.get(s)} for s in seats]


TABLE_PAGE = r"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>卓で入れる</title>
<style>
 :root { color-scheme: dark; --bg:#10141a; --felt:#0f3d2e; --felt-edge:#1d5a43; --ink:#e8eaed; --muted:#9aa0a6;
         --panel:#161b22; --line:#2d333b; --actor:#f0b72f; --accent:#1f6feb; --bad:#ff7b72; --ok:#7ee787; }
 * { box-sizing: border-box; }
 body { margin:0; padding:10px 16px 30px; background:var(--bg); color:var(--ink);
        font:15px/1.45 system-ui,-apple-system,"Hiragino Sans","Yu Gothic UI",sans-serif; }
 h1 { font-size:18px; margin:0; } h2 { font-size:14px; margin:0 0 6px; color:#c9d1d9; }
 a { color:#58a6ff; }
 .muted { color:var(--muted); } .small { font-size:13px; }
 .bar { display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin-bottom:10px; }
 select, textarea, input[type=text] { font:inherit; color:var(--ink); background:#1e232b; border:1px solid #3a414d; border-radius:8px; padding:5px 8px; }
 button { font:inherit; color:var(--ink); background:#2d333b; border:1px solid #3a414d; border-radius:8px; padding:5px 10px; cursor:pointer; }
 button:hover:not(:disabled) { border-color:#58a6ff; } button:disabled { opacity:.35; cursor:default; }
 .pill { font-size:12px; border-radius:999px; padding:2px 10px; background:#2d333b; }
 .pill.pending { background:#5a3e0a; } .pill.live { background:#123d6a; } .pill.done { background:#1f3a2a; }
 .main { display:grid; grid-template-columns: minmax(0,1fr) 320px; gap:14px; }
 @media (max-width: 1100px) { .main { grid-template-columns: 1fr; } }
 .felt { position:relative; width:100%; aspect-ratio: 16 / 10; max-height:80vh; }
 .oval { position:absolute; left:9%; right:9%; top:12%; bottom:12%; border-radius:50%; background:radial-gradient(ellipse at center, #16573f 0%, var(--felt) 70%);
         border:10px solid var(--felt-edge); box-shadow: inset 0 0 40px rgba(0,0,0,.45); }
 .center { position:absolute; left:50%; top:50%; transform:translate(-50%,-50%); text-align:center; width:44%; }
 .center .street { font-size:20px; font-weight:600; } .center .pot { font-size:15px; color:#d6e9df; }
 .cards { display:flex; gap:6px; justify-content:center; margin:8px 0; min-height:44px; }
 .card { width:34px; height:44px; border-radius:5px; background:#f6f6f2; color:#111; display:flex; align-items:center; justify-content:center; font-weight:700; font-size:16px; }
 .card.r { color:#c0262d; } .card.b { color:#1b56c9; } .card.g { color:#1d7a3a; } .card.x { background:#3a414d; color:#9aa0a6; }
 .msg { font-size:13px; color:#ffd8a8; margin-top:6px; } .msg.bad { color:var(--bad); }
 .seat { position:absolute; transform:translate(-50%,-50%); width:200px; background:var(--panel); border:2px solid var(--line); border-radius:12px; padding:6px 8px; }
 .seat.actor { border-color:var(--actor); box-shadow:0 0 0 3px rgba(240,183,47,.25); }
 .seat.out { opacity:.45; } .seat.absent { opacity:.35; border-style:dashed; }
 .seat.winner { border-color:var(--ok); }
 .sh { display:flex; align-items:center; gap:6px; } .sh b { font-size:15px; } .sh .nm { font-size:12px; color:var(--muted); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; flex:1; }
 .pos { font-size:11px; color:#d2a8ff; }
 .dbtn { width:22px; height:22px; padding:0; border-radius:50%; font-size:11px; font-weight:700; background:#2d333b; color:#9aa0a6; }
 .dbtn.on { background:#f6f6f2; color:#111; }
 .st { font-size:12px; color:var(--muted); font-variant-numeric:tabular-nums; }
 .la { font-size:13px; min-height:18px; font-variant-numeric:tabular-nums; } .la.auto { color:var(--muted); }
 .acts { display:grid; grid-template-columns: 1fr 1.5fr 1.5fr 1fr; gap:4px; margin-top:4px; }
 .acts button { padding:5px 2px; font-size:13px; }
 .acts .f { background:#3b2020; } .acts .xc { background:#1d3a2a; } .acts .br { background:#3a2f12; } .acts .ai { background:#3a1d3a; }
 .amt { margin-top:4px; font-size:18px; font-weight:700; text-align:center; background:#0b0f14; border:1px solid var(--actor); border-radius:8px; padding:2px 6px; font-variant-numeric:tabular-nums; }
 .seat .foot { display:flex; justify-content:space-between; align-items:center; margin-top:3px; }
 .seat .foot a { font-size:11px; color:var(--muted); cursor:pointer; }
 .win { background:#1f3a2a; border-color:#2ea043; padding:2px 10px; font-size:12px; }
 .side { display:flex; flex-direction:column; gap:10px; }
 .panel { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:8px 10px; }
 .rows { max-height:46vh; overflow-y:auto; font-size:13px; font-variant-numeric:tabular-nums; }
 .rows .stg { color:#c9d1d9; margin-top:4px; font-weight:600; }
 .rows .r { display:flex; gap:6px; } .rows .r.auto { color:var(--muted); } .rows .r.err { color:var(--bad); }
 .btnrow { display:flex; flex-wrap:wrap; gap:6px; margin-top:6px; }
 .big { background:var(--accent); border-color:var(--accent); font-weight:600; }
 kbd { font:12px ui-monospace,monospace; background:#2d333b; border:1px solid #3a414d; border-radius:4px; padding:0 4px; }
</style></head><body>
<div class="bar">
 <h1>真のアクション（卓）</h1>
 <select id="sess" onchange="pickSession(this.value)"></select>
 <button onclick="go(-1)">← 前</button><b id="title">—</b><button onclick="go(1)">次 →</button>
 <span id="state" class="pill"></span>
 <span id="saved" class="small muted"></span>
 <a class="small" href="/">一覧の入力画面 →</a>
</div>
<div class="main">
 <div class="felt" id="felt"><div class="oval"></div><div class="center" id="center"></div></div>
 <div class="side">
  <div class="panel"><h2>アクション</h2><div class="rows" id="rows"></div>
   <div class="btnrow"><button onclick="undo()">↶ 戻す</button><button onclick="clearAll()">全部消す</button>
    <button id="nextBtn" class="big" onclick="go(1)" hidden>次のハンドへ（Enter）</button></div></div>
  <div class="panel"><h2>メモ</h2><textarea id="notes" rows="3" style="width:100%" placeholder="気づいたこと" oninput="setNotes(this.value)"></textarea>
   <label class="small"><input type="checkbox" id="excluded" onchange="setExcluded(this.checked)"> 評価から外す</label></div>
  <div class="panel small muted">
   <div>テンキー（手番の人 = 黄色の枠）:</div>
   <div><kbd>数字</kbd> ベット / レイズの額（トータル）→ <kbd>Enter</kbd> で確定　<kbd>+</kbd> 000　<kbd>Esc</kbd> 額を消す</div>
   <div><kbd>/</kbd> フォールド　<kbd>*</kbd> チェック / コール　<kbd>.</kbd> オールイン　<kbd>-</kbd> 戻す</div>
   <div style="margin-top:4px">手番より先の席のボタンを押すと、間の席はフォールドになります（灰色 = 自動）。
   <b>D</b> を押すとボタンをその席に。ハンドが配られる前から次のハンドを入れられます（ロガーがハンドを始めたら保存）。</div>
  </div>
 </div>
</div>
<script>
const T = {sid:null, hid:null, data:null, rows:[], button:null, absent:new Set(), winner:null, notes:"", excluded:false,
           legal:null, typing:null, started:null, uiSeats:[], capSeats:null, savedJson:"", saveTimer:null, busy:false};
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const fmt = n => Number(n || 0).toLocaleString("ja-JP");
const AC = {fold:"フォールド", check:"チェック", call:"コール", bet:"ベット", raise:"レイズ", allin:"オールイン"};
const ST = {preflop:"プリフロップ", flop:"フロップ", turn:"ターン", river:"リバー", showdown:"ショーダウン"};
const SUIT = {s:["♠","b"], h:["♥","r"], d:["♦","b"], c:["♣","g"]};
async function api(path, opt){
  const r = await fetch(path, opt); let j = {};
  try { j = await r.json(); } catch (e) {}
  if (!r.ok) { const e = new Error(j.message || r.status); e.status = r.status; throw e; }
  return j;
}
function draftKey(){ return `table_draft_${T.sid}_${T.hid}`; }
function saveDraft(){ try { localStorage.setItem(draftKey(), JSON.stringify({rows:T.rows, button:T.button, absent:[...T.absent],
  winner:T.winner, notes:T.notes, excluded:T.excluded, started:T.started})); } catch (e) {} }
function loadDraft(){ try { return JSON.parse(localStorage.getItem(draftKey()) || "null"); } catch (e) { return null; } }
function dropDraft(){ try { localStorage.removeItem(draftKey()); } catch (e) {} }

async function start(){
  const s = await api("/api/sessions");
  const list = s.sessions || [];
  $("sess").innerHTML = list.map(x => `<option value="${esc(x.session_id)}">${esc(x.session_id.slice(0,8))}（${x.hands} ハンド）</option>`).join("");
  if (list.length) await pickSession(list[0].session_id);
  setInterval(tick, 2000);
}
async function pickSession(sid){
  T.sid = sid;
  const c = await api(`/api/table/${encodeURIComponent(sid)}/current`);
  if (c.hand_id != null) await openHand(c.hand_id);
}
async function openHand(hid){
  let data;
  try { data = await api(`/api/table/${encodeURIComponent(T.sid)}/hands/${hid}`); }
  catch (e) { flash(e.status === 404 ? `ハンド ${hid} はまだありません` : e.message); return; }
  clearTimeout(T.saveTimer);
  T.hid = hid; T.data = data; T.typing = null; T.savedJson = "";
  const b = data.base, gt = data.ground_truth, d = gt ? null : loadDraft();
  T.uiSeats = (b.players || []).map(p => ({seat:p.seat, name:p.name, stack:p.stack_start}));
  T.capSeats = data.state === "pending" ? null : new Set(T.uiSeats.map(p => p.seat));
  const src = gt || d || {};
  T.rows = (src.actions || src.rows || []).map(a => ({seat:a.seat, action:a.action, amount:a.amount || 0, auto:!!a.auto}));
  T.button = src.button_seat ?? src.button ?? b.button_seat;
  T.absent = new Set(gt ? (gt.players || []).filter(p => p.absent).map(p => p.seat) : (d ? d.absent || [] : []));
  for (const p of (gt ? gt.players || [] : [])) {
    if (!T.uiSeats.some(u => u.seat === p.seat)) T.uiSeats.push({seat:p.seat, name:p.name || `席${p.seat}`, stack:p.stack_start});
  }
  T.uiSeats.sort((a, b) => a.seat - b.seat);
  T.winner = src.winner_seat ?? src.winner ?? null;
  T.notes = src.notes || ""; T.excluded = !!src.excluded; T.started = (d && d.started) || null;
  $("notes").value = T.notes; $("excluded").checked = T.excluded;
  if (gt) T.savedJson = "loaded";
  await relegal(false);
}
function playersPayload(){
  return T.uiSeats.map(p => T.absent.has(p.seat) ? {seat:p.seat, absent:true}
    : (T.capSeats && !T.capSeats.has(p.seat) ? {seat:p.seat, name:p.name, stack_start:p.stack} : {seat:p.seat}));
}
async function relegal(save = true){
  const body = {actions: T.rows.map(r => ({seat:r.seat, action:r.action, amount:r.amount || 0, auto:r.auto || undefined})),
                button_seat: T.button, players: playersPayload()};
  try {
    const r = await api(`/api/table/${encodeURIComponent(T.sid)}/hands/${T.hid}/legal`,
                        {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
    T.legal = r;
    T.rows = (r.rows || []).map(x => ({seat:x.seat, action:x.action, amount:x.amount || 0, auto:!!x.auto}));
    const n = r.next || {};
    if (n.hand_over && n.foldout_winner != null) T.winner = n.foldout_winner;
    else if (n.hand_over && T.winner == null && n.showdown_winner != null) T.winner = n.showdown_winner;
    else if (!n.hand_over) T.winner = null;
  } catch (e) { flash(e.message); }
  render();
  if (save) scheduleSave();
}
function normRows(){
  const acc = (T.legal && T.legal.actions) || [];
  return T.rows.map((r, i) => {
    const a = acc[i];
    const row = a ? {seat:a.seat, action:a.action, amount:a.amount || 0, street:a.street} : {seat:r.seat, action:r.action, amount:r.amount || 0};
    return row;
  });
}
function payload(){
  const h = {board:(T.data.base.board || []), actions:normRows(), players:playersPayload(), button_seat:T.button,
             notes:T.notes || "", blind:true};
  if (T.winner != null) h.winner_seat = T.winner;
  if (T.excluded) h.excluded = true;
  return h;
}
function entrySec(){ return T.started ? Math.round((Date.now() - T.started) / 100) / 10 : null; }
function scheduleSave(){ saveDraft(); clearTimeout(T.saveTimer); T.saveTimer = setTimeout(saveNow, 700); }
async function saveNow(){
  if (!T.data) return;
  if (T.data.state === "pending") { setSaved("まだ配っていません — ロガーがこのハンドを始めたら保存します"); return; }
  if (!T.started) return;                         // 開いただけ・まだ何も入れていない（空の真のアクションを作らない）
  const hand = payload(), json = JSON.stringify(hand);
  if (json === T.savedJson) return;
  try {
    await api(`/api/sessions/${encodeURIComponent(T.sid)}/hands/${T.hid}`, {method:"PUT", headers:{"Content-Type":"application/json"},
      body:JSON.stringify({source:"manual-edit", annotator:"table", hand, entry_sec:entrySec(), confirm_lint:true})});
    T.savedJson = json; dropDraft();
    setSaved("保存しました " + new Date().toLocaleTimeString("ja-JP"));
  } catch (e) {
    setSaved(e.status === 404 ? "ロガーのハンドがまだありません — 始まったら保存します" : "保存できません: " + e.message, true);
  }
}
function setSaved(t, bad){ const el = $("saved"); el.textContent = t; el.style.color = bad ? "var(--bad)" : ""; }
async function tick(){
  if (!T.data || T.busy) return;
  if (T.data.state === "done") return;
  try {
    const d = await api(`/api/table/${encodeURIComponent(T.sid)}/hands/${T.hid}`);
    const was = T.data.state;
    T.data.state = d.state; T.data.base.board = d.base.board || [];
    if (was === "pending" && d.state !== "pending") {
      // ロガーがハンドを始めた: 画面の席はそのまま。ロガーだけが入れた席は「空席」で出す（押せば戻せる）
      T.capSeats = new Set((d.base.players || []).map(p => p.seat));
      for (const p of d.base.players || []) {
        if (!T.uiSeats.some(u => u.seat === p.seat)) { T.uiSeats.push({seat:p.seat, name:p.name, stack:p.stack_start}); T.absent.add(p.seat); }
      }
      T.uiSeats.sort((a, b) => a.seat - b.seat);
      await relegal(true);
      return;
    }
    render();
  } catch (e) {}
}
function markStart(){ if (!T.started) T.started = Date.now(); }
function flash(t){ const el = $("flash"); if (el) { el.textContent = t; el.className = "msg bad"; } }
function seatInfo(seat){ return ((T.legal && T.legal.seats) || []).find(s => s.seat === seat); }

function act(seat, kind, amount){
  const n = (T.legal && T.legal.next) || {};
  if (T.legal && T.legal.error) { flash("止まっている行があります。↶ 戻すで直してください"); return; }
  const info = seatInfo(seat);
  if (!info || !info.active) { flash(`席${seat} はもうハンドにいません`); return; }
  if (n.hand_over && kind !== "fold") { flash("ベッティングは終わっています。勝った席の「勝ち」を押してください"); return; }
  const row = kind === "fold" ? {seat, action:"fold", amount:0}
    : kind === "xc" ? {seat, action:"call", amount:0}
    : kind === "allin" ? {seat, action:"allin", amount:0}
    : {seat, action:"raise", amount};
  markStart(); T.typing = null; T.rows.push(row); relegal();
}
function startBet(seat){ T.typing = {seat, digits:""}; render(); }
function confirmBet(){
  const t = T.typing; if (!t) return;
  const amount = parseInt(t.digits || "0", 10);
  if (!amount) { flash("額を入れてください（テンキー）"); return; }
  const info = seatInfo(t.seat) || {};
  if (info.stack != null && amount >= info.stack + (info.committed || 0)) act(t.seat, "allin");
  else act(t.seat, "br", amount);
}
function undo(){
  if (T.typing) { T.typing = null; render(); return; }
  if (!T.rows.length) return;
  T.rows.pop();
  while (T.rows.length && T.rows[T.rows.length - 1].auto) T.rows.pop();
  T.winner = null; relegal();
}
function clearAll(){ if (!T.rows.length) return; T.rows = []; T.winner = null; relegal(); }
function setButton(seat){ T.button = seat; markStart(); relegal(); }
function toggleAbsent(seat){ if (T.absent.has(seat)) T.absent.delete(seat); else T.absent.add(seat); markStart(); relegal(); }
function setWinner(seat){ T.winner = seat; markStart(); render(); scheduleSave(); }
function setNotes(v){ T.notes = v; markStart(); scheduleSave(); }
function setExcluded(v){ T.excluded = v; markStart(); scheduleSave(); }
async function go(delta){ await saveNow(); await openHand(T.hid + delta); }
function card(c){
  if (!c || c === "??") return `<div class="card x">?</div>`;
  const s = SUIT[c[1]] || ["", ""];
  return `<div class="card ${s[1]}">${esc(c[0] === "T" ? "10" : c[0])}${s[0]}</div>`;
}
function handOver(){ const n = (T.legal && T.legal.next) || {}; return !!n.hand_over; }

function render(){
  if (!T.data) return;
  const n = (T.legal && T.legal.next) || {}, err = T.legal && T.legal.error;
  const st = T.data.state;
  $("title").textContent = `ハンド ${T.hid}`;
  $("state").textContent = st === "pending" ? "まだ配っていない" : st === "in_progress" ? "進行中" : "終わった";
  $("state").className = "pill " + (st === "pending" ? "pending" : st === "in_progress" ? "live" : "done");
  // 席: ディーラーを上に、席 1 をディーラーの左（画面の右上）から時計回り
  const seats = T.uiSeats, N = Math.max(seats.length, 1);
  const acted = {};
  ((T.legal && T.legal.actions) || []).forEach((a, i) => { acted[a.seat] = {a, auto: T.rows[i] && T.rows[i].auto}; });
  const html = seats.map((p, i) => {
    const th = (i + 0.5) * 2 * Math.PI / N;
    const x = 50 + 41 * Math.sin(th), y = 50 - 40 * Math.cos(th);
    const absent = T.absent.has(p.seat), info = seatInfo(p.seat) || {};
    const isActor = !absent && n.actor_seat === p.seat && !err;
    const out = !absent && info.active === false;
    const last = acted[p.seat];
    const lastText = last ? `${AC[last.a.action] || last.a.action}${last.a.total ? " " + fmt(last.a.total) : ""}${last.a.street === "showdown" ? "（見せずに）" : ""}` : "";
    const xc = isActor ? (n.amount_to_call > 0 ? `コール ${fmt(n.call_total)}` : "チェック") : "X/C";
    const br = isActor ? ((n.legal_actions || []).includes("bet") ? "ベット" : "レイズ") : "B/R";
    const can = !absent && info.active && !err;
    const betting = can && !n.hand_over;
    const typing = T.typing && T.typing.seat === p.seat;
    const showWin = !absent && n.hand_over && info.active;
    return `<div class="seat ${isActor ? "actor" : ""} ${out ? "out" : ""} ${absent ? "absent" : ""} ${T.winner === p.seat ? "winner" : ""}" style="left:${x}%;top:${y}%">
      <div class="sh"><b>席${p.seat}</b><span class="nm">${esc(p.name || "")}</span><span class="pos">${esc(info.position || "")}</span>
        <button class="dbtn ${T.button === p.seat ? "on" : ""}" title="ボタンをこの席に" onclick="setButton(${p.seat})">D</button></div>
      <div class="st">${absent ? "空席" : `持ち点 ${fmt(info.stack)}${info.committed ? " ・ 出した " + fmt(info.committed) : ""}`}</div>
      <div class="la ${last && last.auto ? "auto" : ""}">${esc(lastText)}${last && last.auto ? "（自動）" : ""}</div>
      ${typing ? `<div class="amt">${T.typing.digits ? fmt(T.typing.digits) : "額を入れて Enter"}</div>` : ""}
      ${absent ? "" : `<div class="acts">
        <button class="f" ${can ? "" : "disabled"} onclick="act(${p.seat},'fold')">F</button>
        <button class="xc" title="チェック / コール" ${betting ? "" : "disabled"} onclick="act(${p.seat},'xc')">${esc(xc)}</button>
        <button class="br" title="ベット / レイズ（額はテンキー）" ${betting ? "" : "disabled"} onclick="startBet(${p.seat})">${esc(br)}</button>
        <button class="ai" ${betting ? "" : "disabled"} onclick="act(${p.seat},'allin')">AI</button></div>`}
      <div class="foot"><a onclick="toggleAbsent(${p.seat})">${absent ? "席に戻す" : "空席にする"}</a>
        ${showWin ? `<button class="win" onclick="setWinner(${p.seat})">${T.winner === p.seat ? "✓ 勝ち" : "勝ち"}</button>` : ""}</div>
    </div>`;
  }).join("");
  const board = (T.data.base.board || []);
  const slots = [0,1,2,3,4].map(i => board[i] ? card(board[i]) : "").join("");
  let msg = "";
  if (err) msg = `<div class="msg bad">${err.index + 1} 行目から反映できません: ${esc(err.message)}（↶ 戻す）</div>`;
  else if (n.hand_over && T.winner == null) msg = `<div class="msg">ショーダウン: 勝った席の「勝ち」を押してください（見せずに降りた人は F）</div>`;
  else if (n.hand_over) msg = `<div class="msg" style="color:var(--ok)">勝ち: 席${T.winner}　→ <kbd>Enter</kbd> で次のハンド</div>`;
  else if (n.actor_seat != null) msg = `<div class="msg">手番: 席${n.actor_seat}</div>`;
  if (st === "pending") msg += `<div class="msg">まだ配っていません。席・持ち点・ボタンは前のハンドからの見込みです（違えば直してください）</div>`;
  $("center").innerHTML = `<div class="street">${esc(ST[n.street] || n.street || "")}</div>
    <div class="pot">ポット ${fmt(n.pot)}　${T.data.base.blinds ? `ブラインド ${fmt(T.data.base.blinds.sb)}/${fmt(T.data.base.blinds.bb)}` : ""}</div>
    <div class="cards">${slots}</div>${msg}<div id="flash"></div>`;
  $("felt").querySelectorAll(".seat").forEach(el => el.remove());
  $("felt").insertAdjacentHTML("beforeend", html);
  // アクションの一覧
  const acc = (T.legal && T.legal.actions) || [];
  let stg = null, out = "";
  T.rows.forEach((r, i) => {
    const a = acc[i], street = a ? a.street : "（反映できない）";
    if (street !== stg) { stg = street; out += `<div class="stg">${esc(ST[street] || street)}</div>`; }
    const name = AC[a ? a.action : r.action] || r.action;
    const amt = a ? (a.total || 0) : r.amount;
    out += `<div class="r ${r.auto ? "auto" : ""} ${!a ? "err" : ""}"><span>${i + 1}.</span><span>席${r.seat}</span><span>${esc(name)}</span><span>${amt ? fmt(amt) : ""}</span>${r.auto ? "<span>（自動）</span>" : ""}</div>`;
  });
  $("rows").innerHTML = out || `<div class="muted">まだありません</div>`;
  $("nextBtn").hidden = !(n.hand_over && T.winner != null);
}

document.addEventListener("keydown", e => {
  const tag = e.target.tagName;
  if (tag === "TEXTAREA" || (tag === "INPUT" && e.target.type === "text") || tag === "SELECT") return;
  const n = (T.legal && T.legal.next) || {}, k = e.key;
  if (/^[0-9]$/.test(k)) {
    if (!T.typing) { if (n.actor_seat == null || n.hand_over) return; T.typing = {seat:n.actor_seat, digits:""}; }
    T.typing.digits = (T.typing.digits + k).replace(/^0+/, ""); render(); e.preventDefault(); return;
  }
  if (k === "+") { if (T.typing && T.typing.digits) { T.typing.digits += "000"; render(); } e.preventDefault(); return; }
  if (k === "Enter") {
    if (T.typing) confirmBet(); else if (n.hand_over && T.winner != null) go(1);
    e.preventDefault(); return;
  }
  if (k === "Escape") { T.typing = null; render(); return; }
  if (k === "Backspace") {
    if (T.typing) { T.typing.digits = T.typing.digits.slice(0, -1); render(); } else undo();
    e.preventDefault(); return;
  }
  if (k === "-") { undo(); e.preventDefault(); return; }
  if (n.actor_seat == null) {
    return;
  }
  if (k === "/" || k === "f" || k === "F") { act(n.actor_seat, "fold"); e.preventDefault(); }
  else if (k === "*" || k === "c" || k === "C") { act(n.actor_seat, "xc"); e.preventDefault(); }
  else if (k === "." || k === "a" || k === "A") { act(n.actor_seat, "allin"); e.preventDefault(); }
});
start().catch(e => { $("center").innerHTML = `<div class="msg bad">読み込めません: ${esc(e.message)}</div>`; });
</script></body></html>
"""
