"""推定の見直し: 推定器が再現したアクションを、真のアクション・ライブの記録と並べて見る画面（2026-10-23, オーナー:
「推定器の再現したアクションと真のアクションを比べて、改善方法を考えたい」）。

真のアクションの入力画面のサーバー（`tools/ground_truth_ui.py`・家の PC は `tools/home_truth.py`）が `/review` で出す。

- 推定 = `logs/<sid>.estimate.shadow.json`（`tools/estimate_logs.py --shadow`）か、無ければ `<sid>.estimate.json`。
- 真のアクションの行は入力画面と同じく pokerkit で流してストリートを付ける（席の有無・始めの持ち点も使う）。
- 行の正誤は物差し（`tools/measure_capture_accuracy.py`）と同じ: 並びを合わせて、同じ人・同じストリート・種類・額。
- 見直しのメモ（誤りの種類のタグと気づいたこと）は `logs/<sid>.estimate_review.json` に書く（真のアクションとは別）。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

REVIEW_SUFFIX = ".estimate_review.json"
ESTIMATE_SUFFIXES = (".estimate.shadow.json", ".estimate.json")
# 誤りの種類（見直しのタグ）。画面のチップと同じ並び
TAGS = (
    "声の読み違い", "声が無い・聞こえない", "声の遅れ・順番", "雑談をアクションに", "札の離脱（フォールド）",
    "補ったコール・チェック", "額", "ボタン・席", "持ち点", "ショーダウン・勝者", "ボード",
    "推定の選び方", "真のアクションの誤り", "その他",
)


def load_estimate_file(log_dir: Path, session_id: str) -> tuple[dict[int, dict], Optional[dict]]:
    """hand_id → 推定の 1 ハンド（`tools/estimate_logs.py:entry_for` の形）と、ファイルの頭（版・指紋）。"""
    for suffix in ESTIMATE_SUFFIXES:
        path = log_dir / f"{session_id}{suffix}"
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        hands = {}
        for key, entry in (data.get("hands") or {}).items():
            if isinstance(entry, dict) and isinstance(entry.get("hand"), dict):
                try:
                    hands[int(key)] = entry
                except (TypeError, ValueError):
                    continue
        head = {k: data.get(k) for k in ("estimator_version", "params_hash", "content_hash")}
        head["file"] = path.name
        return hands, head
    return {}, None


def load_notes(log_dir: Path, session_id: str) -> dict[str, dict]:
    path = log_dir / f"{session_id}{REVIEW_SUFFIX}"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    hands = data.get("hands") if isinstance(data, dict) else None
    return hands if isinstance(hands, dict) else {}


def save_note(log_dir: Path, session_id: str, hand_id: int, body: Any) -> tuple[int, dict]:
    if not isinstance(body, dict):
        return 400, {"code": "invalid", "message": "本体が JSON のオブジェクトではありません"}
    tags = [t for t in body.get("tags") or [] if t in TAGS]
    note = body.get("note") if isinstance(body.get("note"), str) else ""
    hands = load_notes(log_dir, session_id)
    if tags or note.strip():
        hands[str(hand_id)] = {"tags": tags, "note": note.strip()[:4000],
                               "updated_at": datetime.now().isoformat(timespec="seconds")}
    else:
        hands.pop(str(hand_id), None)
    path = log_dir / f"{session_id}{REVIEW_SUFFIX}"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"session_id": session_id, "hands": hands}, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(path)
    return 200, {"ok": True, "hand_id": hand_id, **(hands.get(str(hand_id)) or {"tags": [], "note": ""})}


def _truth_hand(captured: dict, gt: dict) -> dict:
    """真のアクションに、入力画面と同じ流し方でストリートと合計の額を付けたハンド。"""
    from core.truth_hand import with_table
    from tools.ground_truth_ui import _gt_actions, _gt_button, _gt_cards, replay_legal

    rows = _gt_actions(gt)
    board, holes = _gt_cards(gt)
    table = with_table(captured, gt.get("players"))
    legal = replay_legal(table, rows, board=board, holes=holes, button=_gt_button(gt))
    done = legal.get("actions") or []
    actions = []
    for i, a in enumerate(rows):
        row = dict(a)
        if i < len(done):
            row.update(street=done[i].get("street"), amount=done[i].get("amount"), total=done[i].get("total"))
        actions.append(row)
    button = gt.get("button_seat") if gt.get("button_seat") is not None else captured.get("button_seat")
    holes = {p.get("seat"): p.get("hole_cards") for p in gt.get("players") or [] if isinstance(p, dict)}
    players = [{"seat": p.get("seat"), "stack_start": p.get("stack_start"), "hole_cards": holes.get(p.get("seat"))}
               for p in table.get("players") or []]
    return {**gt, "actions": actions, "board": gt.get("board") or captured.get("board") or [],
            "button_seat": button, "players": players}


def _compare(truth: dict, hand: Optional[dict]) -> dict:
    """行ごとの正誤: 真のアクションの各行が合ったか（`truth_ok`）と、比べる側の各行が正しいか（`ok`）。"""
    from tools.measure_capture_accuracy import _align_actions, hand_fully_correct, measure_hand, row_correct

    gt_rows = truth.get("actions") or []
    rows = (hand or {}).get("actions") or []
    pairs, _, _ = _align_actions(gt_rows, rows)
    ok_cap = {id(c) for g, c in pairs if row_correct(g, c)}
    ok_gt = {id(g) for g, c in pairs if row_correct(g, c)}
    m = measure_hand(truth, hand)
    return {
        "exact": hand_fully_correct(truth, hand),
        "correct": m.action_correct, "total": m.action_total,
        "rows_ok": [id(r) in ok_cap for r in rows],
        "truth_ok": [id(r) in ok_gt for r in gt_rows],
        "winner_ok": m.winner_match, "board_ok": m.board_match,
    }


def _rows(hand: Optional[dict]) -> list[dict]:
    keep = ("street", "seat", "action", "amount", "raw_text", "reason", "actor_source", "needs_review", "position")
    return [{k: a.get(k) for k in keep if a.get(k) is not None} for a in (hand or {}).get("actions") or []]


def _side(hand: Optional[dict]) -> Optional[dict]:
    if hand is None:
        return None
    return {"button_seat": hand.get("button_seat"), "winner_seat": hand.get("winner_seat"),
            "board": hand.get("board") or [], "pot_total": hand.get("pot_total"),
            "seats": [p.get("seat") for p in hand.get("players") or []],
            "stacks": {str(p.get("seat")): p.get("stack_start") for p in hand.get("players") or []},
            "rows": _rows(hand)}


def _hand_parts(log_dir: Path, session_id: str, hand_id: int, gt_repo: Any, corr_repo: Any,
                estimates: dict[int, dict]) -> Optional[tuple[dict, Optional[dict], Optional[dict], Optional[dict]]]:
    from tools.ground_truth_ui import _get_hand
    from tools.eval_store import is_excluded

    captured = _get_hand(log_dir, session_id, hand_id, corr_repo)
    if captured is None:
        return None
    gt = gt_repo.get(session_id, hand_id)
    truth = _truth_hand(captured, gt.hand) if gt is not None and not is_excluded(gt.hand) else None
    entry = estimates.get(hand_id)
    return captured, truth, entry, (gt.hand if gt is not None else None)


def review_summary(log_dir: Path, session_id: str, gt_repo: Any, corr_repo: Any = None) -> dict:
    from tools.ground_truth_ui import _hands_of, _load_hand_log

    log = _load_hand_log(log_dir, session_id)
    if log is None:
        return {"session_id": session_id, "hands": [], "estimate": None}
    estimates, head = load_estimate_file(log_dir, session_id)
    notes = load_notes(log_dir, session_id)
    out = []
    for h in _hands_of(log):
        hid = h.get("hand_id")
        parts = _hand_parts(log_dir, session_id, hid, gt_repo, corr_repo, estimates)
        if parts is None:
            continue
        captured, truth, entry, gt_raw = parts
        row: dict[str, Any] = {"hand_id": hid, "started_at": captured.get("started_at"),
                               "truth": truth is not None, "excluded": gt_raw is not None and truth is None,
                               "note": notes.get(str(hid))}
        if truth is not None:
            live = _compare(truth, captured)
            row["live"] = {k: live[k] for k in ("exact", "correct", "total")}
            if entry is not None:
                est = _compare(truth, entry["hand"])
                row["est"] = {k: est[k] for k in ("exact", "correct", "total")}
        if entry is not None:
            row["margin"] = entry.get("margin")
            row["review"] = bool(entry.get("review"))
            row["changed"] = bool(entry.get("changed"))
        out.append(row)
    return {"session_id": session_id, "hands": out, "estimate": head, "tags": list(TAGS)}


def review_detail(log_dir: Path, session_id: str, hand_id: int, gt_repo: Any, corr_repo: Any = None) -> Optional[dict]:
    from tools.ground_truth_ui import _next_started_at, hand_timeline

    estimates, head = load_estimate_file(log_dir, session_id)
    parts = _hand_parts(log_dir, session_id, hand_id, gt_repo, corr_repo, estimates)
    if parts is None:
        return None
    captured, truth, entry, gt_raw = parts
    est_hand = entry["hand"] if entry is not None else None
    out: dict[str, Any] = {
        "session_id": session_id, "hand_id": hand_id, "started_at": captured.get("started_at"),
        "blinds": captured.get("blinds"), "estimate_head": head,
        "truth": _side(truth), "est": _side(est_hand), "live": _side(captured),
        "truth_notes": (gt_raw or {}).get("notes"), "excluded": gt_raw is not None and truth is None,
        "note": load_notes(log_dir, session_id).get(str(hand_id)),
        "timeline": hand_timeline(log_dir, session_id, captured, _next_started_at(log_dir, session_id, hand_id)),
        "tags": list(TAGS),
    }
    if entry is not None:
        out["estimate"] = {k: entry.get(k) for k in ("margin", "posterior", "review", "changed", "edits", "notes",
                                                     "alternatives", "explanation_only")}
    if truth is not None:
        out["compare"] = {"live": _compare(truth, captured)}
        if est_hand is not None:
            out["compare"]["est"] = _compare(truth, est_hand)
    return out


REVIEW_PAGE = r"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>推定の見直し</title>
<style>
 :root { color-scheme: dark; }
 * { box-sizing: border-box; }
 body { margin:0; padding:12px 16px 40px; font:15px/1.5 system-ui,-apple-system,"Hiragino Sans",sans-serif;
        background:#12151a; color:#e8eaed; }
 h1 { font-size:18px; margin:0; } h2 { font-size:15px; margin:14px 0 6px; color:#c9d1d9; }
 a { color:#58a6ff; }
 .muted { color:#9aa0a6; } .small { font-size:13px; }
 .bar { display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin-bottom:10px; }
 select, textarea { font:inherit; color:#e8eaed; background:#1e232b; border:1px solid #3a414d; border-radius:8px; padding:6px 8px; }
 button { font:inherit; color:#e8eaed; background:#2d333b; border:1px solid #3a414d; border-radius:8px; padding:6px 12px; cursor:pointer; }
 button.primary { background:#1f6feb; border-color:#1f6feb; }
 button.sm { padding:2px 8px; font-size:13px; }
 .layout { display:grid; grid-template-columns: minmax(250px, 330px) 1fr; gap:14px; }
 @media (max-width: 900px) { .layout { grid-template-columns: 1fr; } }
 .list { max-height:85vh; overflow-y:auto; border:1px solid #2d333b; border-radius:10px; }
 table { border-collapse:collapse; width:100%; }
 td, th { padding:4px 6px; border-bottom:1px solid #23272e; text-align:left; font-size:13px; white-space:nowrap; }
 th { color:#9aa0a6; font-weight:400; }
 tr.sel { background:#1f2f3a; } tr.pick { cursor:pointer; } tr.pick:hover { background:#1a1f27; }
 .ok { color:#7ee787; } .ng { color:#ff7b72; } .na { color:#6e7681; }
 .cols { display:grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap:10px; }
 @media (max-width: 1200px) { .cols { grid-template-columns: 1fr; } }
 .panel { border:1px solid #2d333b; border-radius:10px; padding:8px 10px; background:#161a20; overflow-x:auto; }
 .rowok td { background:rgba(46,160,67,.10); } .rowng td { background:rgba(248,81,73,.14); }
 .why { color:#9aa0a6; font-size:12px; white-space:normal; }
 .chips { display:flex; flex-wrap:wrap; gap:6px; }
 .chip { border-radius:999px; padding:3px 10px; font-size:13px; }
 .chip.on { background:#1f6feb; border-color:#1f6feb; }
 .tag { display:inline-block; font-size:11px; padding:0 6px; border-radius:999px; background:#2d333b; margin-right:3px; }
 .tl { position:relative; max-height:45vh; overflow-y:auto; border:1px solid #2d333b; border-radius:10px; padding:4px 8px; }
 .tl .item { display:flex; gap:8px; align-items:center; padding:3px 0; border-bottom:1px solid #23272e; font-size:13px; }
 .tl .t { color:#9aa0a6; min-width:52px; font-variant-numeric:tabular-nums; }
 .tl .noise { opacity:.5; } .tl .sys { color:#9aa0a6; } .tl .playing { background:#1f2f3a; }
 .meta { display:grid; grid-template-columns: max-content 1fr; gap:2px 10px; font-size:13px; }
</style></head><body>
<div class="bar"><h1>推定の見直し</h1><select id="sess" onchange="pickSession(this.value)"></select>
 <label class="small"><input type="checkbox" id="onlyBad" onchange="renderList()"> 推定が真と違うハンドだけ</label>
 <span id="sum" class="small muted"></span>
 <a class="small" href="/">← 真のアクションの入力</a></div>
<div class="layout"><div class="list" id="list"></div><div id="detail" class="muted">左のハンドを選んでください。</div></div>
<script>
const R = {sid: null, sum: null, hand: null, audio: null, playing: null, playAll: false, tl: []};
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const ST = {preflop:"プリ", flop:"フロップ", turn:"ターン", river:"リバー", showdown:"SD"};
const AC = {fold:"フォールド", check:"チェック", call:"コール", bet:"ベット", raise:"レイズ", allin:"オールイン"};
async function api(path, opt){ const r = await fetch(path, opt); const j = await r.json(); if (!r.ok) throw new Error(j.message || r.status); return j; }
async function start(){
  const s = await api("/api/sessions");
  const sessions = (s.sessions || []).filter(x => x.hands > 0);
  $("sess").innerHTML = sessions.map(x => `<option value="${esc(x.session_id)}">${esc(x.session_id.slice(0,8))}（${x.hands} ハンド）</option>`).join("");
  if (sessions.length) pickSession(sessions[0].session_id);
}
async function pickSession(sid){ R.sid = sid; R.sum = await api("/api/review/" + encodeURIComponent(sid)); renderList(); }
function mark(x){ return x == null ? '<span class="na">—</span>' : (x.exact ? '<span class="ok">✓</span>' : '<span class="ng">✗</span>') + ` <span class="muted">${x.correct}/${x.total}</span>`; }
function renderList(){
  const s = R.sum; if (!s) return;
  const only = $("onlyBad").checked;
  const rows = s.hands.filter(h => !only || (h.est && !h.est.exact));
  const withT = s.hands.filter(h => h.est);
  $("sum").textContent = s.estimate ? `推定 ${withT.filter(h => h.est.exact).length}/${withT.length} ・ 記録 ${s.hands.filter(h => h.live && h.live.exact).length}/${s.hands.filter(h => h.live).length} が全部正しい（${s.estimate.file}・内容の指紋 ${s.estimate.content_hash || "—"}）`
                                    : "このセッションの推定のファイルがありません（pokerestimate の zip を一緒に渡してください）";
  $("list").innerHTML = `<table><tr><th>ハンド</th><th>推定</th><th>記録</th><th>差</th><th>メモ</th></tr>${rows.map(h => `
    <tr class="pick ${R.hand && R.hand.hand_id === h.hand_id ? "sel" : ""}" onclick="openHand(${h.hand_id})">
     <td>#${h.hand_id} <span class="muted">${esc((h.started_at||"").slice(11,16))}</span>${h.excluded ? ' <span class="tag">外した</span>' : (h.truth ? "" : ' <span class="tag">真なし</span>')}</td>
     <td>${mark(h.est)}${h.review ? ' <span class="tag">要確認</span>' : ""}</td><td>${mark(h.live)}</td>
     <td class="muted">${h.margin == null ? "" : Number(h.margin).toFixed(1)}</td>
     <td>${h.note ? (h.note.tags || []).map(t => `<span class="tag">${esc(t)}</span>`).join("") || "✎" : ""}</td></tr>`).join("")}</table>`;
}
function rowsTable(side, oks, title, cmp){
  if (!side) return `<div class="panel"><h2>${title}</h2><p class="muted small">ありません</p></div>`;
  const body = side.rows.map((a, i) => {
    const ok = oks ? oks[i] : null;
    return `<tr class="${ok === true ? "rowok" : ok === false ? "rowng" : ""}"><td>${ST[a.street] || esc(a.street)}</td><td>席${a.seat}${a.position ? ` <span class="muted">${esc(a.position)}</span>` : ""}</td>
      <td>${AC[a.action] || esc(a.action)}</td><td>${a.amount || ""}</td>
      <td class="why">${a.raw_text ? "「" + esc(a.raw_text) + "」 " : ""}${a.reason ? esc(a.reason) : ""}</td></tr>`;
  }).join("");
  const head = `ボタン 席${side.button_seat ?? "—"} ／ 勝者 ${side.winner_seat != null ? "席" + side.winner_seat : "—"} ／ ボード ${(side.board || []).join(" ") || "—"}`
    + (cmp ? ` ／ 勝者 ${cmp.winner_ok === false ? '<span class="ng">✗</span>' : '<span class="ok">✓</span>'} ボード ${cmp.board_ok ? '<span class="ok">✓</span>' : '<span class="ng">✗</span>'}` : "");
  return `<div class="panel"><h2>${title} ${cmp ? (cmp.exact ? '<span class="ok">全部正しい</span>' : `<span class="ng">${cmp.correct}/${cmp.total}</span>`) : ""}</h2>
    <div class="small muted">${head}</div><div class="small muted">席 ${(side.seats||[]).map(s => `${s}:${side.stacks[String(s)] ?? "—"}`).join(" ")}</div>
    <table>${body || "<tr><td class='muted'>行なし</td></tr>"}</table></div>`;
}
async function openHand(hid){
  stopAll();
  const d = await api(`/api/review/${encodeURIComponent(R.sid)}/${hid}`);
  R.hand = d; renderList(); renderDetail();
}
function renderDetail(){
  const d = R.hand, c = d.compare || {}, e = d.estimate || {};
  const note = d.note || {tags: [], note: ""};
  R.tl = (d.timeline || []).filter(x => x.kind === "speech" && x.audio).map(x => x.audio);
  const alts = (e.alternatives || []).map((a, i) => `<div class="small">次点 ${i + 1}（事後 ${a.posterior}）: ${esc((a.edits || []).join("・") || "直しなし")} — <span class="muted">${esc(JSON.stringify(a.actions || "")).slice(0, 300)}</span></div>`).join("");
  $("detail").innerHTML = `
    <div class="bar"><h1 style="font-size:16px">ハンド #${d.hand_id}</h1><span class="muted small">${esc(d.started_at)} ／ ブラインド ${d.blinds ? d.blinds.sb + "/" + d.blinds.bb : "—"}</span>
      ${d.truth_notes ? `<span class="small">真のアクションのメモ: ${esc(d.truth_notes)}</span>` : ""}</div>
    <div class="cols">${rowsTable(d.truth, c.est ? c.est.truth_ok : null, "真のアクション（色 = 推定で合った行）", null)}
      ${rowsTable(d.est, c.est ? c.est.rows_ok : null, "推定", c.est)}${rowsTable(d.live, c.live ? c.live.rows_ok : null, "ライブの記録", c.live)}</div>
    <div class="panel" style="margin-top:10px"><div class="meta">
      <span class="muted">推定の直し</span><span>${esc((e.edits || []).join("・") || "なし")}</span>
      <span class="muted">要確認の理由</span><span>${esc((e.notes || []).join("・") || "なし")}</span>
      <span class="muted">次点との差</span><span>${e.margin == null ? "—" : Number(e.margin).toFixed(2)}（事後確率 ${e.posterior ?? "—"}）</span></div>${alts}</div>
    <h2>見直しのメモ</h2>
    <div class="chips">${d.tags.map(t => `<button class="chip ${note.tags.includes(t) ? "on" : ""}" onclick="toggleTag('${esc(t)}')">${esc(t)}</button>`).join("")}</div>
    <textarea id="noteText" rows="3" style="width:100%;margin-top:6px" placeholder="気づいたこと（何が原因か・どう直せそうか）">${esc(note.note)}</textarea>
    <div class="bar" style="margin-top:6px"><button class="primary" onclick="saveNote()">メモを保存</button><span id="saved" class="small muted"></span></div>
    <h2>発話と札の流れ <span class="muted small">+秒 = 手札が配られてから</span></h2>
    <div class="bar"><button class="sm" id="playAll" onclick="togglePlayAll()">▶ 続けて再生</button><span class="small muted">発話 ${R.tl.length} 件</span></div>
    <div class="tl">${(d.timeline || []).map(it => {
      const t = (it.t >= 0 ? "+" : "") + Number(it.t).toFixed(1) + "s";
      if (it.kind === "speech") return `<div class="item ${it.noise ? "noise" : ""}" data-audio="${esc(it.audio || "")}"><span class="t">${t}</span>${it.audio ? `<button class="sm" onclick="play('${esc(it.audio)}')">▶</button>` : ""}<span>「${esc(it.text)}」 <span class="muted">${it.parsed && it.parsed.length ? "→ " + esc(it.parsed.join("、")) : ""}</span></span></div>`;
      return `<div class="item sys"><span class="t">${t}</span><span>${esc(it.text)}</span></div>`;
    }).join("")}</div>`;
  R.note = {tags: [...note.tags], note: note.note};
}
function toggleTag(t){ const i = R.note.tags.indexOf(t); if (i >= 0) R.note.tags.splice(i, 1); else R.note.tags.push(t);
  document.querySelectorAll(".chips .chip").forEach(b => b.classList.toggle("on", R.note.tags.includes(b.textContent))); }
async function saveNote(){
  R.note.note = $("noteText").value;
  try {
    await api(`/api/review/${encodeURIComponent(R.sid)}/${R.hand.hand_id}`, {method: "PUT", headers: {"Content-Type": "application/json"}, body: JSON.stringify(R.note)});
    $("saved").textContent = "保存しました";
    const h = R.sum.hands.find(x => x.hand_id === R.hand.hand_id); if (h) h.note = {tags: [...R.note.tags], note: R.note.note};
    renderList();
  } catch (e) { $("saved").textContent = "保存できません: " + e.message; }
}
function play(name){
  try { if (R.audio) { R.audio.onended = null; R.audio.pause(); } } catch (e) {}
  R.audio = new Audio(`/api/sessions/${encodeURIComponent(R.sid)}/audio/${encodeURIComponent(name)}`);
  R.playing = name; markPlaying();
  R.audio.onended = () => { const i = R.tl.indexOf(name); if (R.playAll && i >= 0 && i + 1 < R.tl.length) play(R.tl[i + 1]); else stopAll(); };
  R.audio.play().catch(() => stopAll());
}
function markPlaying(){
  document.querySelectorAll(".tl .item.playing").forEach(el => el.classList.remove("playing"));
  const el = Array.from(document.querySelectorAll(".tl .item[data-audio]")).find(x => x.dataset.audio === R.playing);
  if (!el) return; el.classList.add("playing");
  const box = el.closest(".tl"), top = el.offsetTop, bottom = top + el.offsetHeight;
  if (top < box.scrollTop) box.scrollTop = top; else if (bottom > box.scrollTop + box.clientHeight) box.scrollTop = bottom - box.clientHeight;
}
function togglePlayAll(){ if (R.playAll) { stopAll(); return; } if (!R.tl.length) return; R.playAll = true; $("playAll").textContent = "■ 止める"; play(R.playing && R.tl.includes(R.playing) ? R.playing : R.tl[0]); }
function stopAll(){ R.playAll = false; try { if (R.audio) { R.audio.onended = null; R.audio.pause(); } } catch (e) {} R.playing = null; markPlaying(); const b = $("playAll"); if (b) b.textContent = "▶ 続けて再生"; }
start().catch(e => { $("detail").textContent = "読み込めません: " + e.message; });
</script></body></html>
"""
