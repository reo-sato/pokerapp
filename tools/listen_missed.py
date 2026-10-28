"""読めなかったコール・フォールドを聞いて確かめるページ（店舗 2026-10-26 のレビュー）。

真のアクションのうち配る人が言う行（`eval_store.truth_rows`）で、発話の読みと対応しなかったコール・フォールド
（`--kinds` で変えられる）ごとに、その前後 = 対応した読みの発話のあいだの発話を、音声つきで 1 枚の HTML に並べる。
聞いて「言った（どの発話か）/ 言っていない / 分からない」を付けると、ページが JSON に書き出す。`--read` でまとめる。

読めない理由が「言っていない」（ディーラーの言い方で直す）か「言ったのに落ちた」（マイク・書き起こしで直す）かを
分けるため。店舗の音声が入るので公開しない（チャットで送る）。

    TZ=Asia/Tokyo python tools/listen_missed.py pokerlogs_<日時>_1of9.zip … --truth pokertruth_<日時>.zip --out listen.html
    python tools/listen_missed.py --read listen_<sid8>.json
"""
from __future__ import annotations

import argparse
import base64
import html
import json
import logging
import shutil
import subprocess
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.eval_store import (  # noqa: E402
    _is_noise, _read_json, _read_jsonl, _speech_token, find_sessions, hand_windows, heard_items, is_excluded,
    matched_pairs, open_input, truth_hands, truth_rows, utterance_kind,
)

DEFAULT_KINDS = ("call", "fold")
_ACTION_NAMES = {"fold": "フォールド", "check": "チェック", "call": "コール", "bet": "ベット", "raise": "レイズ",
                 "allin": "オールイン"}
_KIND_NAMES = {"action": "アクション", "chatter": "雑談", "empty": "声ではない", "stock": "幻聴（定型句）",
               "echo": "幻聴（プロンプト）"}
# 1 つの場面に並べる発話の上限（前後の読めた発話のあいだが長いハンドの序盤）。多ければ真ん中寄りを残す
MAX_UTTERANCES = 16


_STREET_NAMES = {"preflop": "プリフロップ", "flop": "フロップ", "turn": "ターン", "river": "リバー", "showdown": "ショーダウン"}


def with_streets(hand: dict, record_hand: Optional[dict]) -> dict:
    """真のアクションの行にストリートが無ければ（家で入れた真のアクション）、記録の卓（真のアクションの席・ボタン）で
    流して入れる。流せなかった行から先はストリート無しのまま。"""
    actions = [a for a in hand.get("actions") or [] if isinstance(a, dict)]
    if all(a.get("street") for a in actions):
        return hand
    applied: list[dict] = []
    if record_hand:
        from core.truth_hand import apply_rows, start_state, with_table

        try:
            gs = start_state(with_table(record_hand, hand.get("players")), hand.get("button_seat"))
            applied = apply_rows(gs, actions)[0]
        except Exception:  # noqa: BLE001 — 流せない卓（記録のブラインド・席が違う など）は下の最初の入力で
            applied = []
    # 記録を見ずに入れた最初の入力（`blind_entry`）にはストリートがある。同じ席・同じアクションの行はそれを使う
    blind = [a for a in ((hand.get("blind_entry") or {}).get("actions") or []) if isinstance(a, dict)]
    filled = []
    for i, a in enumerate(actions):
        street = a.get("street") or (applied[i].get("street") if i < len(applied) else None)
        if not street and i < len(blind) and (blind[i].get("seat"), blind[i].get("action")) == (
                a.get("seat"), a.get("action")):
            street = blind[i].get("street")
        filled.append(dict(a, street=street) if street else a)
    return {**hand, "actions": filled}


def _row_text(action: dict) -> str:
    name = _ACTION_NAMES.get(action.get("action"), action.get("action") or "")
    amount = action.get("amount")
    street = _STREET_NAMES.get(action.get("street") or "", "")
    return f"{street + ' ' if street else ''}席{action.get('seat')} {name}" + (
        f" {amount}" if amount and action.get("action") in ("bet", "raise") else "")


def missed_spots(hand: dict, transcripts: list[dict], window: tuple[float, float],
                 kinds: tuple[str, ...] = DEFAULT_KINDS) -> list[dict]:
    """1 ハンドの読めなかった行（`kinds`）の場面。続けて読めなかった行で前後の発話が同じものは 1 つの場面にする。"""
    rows = truth_rows(hand)
    tokens = [_speech_token(a["action"], a.get("amount")) for a in rows]
    items = heard_items(transcripts, window)
    pairs = matched_pairs(tokens, [t for t, _ in items])
    spots: list[dict] = []
    for k, action in enumerate(rows):
        if k in pairs or action.get("action") not in kinds:
            continue
        before = [i for i in pairs if i < k]
        after = [i for i in pairs if i > k]
        lo = items[pairs[max(before)]][1]["utterance_start_ts"] if before else window[0]
        if after:
            nxt = items[pairs[min(after)]][1]
            hi = nxt["utterance_start_ts"] + 0.001
        else:
            hi = window[1]
        missed = {"index": k, "text": _row_text(action), "action": action.get("action")}
        if spots and spots[-1]["lo"] == lo and spots[-1]["hi"] == hi:
            spots[-1]["missed"].append(missed)
            continue
        utts = sorted((r for r in transcripts if isinstance(r.get("utterance_start_ts"), (int, float))
                       and lo <= r["utterance_start_ts"] < hi), key=lambda r: r["utterance_start_ts"])
        dropped = max(0, len(utts) - MAX_UTTERANCES)
        if dropped:
            cut = dropped // 2
            utts = utts[cut:cut + MAX_UTTERANCES]
        spots.append({
            "hand_id": hand.get("hand_id"), "lo": lo, "hi": hi, "missed": [missed], "dropped": dropped,
            "context": [{"text": _row_text(a), "read": i in pairs} for i, a in enumerate(rows)],
            "utterances": [_utterance(r, window[0]) for r in utts],
        })
    for spot in spots:
        spot["context_marks"] = [m["index"] for m in spot["missed"]]
    return spots


def _utterance(row: dict, hand_start: float) -> dict:
    text = (row.get("text") or "").strip()
    return {
        "file": row.get("audio_file"), "at": round(row["utterance_start_ts"] - hand_start, 1),
        "sec": round(float(row.get("audio_sec") or 0.0), 1), "text": text,
        "ear": ((row.get("ear") or {}).get("text") or "").strip(),
        "kind": utterance_kind(row), "noise": _is_noise(row, text),
    }


# ――― 音声 ―――

def find_audio(root: Path, session_id: str, name: str) -> Optional[Path]:
    """発話の WAV（pack_logs の zip = `<sid>/audio/<ms>.wav`、ロガーの logs = `audio/<sid>/<ms>.wav`）。"""
    for candidate in (root / session_id / "audio" / name, root / "audio" / session_id / name):
        if candidate.is_file():
            return candidate
    found = next(iter(root.rglob(f"{session_id}*/**/{name}")), None) or next(iter(root.rglob(f"audio/{name}")), None)
    return found if found is not None and found.is_file() else None


def encode_audio(path: Path) -> str:
    """ページに入れる data URI。ffmpeg があれば小さい mp3（16 kHz・32 kbps）、無ければ WAV のまま。"""
    if shutil.which("ffmpeg"):
        try:
            mp3 = subprocess.run(
                ["ffmpeg", "-loglevel", "error", "-i", str(path), "-ac", "1", "-ar", "16000", "-b:a", "32k",
                 "-f", "mp3", "pipe:1"], capture_output=True, check=True, timeout=60).stdout
            if mp3:
                return "data:audio/mpeg;base64," + base64.b64encode(mp3).decode("ascii")
        except (OSError, subprocess.SubprocessError):
            pass
    return "data:audio/wav;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


# ――― ページ ―――

def build_page(session_id: str, started: str, spots: list[dict], audio: dict[str, str]) -> str:
    data = {"session_id": session_id, "started": started, "spots": spots}
    counts = Counter(m["action"] for s in spots for m in s["missed"])
    summary = "・".join(f"{_ACTION_NAMES.get(k, k)} {v}" for k, v in counts.most_common())
    return (TEMPLATE
            .replace("__TITLE__", html.escape(f"聞き取りの確認 {session_id[:8]}"))
            .replace("__SUMMARY__", html.escape(summary))
            .replace("__STARTED__", html.escape(started))
            .replace("__DATA__", json.dumps(data, ensure_ascii=False).replace("</", "<\\/"))
            .replace("__AUDIO__", json.dumps(audio).replace("</", "<\\/")))


def read_labels(path: Path) -> str:
    """ページが書き出した JSON をまとめる（種類ごとの 言った / 言っていない / 分からない と、言った発話の種類）。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    by_kind: dict[str, Counter] = {}
    said_as: Counter = Counter()
    for label in data.get("labels") or []:
        by_kind.setdefault(label.get("action") or "", Counter())[label.get("label") or "unlabeled"] += 1
        if label.get("label") == "said":
            said_as[label.get("utterance_kind") or "（発話を選んでいない）"] += 1
    lines = [f"セッション {data.get('session_id', '')[:8]}: 付けた {sum(sum(c.values()) for c in by_kind.values())}"]
    names = {"said": "言った", "not_said": "言っていない", "unknown": "分からない", "unlabeled": "未入力"}
    for kind, c in sorted(by_kind.items()):
        lines.append(f"  {_ACTION_NAMES.get(kind, kind)}: " + "・".join(f"{names.get(k, k)} {v}" for k, v in c.most_common()))
    if said_as:
        lines.append("  言った発話の書き起こし: " + "・".join(f"{_KIND_NAMES.get(k, k)} {v}" for k, v in said_as.most_common()))
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="読めなかったコール・フォールドを聞いて確かめるページを作る")
    ap.add_argument("path", type=Path, nargs="*", help="pack_logs の zip（分けた zip は全部）/ 展開したフォルダ / logs")
    ap.add_argument("--truth", type=Path, action="append", default=[],
                    help="真のアクションの入ったフォルダ・zip（家で入れた pokertruth_*.zip など。無ければログの中のもの）")
    ap.add_argument("--session", action="append", help="セッション ID（先頭の数文字でよい）")
    ap.add_argument("--kinds", default=",".join(DEFAULT_KINDS), help="確かめるアクション（既定 call,fold）")
    ap.add_argument("--out", type=Path, help="書き出す HTML（既定 listen_<sid 8 桁>.html）")
    ap.add_argument("--read", type=Path, help="ページが書き出した JSON をまとめる")
    args = ap.parse_args(argv)
    if args.read:
        print(read_labels(args.read))
        return 0
    if not args.path:
        ap.error("ログの zip かフォルダを渡してください")
    logging.disable(logging.WARNING)
    kinds = tuple(k.strip() for k in args.kinds.split(",") if k.strip())
    root, cleanup = open_input(list(args.path))
    truth_root, truth_cleanup = open_input(list(args.truth)) if args.truth else (None, None)
    try:
        sessions = find_sessions(root, args.session)
        made = 0
        for files in sessions:
            gt_path = files.path(".ground_truth.json")
            if truth_root is not None:
                gt_path = next(iter(truth_root.rglob(f"{files.session_id}.ground_truth.json")), gt_path)
            gt = truth_hands(_read_json(gt_path))
            record = _read_json(files.path(".json")) or {}
            transcripts = _read_jsonl(files.path(".transcripts.jsonl"))
            if not gt["hands"] or not transcripts:
                continue
            live = [h for h in record.get("hands") or [] if isinstance(h, dict)]
            windows = hand_windows(live)
            by_id = {h.get("hand_id"): h for h in live}
            spots = []
            for hand in gt["hands"]:
                if hand.get("hand_id") in windows and not is_excluded(hand):
                    hand = with_streets(hand, by_id.get(hand["hand_id"]))
                    spots += missed_spots(hand, transcripts, windows[hand["hand_id"]], kinds)
            if not spots:
                continue
            audio: dict[str, str] = {}
            missing = 0
            for spot in spots:
                for u in spot["utterances"]:
                    if u["file"] and u["file"] not in audio:
                        wav = find_audio(files.folder, files.session_id, u["file"]) or find_audio(
                            root, files.session_id, u["file"])
                        if wav is None:
                            missing += 1
                            continue
                        audio[u["file"]] = encode_audio(wav)
            first = min((s["lo"] for s in spots), default=0.0)
            started = datetime.fromtimestamp(first).strftime("%Y-%m-%d %H:%M")
            out = args.out if args.out and len(sessions) == 1 else Path(f"listen_{files.session_id[:8]}.html")
            out.write_text(build_page(files.session_id, started, spots, audio), encoding="utf-8")
            n = sum(len(s["missed"]) for s in spots)
            print(f"{out}: {files.session_id[:8]} 読めなかった行 {n}（場面 {len(spots)}）・音声 {len(audio)}"
                  + (f"（見つからない {missing}）" if missing else "") + f"・{out.stat().st_size / 1e6:.1f} MB")
            made += 1
        if not made:
            print("真のアクションと書き起こしのあるセッションがありません", file=sys.stderr)
            return 1
    finally:
        for tmp in (cleanup, truth_cleanup):
            if tmp is not None:
                shutil.rmtree(tmp, ignore_errors=True)
    return 0


TEMPLATE = r"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--ink:#1c2430;--muted:#5d6878;--line:#dde2e8;--accent:#1f6feb;--miss:#c2410c;
--miss-bg:#fff1e8;--ok:#15803d;--chip:#eef1f5;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#11151b;--card:#1a2029;--ink:#e6eaf0;--muted:#9aa5b4;--line:#2c3441;
--accent:#58a6ff;--miss:#fb923c;--miss-bg:#3a2416;--ok:#4ade80;--chip:#232b36;color-scheme:dark}}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 system-ui,-apple-system,"Hiragino Sans","Yu Gothic UI",sans-serif;padding:0 16px 80px}
main{max-width:860px;margin:0 auto}
header{padding:20px 0 8px}
h1{font-size:20px;margin:0 0 4px}
.sub{color:var(--muted);font-size:13px}
.how{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin:12px 0;font-size:14px}
.bar{position:sticky;top:0;background:var(--bg);padding:8px 0;display:flex;gap:8px;flex-wrap:wrap;align-items:center;z-index:2;border-bottom:1px solid var(--line)}
button,select,input[type=text]{font:inherit;color:inherit}
button{background:var(--chip);border:1px solid var(--line);border-radius:8px;padding:6px 12px;cursor:pointer}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
.progress{margin-left:auto;color:var(--muted);font-variant-numeric:tabular-nums}
.spot{background:var(--card);border:1px solid var(--line);border-radius:10px;margin:14px 0;padding:12px 14px}
.spot h2{font-size:16px;margin:0 0 6px;display:flex;gap:8px;align-items:baseline;flex-wrap:wrap}
.ctx{display:flex;flex-wrap:wrap;gap:4px;margin:6px 0 10px;font-size:12px}
.ctx span{background:var(--chip);border-radius:6px;padding:1px 6px;color:var(--muted)}
.ctx span.read{color:var(--ok)}
.ctx span.miss{background:var(--miss-bg);color:var(--miss);font-weight:600}
table{width:100%;border-collapse:collapse;font-size:13px}
td{border-top:1px solid var(--line);padding:4px 6px;vertical-align:top}
td.n,td.t{white-space:nowrap;font-variant-numeric:tabular-nums;color:var(--muted)}
td.k{white-space:nowrap;color:var(--muted);font-size:12px}
.ear{color:var(--muted)}
.playing td{background:var(--miss-bg)}
.label{border-top:1px dashed var(--line);margin-top:8px;padding-top:8px;display:grid;gap:6px}
.label .what{font-weight:600;color:var(--miss)}
.choices{display:flex;flex-wrap:wrap;gap:10px;align-items:center}
.choices label{display:flex;gap:4px;align-items:center}
input[type=text]{border:1px solid var(--line);border-radius:6px;padding:4px 8px;background:var(--bg);width:100%;box-sizing:border-box}
.scroll{overflow-x:auto}
.note{font-size:12px;color:var(--muted)}
</style></head><body><main>
<header><h1>__TITLE__</h1>
<div class="sub">__STARTED__ ・ 読めなかった行: __SUMMARY__</div></header>
<div class="how">真のアクションのうち、書き起こしの読みと合わなかった行の前後の発話です（前後の読めた発話のあいだ）。
▶ で 1 つずつ、「まとめて聞く」で順に聞けます。行ごとに、ディーラーが<b>言った</b>（どの発話か）／<b>言っていない</b>／<b>分からない</b>を選んでください。
入力はこのブラウザに残ります。終わったら「結果を書き出す」でファイル（JSON）を保存して送ってください（保存できなければ「結果をコピー」）。</div>
<div class="bar"><button id="all">全部</button><button id="calls">コールだけ</button><button id="folds">フォールドだけ</button>
<button class="primary" id="save">結果を書き出す</button><button id="copy">結果をコピー</button><span class="progress" id="progress"></span></div>
<div id="spots"></div>
<textarea id="copybox" hidden style="width:100%;height:120px"></textarea>
</main>
<script id="data" type="application/json">__DATA__</script>
<script id="audio" type="application/json">__AUDIO__</script>
<script>
const DATA=JSON.parse(document.getElementById('data').textContent);
const AUDIO=JSON.parse(document.getElementById('audio').textContent);
const KIND={action:'アクション',chatter:'雑談',empty:'声ではない',stock:'幻聴(定型句)',echo:'幻聴(プロンプト)'};
const KEY='listen_'+DATA.session_id;
let labels={};try{labels=JSON.parse(localStorage.getItem(KEY)||'{}')}catch(e){labels={}}
function store(){try{localStorage.setItem(KEY,JSON.stringify(labels))}catch(e){}progress()}
const player=new Audio();let queue=[],playingRow=null;
function play(file,row,rest){if(!AUDIO[file])return;queue=rest||[];if(playingRow)playingRow.classList.remove('playing');
playingRow=row;row&&row.classList.add('playing');player.src=AUDIO[file];player.play().catch(()=>{})}
player.onended=()=>{if(playingRow)playingRow.classList.remove('playing');playingRow=null;
if(queue.length){const [f,r]=queue.shift();play(f,r,queue)}};
function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
const root=document.getElementById('spots');
DATA.spots.forEach((s,si)=>{
 const el=document.createElement('section');el.className='spot';el.dataset.kinds=s.missed.map(m=>m.action).join(' ');
 const marks=new Set(s.context_marks);
 el.innerHTML=`<h2>ハンド ${s.hand_id}<span class="note">読めなかった: ${s.missed.map(m=>esc(m.text)).join('、')}</span></h2>
 <div class="ctx">${s.context.map((c,i)=>`<span class="${marks.has(i)?'miss':(c.read?'read':'')}">${esc(c.text)}</span>`).join('')}</div>
 <div><button class="playall">▶ まとめて聞く</button> <span class="note">${s.utterances.length} 発話${s.dropped?`（長いので前後 ${s.dropped} を省略）`:''}</span></div>
 <div class="scroll"><table>${s.utterances.map((u,ui)=>`<tr data-file="${esc(u.file||'')}"><td class="n">${ui+1}</td>
 <td><button class="p" ${AUDIO[u.file]?'':'disabled'}>▶</button></td><td class="t">+${u.at}s・${u.sec}s</td>
 <td>${esc(u.text||'（空）')}<div class="ear">耳: ${esc(u.ear||'—')}</div></td><td class="k">${KIND[u.kind]||u.kind}</td></tr>`).join('')}</table></div>
 ${s.missed.map(m=>{const id=`${s.hand_id}:${m.index}`;return `<div class="label" data-id="${id}" data-action="${m.action}">
 <div class="what">${esc(m.text)}</div><div class="choices">
 <label><input type="radio" name="r${id}" value="said">言った</label>
 <select class="utt"><option value="">どの発話？</option>${s.utterances.map((u,ui)=>`<option value="${ui}">${ui+1}: ${esc((u.text||'（空）').slice(0,16))}</option>`).join('')}</select>
 <label><input type="radio" name="r${id}" value="not_said">言っていない</label>
 <label><input type="radio" name="r${id}" value="unknown">分からない</label></div>
 <input type="text" class="memo" placeholder="メモ（任意）"></div>`}).join('')}`;
 root.appendChild(el);
 const rows=[...el.querySelectorAll('tr')];
 rows.forEach((r,ui)=>{const b=r.querySelector('button.p');b&&b.addEventListener('click',()=>play(r.dataset.file,r,[]))});
 el.querySelector('.playall').addEventListener('click',()=>{const list=rows.filter(r=>AUDIO[r.dataset.file]).map(r=>[r.dataset.file,r]);
  if(list.length){const [f,r]=list.shift();play(f,r,list)}});
 el.querySelectorAll('.label').forEach(lb=>{const id=lb.dataset.id;const cur=labels[id]||{};
  if(cur.label){const x=lb.querySelector(`input[value="${cur.label}"]`);if(x)x.checked=true}
  if(cur.utt!=null)lb.querySelector('.utt').value=String(cur.utt);if(cur.memo)lb.querySelector('.memo').value=cur.memo;
  const save=()=>{const r=lb.querySelector('input[type=radio]:checked');const sel=lb.querySelector('.utt').value;
   let label=r?r.value:'';if(sel!==''&&!label){label='said';lb.querySelector('input[value="said"]').checked=true}
   labels[id]={label,utt:sel===''?null:Number(sel),memo:lb.querySelector('.memo').value,spot:si,action:lb.dataset.action};store()};
  lb.querySelectorAll('input,select').forEach(x=>x.addEventListener('change',save));lb.querySelector('.memo').addEventListener('input',save)})});
function progress(){const all=document.querySelectorAll('.label').length;const done=Object.values(labels).filter(l=>l.label).length;
 document.getElementById('progress').textContent=`入力 ${done} / ${all}`}
progress();
function filter(k){document.querySelectorAll('.spot').forEach(s=>{s.hidden=k&&!s.dataset.kinds.split(' ').includes(k)})}
document.getElementById('all').onclick=()=>filter('');document.getElementById('calls').onclick=()=>filter('call');
document.getElementById('folds').onclick=()=>filter('fold');
function result(){return JSON.stringify({session_id:DATA.session_id,labels:Object.entries(labels).map(([id,l])=>{
 const [h,i]=id.split(':').map(Number);const s=DATA.spots[l.spot]||{utterances:[]};const u=l.utt==null?null:s.utterances[l.utt];
 const m=(s.missed||[]).find(x=>x.index===i)||{};
 return {hand_id:h,index:i,action:l.action,text:m.text,label:l.label||'',utterance:u?u.file:null,
  utterance_text:u?u.text:null,utterance_kind:u?u.kind:null,memo:l.memo||''}})},null,1)}
document.getElementById('save').onclick=()=>{const blob=new Blob([result()],{type:'application/json'});
 const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=`listen_${DATA.session_id.slice(0,8)}.json`;
 document.body.appendChild(a);a.click();a.remove()};
document.getElementById('copy').onclick=()=>{const t=result();const box=document.getElementById('copybox');
 (navigator.clipboard?navigator.clipboard.writeText(t):Promise.reject()).catch(()=>{box.hidden=false;box.value=t;box.select()})};
</script></body></html>
"""


if __name__ == "__main__":
    sys.exit(main())
