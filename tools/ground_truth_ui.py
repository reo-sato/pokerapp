"""tools/ground_truth_ui.py

真のアクション入力 — 記録したハンドを 1 つずつ見て、**実際に起きたアクション列**（ground truth）を
入れる LAN 上のブラウザ画面（iPad / スマホ / PC）。Phase A 計測（`docs/dogfood/measurement-plan.md`）の
`logs/{session_id}.ground_truth.json` を書く。

- hand logger（`main.py --cli`）が書く `logs/{session_id}.json` を**読むだけ**（ゲーム状態は触らない。
  卓モニタと同じ型 = 単一書き手 + reload-on-read）。ground truth ファイルはこの画面だけが書く。
- 記録が合っていれば **1 タップ（「記録どおり」= captured-passthrough）**。要確認のハンドは内容を確かめて
  「保存」する（ADR-0043 C-2 ガード = staff API と同じ規則）。
- **手番・ストリート・コールの額は pokerkit で補う**。入力するのは席とアクション、ベット / レイズの
  額（トータル）だけ。合法でない列は行を赤くして知らせる。
- 保存のたびに `tools/measure_capture_accuracy.py` と同じ計算で一致 / 差分を出す。
- 手間を減らす（ADR-0056 追記 1 の S1）: ハンドの間の**発話の音声を再生**でき、書き起こし・ボードの札・札の
  離脱を時刻順に並べる。要確認の行を **✓ で確かめれば「記録どおり」**にできる。行ごとに「自信なし」を付けられる。
  **5 ハンドに 1 つはブラインド**（記録を見ずに入れる = 記録に引きずられていないかを測る）。入力にかかった時間を残す。

使い方:

    python tools/ground_truth_ui.py                          # http://127.0.0.1:8791/
    python tools/ground_truth_ui.py --host 0.0.0.0 --port 8791   # iPad から http://<PC の IP>:8791/

ハンドの一覧は 5 秒ごとに読み直すので、卓の脇でハンドが終わるたびに入れられる。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import unquote, urlsplit

# リポジトリ直下を import path に入れる（他の tools/ と同じ規約）。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.game_state import PlayerState  # noqa: E402
from core.ground_truth import (  # noqa: E402
    SOURCE_PASSTHROUGH,
    GroundTruthError,
    hand_has_needs_review,
    validate_source,
)
from core.ground_truth_repository import GroundTruthRepository  # noqa: E402
from core.hand_correction import apply_hand_corrections  # noqa: E402
from core.hand_correction_repository import HandCorrectionRepository  # noqa: E402
from core.hand_log import hand_street_flow  # noqa: E402
from core.poker_engine import PokerkitGameState  # noqa: E402
from tools.measure_capture_accuracy import HandAccuracy, measure_hand  # noqa: E402

logger = logging.getLogger(__name__)

GT_ACTIONS = ("fold", "check", "call", "bet", "raise", "allin")
_CARD_RE = re.compile(r"^[2-9TJQKA][shdc]$")
_SESSION_RE = re.compile(r"^[A-Za-z0-9_\-]{1,120}$")
# スタックが記録に無い / 0 の席の再現用（pokerkit はスタック 0 を受け付けない。GT の目的は手番と
# ストリートの補完なので、十分大きければよい）
_FALLBACK_STACK = 10_000_000
_MAX_BODY = 1_000_000
# ブラインドで入れるハンドの割合（N ハンドに 1 つ。0 = しない）。テスト方針 週 1（2026-09-30）: 全部のハンドを先に
# 記録を見ずに入れ、保存したあとで記録と照らし合わせて直す（ADR-0056 追記 1 の 2 割から変更。記憶で入れると誤るので、
# 照らし合わせで正解を直しつつ、記録を見ずに入れた内容で入れる側の誤りと記録への引きずられを測る）
BLIND_EVERY = 1
_AUDIO_RE = re.compile(r"^[0-9]{6,16}\.wav$")
# 台本のハンドのセッション（`tools/test_script.py`）。正解は台本なので入力は要らない
_SCRIPT_SUFFIX = ".script.json"
# タイムラインに出す範囲: ハンドの始まり（配布）の少し前から、次のハンドの始まりまで
_TIMELINE_BEFORE_SEC = 10.0
_TIMELINE_AFTER_SEC = 20.0

# ───────────────────────── 読み取り ─────────────────────────


def _load_hand_log(log_dir: Path, session_id: str) -> Optional[dict]:
    """`logs/{session_id}.json` を読む。不在・破損・書き込み途中は None（次の読み直しで読める）。"""
    path = log_dir / f"{session_id}.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("hands"), list):
        return None
    return data


def _hands_of(log: dict) -> list[dict]:
    return sorted(
        (h for h in log["hands"] if isinstance(h, dict) and isinstance(h.get("hand_id"), int)),
        key=lambda h: h["hand_id"],
    )


def _apply_corrections(
    hand: dict, session_id: str, corr_repo: Optional[HandCorrectionRepository],
) -> dict:
    if corr_repo is None:
        return hand
    try:
        corrections = corr_repo.list_for_hand(session_id, hand["hand_id"])
    except Exception:  # noqa: BLE001 — 訂正が読めなくても入力は続ける
        return hand
    return apply_hand_corrections(hand, corrections) if corrections else hand


def list_sessions(log_dir: Path, gt_repo: GroundTruthRepository) -> list[dict]:
    """`logs/` のハンドログ（sidecar を除く）を更新の新しい順に返す。"""
    out: list[tuple[float, dict]] = []
    for path in log_dir.glob("*.json"):
        if "." in path.stem or not path.is_file():      # x.table_state.json / x.ground_truth.json 等
            continue
        if not _SESSION_RE.match(path.stem):
            continue
        log = _load_hand_log(log_dir, path.stem)
        if log is None:
            continue
        hands = _hands_of(log)
        mtime = path.stat().st_mtime
        out.append((mtime, {
            "session_id": path.stem,
            "hands": len(hands),
            "annotated": len(gt_repo.list_for_session(path.stem)),
            "started_at": hands[0].get("started_at") if hands else None,
            "updated_at": datetime.fromtimestamp(mtime).isoformat(timespec="seconds"),
            "script": (log_dir / f"{path.stem}{_SCRIPT_SUFFIX}").is_file(),
        }))
    return [d for _, d in sorted(out, key=lambda t: t[0], reverse=True)]


def _accuracy_dict(acc: HandAccuracy) -> dict:
    mismatches = (acc.action_total - acc.action_correct) + (0 if acc.board_match else 1)
    if acc.winner_match is False:
        mismatches += 1
    return {
        "action_correct": acc.action_correct,
        "action_total": acc.action_total,
        "board_match": acc.board_match,
        "winner_match": acc.winner_match,
        "hole_correct": acc.hole_correct,
        "hole_total": acc.hole_total,
        "mismatches": mismatches,
        "all_match": mismatches == 0,
    }


def _hand_row(hand: dict, gt) -> dict:
    hand_id = hand["hand_id"]
    accuracy = None
    if gt is not None:
        accuracy = _accuracy_dict(measure_hand(dict(gt.hand, hand_id=hand_id), hand))
    return {
        "hand_id": hand_id,
        "started_at": hand.get("started_at"),
        "ended_at": hand.get("ended_at"),
        "seats": [p["seat"] for p in hand.get("players") or []
                  if isinstance(p, dict) and isinstance(p.get("seat"), int)],
        "board": [c for c in hand.get("board") or [] if isinstance(c, str)],
        "winner_seat": hand.get("winner_seat"),
        "winner_source": hand.get("winner_source"),
        "pot_total": hand.get("pot_total"),
        "review_required": bool(hand.get("review_required")),
        "has_needs_review": hand_has_needs_review(hand),
        "ground_truth": (
            {"source": gt.source, "annotated_at": gt.annotated_at, "annotator": gt.annotator,
             "blind": bool(gt.hand.get("blind")), "reconciled": bool(gt.hand.get("reconciled")),
             "entry_sec": gt.hand.get("entry_sec")}
            if gt is not None else None
        ),
        "accuracy": accuracy,
    }


def _average(values: list[Any]) -> Optional[float]:
    nums = [float(v) for v in values if isinstance(v, (int, float))]
    return round(sum(nums) / len(nums), 1) if nums else None


def list_hands(
    log_dir: Path, session_id: str, gt_repo: GroundTruthRepository,
    corr_repo: Optional[HandCorrectionRepository] = None,
) -> Optional[dict]:
    """セッションのハンド一覧（新しい順）と、入力済みハンドの一致の集計。"""
    log = _load_hand_log(log_dir, session_id)
    if log is None:
        return None
    rows = [
        _hand_row(_apply_corrections(h, session_id, corr_repo), gt_repo.get(session_id, h["hand_id"]))
        for h in _hands_of(log)
    ]
    annotated = [r for r in rows if r["accuracy"] is not None]
    summary = {
        "hands": len(rows),
        "annotated": len(annotated),
        "review": sum(1 for r in rows if r["has_needs_review"]),
        "action_correct": sum(r["accuracy"]["action_correct"] for r in annotated),
        "action_total": sum(r["accuracy"]["action_total"] for r in annotated),
        "board_match": sum(1 for r in annotated if r["accuracy"]["board_match"]),
        "winner_match": sum(1 for r in annotated if r["accuracy"]["winner_match"] is True),
        "winner_total": sum(1 for r in annotated if r["accuracy"]["winner_match"] is not None),
        "hands_match": sum(1 for r in annotated if r["accuracy"]["all_match"]),
        "blind": sum(1 for r in rows if r["ground_truth"] and r["ground_truth"].get("blind")),
        "entry_sec_avg": _average([r["ground_truth"].get("entry_sec") for r in rows if r["ground_truth"]]),
    }
    rows.sort(key=lambda r: r["hand_id"], reverse=True)
    return {"session_id": session_id, "hands": rows, "summary": summary,
            "script": (log_dir / f"{session_id}{_SCRIPT_SUFFIX}").is_file()}


def _get_hand(
    log_dir: Path, session_id: str, hand_id: int,
    corr_repo: Optional[HandCorrectionRepository] = None,
) -> Optional[dict]:
    log = _load_hand_log(log_dir, session_id)
    if log is None:
        return None
    for hand in _hands_of(log):
        if hand["hand_id"] == hand_id:
            return _apply_corrections(hand, session_id, corr_repo)
    return None


def _gt_actions(hand: dict) -> list[dict]:
    """GT の入力欄に出すアクション列（席 / アクション / 額だけ。ハンド終了などの記録は除く）。"""
    out = []
    for a in hand.get("actions") or []:
        if not isinstance(a, dict) or a.get("action") not in GT_ACTIONS:
            continue
        if not isinstance(a.get("seat"), int):
            continue
        out.append({"seat": a["seat"], "action": a["action"], "amount": int(a.get("amount") or 0)})
    return out


def _gt_cards(hand: dict) -> tuple[Optional[list], Optional[dict]]:
    """GT に入れたボードと手札（ショーダウンの勝者の判定に使う。無ければ記録の札）。"""
    board = hand.get("board") if isinstance(hand.get("board"), list) else None
    holes = {p["seat"]: p.get("hole_cards") or [] for p in hand.get("players") or []
             if isinstance(p, dict) and isinstance(p.get("seat"), int)}
    return board, holes or None


def _gt_button(hand: dict) -> Optional[int]:
    """GT で選んだボタンの席（ディーラーがボタンを動かし忘れたハンド, 2026-09-29）。無ければ記録のボタン。"""
    button = hand.get("button_seat")
    return button if isinstance(button, int) and not isinstance(button, bool) else None


def _epoch(iso: Any) -> Optional[float]:
    """記録の時刻（この PC の時刻の ISO 文字列）を epoch 秒に。"""
    if not isinstance(iso, str) or not iso:
        return None
    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return None


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue                      # 書き込み途中の行
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _next_started_at(log_dir: Path, session_id: str, hand_id: int) -> Optional[str]:
    log = _load_hand_log(log_dir, session_id)
    if log is None:
        return None
    later = [h for h in _hands_of(log) if h["hand_id"] > hand_id]
    return later[0].get("started_at") if later else None


def hand_timeline(log_dir: Path, session_id: str, hand: dict, next_started_at: Optional[str] = None) -> list[dict]:
    """ハンドの間の発話（書き起こし・自信・音声ファイル）・ボードの札・札の離脱を時刻順に（`t` = 配布からの秒）。"""
    start = _epoch(hand.get("started_at"))
    if start is None:
        return []
    end = _epoch(next_started_at)
    if end is None:
        end = (_epoch(hand.get("ended_at")) or start) + _TIMELINE_AFTER_SEC
    lo = start - _TIMELINE_BEFORE_SEC
    audio_dir = log_dir / "audio" / session_id
    items: list[dict] = []
    for x in _read_jsonl(log_dir / f"{session_id}.transcripts.jsonl"):
        t = x.get("utterance_start_ts")
        if not isinstance(t, (int, float)) or not lo <= t < end or x.get("no_speech"):
            continue
        parsed = []
        for e in x.get("events") or []:
            if isinstance(e, dict) and e.get("action"):
                parsed.append(str(e["action"]) + (f" {e['amount']}" if e.get("amount") else "")
                              + (f" 席{e['seat']}" if e.get("seat") else ""))
        audio = x.get("audio_file")
        heard = x.get("heard_at")
        items.append({
            "t": round(t - start, 1), "kind": "speech", "text": str(x.get("text") or ""),
            "confidence": round(float(x["confidence"]), 2) if isinstance(x.get("confidence"), (int, float)) else None,
            "noise": bool(x.get("noise")), "question": bool(x.get("question")), "parsed": parsed,
            "lag": round(heard - t, 1) if isinstance(heard, (int, float)) else None,
            "audio": audio if isinstance(audio, str) and _AUDIO_RE.match(audio) and (audio_dir / audio).is_file()
            else None,
        })
    for x in _read_jsonl(log_dir / f"{session_id}.events.jsonl"):
        if x.get("type") != "rfid":
            continue
        kind = x.get("kind") or "card"
        t = x.get("observed_at") if kind in ("leave", "muck") and x.get("observed_at") else x.get("timestamp")
        if not isinstance(t, (int, float)) or not lo <= t < end:
            continue
        if kind == "card" and x.get("role") == "board" and x.get("board_index"):
            text = f"ボード {x['board_index']} 枚目 {x.get('card') or '?'}"
            if x.get("replaces"):
                text += f"（{x['replaces']} を差し替え）"
            items.append({"t": round(t - start, 1), "kind": "board", "text": text})
        elif kind in ("leave", "muck", "return") and isinstance(x.get("seat"), int):
            what = {"leave": "札が離れた", "muck": "札が中央を通過", "return": "札が戻った"}[kind]
            items.append({"t": round(t - start, 1), "kind": kind, "text": f"席{x['seat']} の{what}"})
    items.sort(key=lambda i: i["t"])
    return items


def is_blind(session_id: str, hand_id: int, every: int = BLIND_EVERY) -> bool:
    """このハンドを記録を見ずに入れるか（`every` ハンドに 1 つ。ハンドごとに決まっていて選べない）。"""
    if every <= 0:
        return False
    digest = hashlib.sha1(f"{session_id}:{hand_id}".encode("utf-8")).hexdigest()
    return int(digest, 16) % every == 0


def hand_detail(
    log_dir: Path, session_id: str, hand_id: int, gt_repo: GroundTruthRepository,
    corr_repo: Optional[HandCorrectionRepository] = None, blind_every: int = BLIND_EVERY,
) -> Optional[dict]:
    captured = _get_hand(log_dir, session_id, hand_id, corr_repo)
    if captured is None:
        return None
    _add_street_totals(captured)
    gt = gt_repo.get(session_id, hand_id)
    script = (log_dir / f"{session_id}{_SCRIPT_SUFFIX}").is_file()   # 台本のハンドは正解が台本（入力は要らない）
    blind = gt is None and not script and is_blind(session_id, hand_id, blind_every)
    initial = _gt_actions(gt.hand) if gt is not None else ([] if blind else _gt_actions(captured))
    board, holes = _gt_cards(gt.hand) if gt is not None else (None, None)
    button = _gt_button(gt.hand) if gt is not None else None
    legal = replay_legal(captured, initial, board=board, holes=holes, button=button)
    # 開いた時点で食い違いを見せる（入れ終わったハンドを見直すときに、編集しなくても出る）
    legal["lint"] = hand_lint(captured, legal["actions"], legal["next"], board, holes, show_record=not blind)
    return {
        "session_id": session_id,
        "captured": captured,
        "ground_truth": gt.to_dict() if gt is not None else None,
        "has_needs_review": hand_has_needs_review(captured),
        "legal": legal,
        "blind": blind,
        "script": script,
        "timeline": hand_timeline(log_dir, session_id, captured, _next_started_at(log_dir, session_id, hand_id)),
    }


def _add_street_totals(captured: dict) -> None:
    """記録の各アクションに `total`（そのストリートでその人が出した合計）を付ける。画面はコールをこの額で見せる
    （オーナー, 2026-09-29: コールは追加額ではなくトータル。記録・真のアクションの `amount` は追加額のまま）。"""
    actions = [a for a in captured.get("actions") or [] if isinstance(a, dict)]
    for a, (_, total) in zip(actions, hand_street_flow(captured)):
        if total is not None:
            a["total"] = total


# ───────────────────────── pokerkit で手番・ストリート・額を補う ─────────────────────────


def _players_for_replay(captured: dict) -> list[PlayerState]:
    players: list[PlayerState] = []
    for p in captured.get("players") or []:
        if not isinstance(p, dict) or not isinstance(p.get("seat"), int):
            continue
        stack = p.get("stack_start")
        stack = int(stack) if isinstance(stack, (int, float)) and stack > 0 else _FALLBACK_STACK
        players.append(PlayerState(seat=p["seat"], name=str(p.get("name") or f"席{p['seat']}"), stack=stack))
    return sorted(players, key=lambda p: p.seat)


def _showdown_muck_error(active: list[int], seat: int, act: str) -> Optional[str]:
    """ベッティングが終わったあとの行を入れられない理由（入れられるなら None）。

    入れられるのは、ショーダウンに残っている人が手札を見せずに降りた（マック）フォールドだけ（ADR-0062。
    ライブの記録も street=showdown の fold として残す）。最後の 1 人は降りられない（その人の勝ち）。
    """
    if act != "fold":
        return "ベッティングは終わっています（この行は入りません。ショーダウンで見せずに降りた人はフォールド）"
    if len(active) < 2:
        return "ほかの人はもう降りています（この行は入りません）"
    if seat not in active:
        return f"席{seat} はショーダウンに残っていません（残っているのは席 {'・'.join(map(str, active))}）"
    return None


def replay_legal(
    captured: dict, actions: list[dict], *, board: Optional[list] = None, holes: Optional[dict] = None,
    button: Optional[int] = None,
) -> dict:
    """GT のアクション列を pokerkit で流し、各行のストリートと額（コールは自動）、次の手番を返す。

    返り値: `{"actions": [{seat, action, amount, total, street}], "next": {...} | None, "error": {index, message} | None}`。
    `amount` はコールなら追加額（記録と同じ）、`total` はそのストリートでその人が出した合計（画面の表示用。コールは
    トータルで見せる）。
    `error` はその行から先を反映できなかった理由（手番違い・額の範囲外など）。`next` は最後に反映できた
    ところの手番（`hand_over` ならベッティングは終わり、`foldout_winner` はほかが全員降りた勝者）。
    ベッティングが終わったあとのフォールドは、ショーダウンで手札を見せずに降りた（マック）として
    street=showdown で入る（ライブの記録と同じ形, ADR-0062）。
    `button` はこのハンドのボタンの席（無ければ記録のボタン。ディーラーがボタンを動かし忘れたハンドは、
    実際のボタンを選ぶと手番の順がそれに合う, 2026-09-29）。
    """
    players = _players_for_replay(captured)
    if len(players) < 2:
        return {"actions": [], "next": None, "error": {"index": -1, "message": "記録に席が 2 つ未満です"}}
    blinds = captured.get("blinds") if isinstance(captured.get("blinds"), dict) else {}
    try:
        sb = max(1, int(blinds.get("sb") or 1))
        bb = max(sb, int(blinds.get("bb") or 2))
    except (TypeError, ValueError):
        sb, bb = 1, 2
    try:
        gs = PokerkitGameState(players, sb, bb)
        if button is None:
            button = captured.get("button_seat")
        if isinstance(button, int) and any(p.seat == button for p in players):
            gs.set_button(button)
        gs.new_hand()
    except (ImportError, ValueError, RuntimeError) as e:
        return {"actions": [], "next": None,
                "error": {"index": -1, "message": f"pokerkit で再現できません: {e}"}}

    out: list[dict] = []
    error: Optional[dict] = None
    mucked: list[int] = []                   # ショーダウンで手札を見せずに降りた席

    def showdown_seats() -> list[int]:
        try:
            return [s for s in gs.get_active_seats() if s not in mucked]
        except Exception:  # noqa: BLE001
            return []

    for i, raw in enumerate(actions):
        if not isinstance(raw, dict):
            error = {"index": i, "message": "行の形が不正です"}
            break
        act = str(raw.get("action") or "").lower()
        try:
            seat = int(raw.get("seat"))
        except (TypeError, ValueError):
            error = {"index": i, "message": "席が要ります"}
            break
        if act not in GT_ACTIONS:
            error = {"index": i, "message": f"不明なアクション: {act!r}"}
            break
        ctx = gs.legal_context()
        if ctx.actor_seat is None:
            reason = _showdown_muck_error(showdown_seats(), seat, act)
            if reason is not None:
                error = {"index": i, "message": reason}
                break
            mucked.append(seat)
            out.append({"seat": seat, "action": "fold", "amount": 0, "street": "showdown"})
            continue
        if seat != ctx.actor_seat:
            error = {"index": i, "message": f"手番は席{ctx.actor_seat} です（席{seat} の番ではありません）"}
            break
        street = gs.street
        amount = 0
        total = 0
        try:
            if act == "fold":
                if "fold" in ctx.legal_actions:
                    gs.apply_action(seat, "fold")
                else:
                    gs.force_fold(seat)              # チェックできるときに降りた（実際にある）
            elif act in ("check", "call"):
                act = "call" if ctx.amount_to_call > 0 else "check"
                amount = ctx.amount_to_call
                total = ctx.committed + ctx.amount_to_call if act == "call" else 0
                gs.apply_action(seat, act)
            elif act == "allin":
                amount = ctx.max_raise if ctx.max_raise else ctx.amount_to_call
                total = ctx.max_raise if ctx.max_raise else ctx.committed + ctx.amount_to_call
                gs.apply_action(seat, "allin")
            else:
                try:
                    amount = int(raw.get("amount") or 0)
                except (TypeError, ValueError):
                    amount = 0
                if "raise" in ctx.legal_actions:
                    act = "raise"
                elif "bet" in ctx.legal_actions:
                    act = "bet"
                else:
                    raise ValueError("ここではベット / レイズできません")
                if amount <= 0:
                    raise ValueError(f"額（トータル）を入れてください（最小 {ctx.min_raise} / 最大 {ctx.max_raise}）")
                try:
                    gs.apply_action(seat, act, amount)
                except ValueError:
                    raise ValueError(f"額 {amount} は使えません（最小 {ctx.min_raise} / 最大 {ctx.max_raise}）") from None
                total = amount
        except ValueError as e:
            error = {"index": i, "message": str(e)}
            break
        out.append({"seat": seat, "action": act, "amount": amount, "total": total, "street": street})

    ctx = gs.legal_context()
    active = showdown_seats()
    nxt = {
        "street": gs.street,
        "actor_seat": ctx.actor_seat,
        "legal_actions": sorted(ctx.legal_actions),
        "amount_to_call": ctx.amount_to_call,
        "call_total": ctx.committed + ctx.amount_to_call if ctx.amount_to_call else 0,
        "min_raise": ctx.min_raise,
        "max_raise": ctx.max_raise,
        "pot": gs.pot,
        "active_seats": active,
        "hand_over": ctx.actor_seat is None,
        "foldout_winner": active[0] if len(active) == 1 else None,
        "showdown_winner": None,
        "button_seat": gs.button_seat,
    }
    if ctx.actor_seat is None and len(active) >= 2:
        nxt["showdown_winner"] = _showdown_winner(captured, gs, active, board, holes)
    if ctx.actor_seat is None and mucked and len(active) == 1:
        # 見せずに降りた人のほうが手札が強い = 降りた席の入れ間違いのことが多い（店舗 a6ee12e4 ハンド 1: トリップスの
        # 勝者を「フォールド」にしていた）。ショーダウンの行は手番の順に縛られないので画面では止まらない
        strongest = _showdown_winner(captured, gs, active + mucked, board, holes)
        if strongest is not None and strongest in mucked:
            nxt["mucked_stronger"] = strongest
    return {"actions": out, "next": nxt, "error": error}


def _showdown_winner(
    captured: dict, gs: Any, active: list[int], board: Optional[list], holes: Optional[dict],
) -> Optional[int]:
    """ショーダウンの勝者を入力したボードと手札（無ければ記録の札）で判定する（オーナー 2026-09-29:
    勝った席が未定になってしまう。実際には確定している）。読めていない札があっても、どの札でも同じ勝者なら決める。"""
    from core.hand_log import UNKNOWN_CARD
    from core.showdown import award_with_unknown_cards

    cards = [c for c in (board if board is not None else captured.get("board") or [])
             if isinstance(c, str) and c and c != UNKNOWN_CARD]
    recorded = {p.get("seat"): p.get("hole_cards") or [] for p in captured.get("players") or []
                if isinstance(p, dict)}
    hands = {s: [c for c in ((holes or {}).get(s) if holes and s in holes else recorded.get(s)) or []
                 if isinstance(c, str) and c] for s in active}
    try:
        pots = gs.current_pots() or [{"amount": gs.pot, "eligible_seats": active}]
        decided = award_with_unknown_cards(hands, cards, pots, gs.acting_order())
    except Exception:  # noqa: BLE001 — 判定できなければ未定のまま（人が選ぶ）
        return None
    winners = decided[1][0] if decided and decided[1] else []
    return winners[0] if len(winners) == 1 else None   # 引き分けは 1 席を選べないので人が選ぶ


# ───────────────────────── 保存 ─────────────────────────


def _card_list(value: Any, what: str, limit: int) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise GroundTruthError(f"{what} はカードの配列です")
    cards = [c for c in value if c not in (None, "")]
    for c in cards:
        if not isinstance(c, str) or not _CARD_RE.match(c):
            raise GroundTruthError(f"{what} のカードが不正です: {c!r}（例: As, Td, 9h）")
    if len(cards) > limit:
        raise GroundTruthError(f"{what} は {limit} 枚までです")
    return cards


def validate_gt_hand(hand: Any) -> dict:
    """manual-edit の GT 本体を検査して、既知の項目だけに整えて返す（不正は GroundTruthError）。"""
    if not isinstance(hand, dict):
        raise GroundTruthError("hand はオブジェクトです")
    board = _card_list(hand.get("board"), "ボード", 5)
    actions_raw = hand.get("actions")
    if not isinstance(actions_raw, list):
        raise GroundTruthError("actions はアクションの配列です")
    actions = []
    for i, a in enumerate(actions_raw):
        if not isinstance(a, dict):
            raise GroundTruthError(f"actions[{i}] の形が不正です")
        try:
            seat = int(a.get("seat"))
            amount = int(a.get("amount") or 0)
        except (TypeError, ValueError):
            raise GroundTruthError(f"actions[{i}] の席 / 額が不正です") from None
        act = str(a.get("action") or "").lower()
        if seat < 1 or act not in GT_ACTIONS or amount < 0:
            raise GroundTruthError(f"actions[{i}] が不正です: 席{seat} {act} {amount}")
        row = {"seat": seat, "action": act, "amount": 0 if act in ("fold", "check") else amount}
        street = a.get("street")
        if isinstance(street, str) and street:
            row["street"] = street
        if a.get("unsure") is True:
            row["unsure"] = True             # 入れた人に自信が無い行（評価で分けて見る）
        actions.append(row)
    players = []
    seen_cards: dict[str, str] = {c: "ボード" for c in board}
    for i, p in enumerate(hand.get("players") or []):
        if not isinstance(p, dict):
            raise GroundTruthError(f"players[{i}] の形が不正です")
        try:
            seat = int(p.get("seat"))
        except (TypeError, ValueError):
            raise GroundTruthError(f"players[{i}] の席が不正です") from None
        holes = _card_list(p.get("hole_cards"), f"席{seat} の手札", 2)
        for c in holes:
            if c in seen_cards:
                raise GroundTruthError(f"カード {c} が {seen_cards[c]} と席{seat} の両方にあります")
            seen_cards[c] = f"席{seat}"
        entry: dict[str, Any] = {
            "seat": seat,
            "hole_cards": holes if holes else None,
            "showed_down": bool(p.get("showed_down")),
        }
        if isinstance(p.get("name"), str):
            entry["name"] = p["name"]
        players.append(entry)
    winner = hand.get("winner_seat")
    if winner is not None:
        try:
            winner = int(winner)
        except (TypeError, ValueError):
            raise GroundTruthError("winner_seat が不正です") from None
    out: dict[str, Any] = {"board": board, "actions": actions, "players": players}
    if winner is not None:
        out["winner_seat"] = winner
    button = hand.get("button_seat")
    if button is not None:
        if not isinstance(button, int) or isinstance(button, bool) or button < 1:
            raise GroundTruthError("button_seat が不正です")
        out["button_seat"] = button          # 実際のボタン（記録と違うとき = ディーラーが動かし忘れた）
    notes = hand.get("notes")
    if isinstance(notes, str) and notes.strip():
        out["notes"] = notes.strip()[:2000]
    if hand.get("blind") is True:
        out["blind"] = True                  # 記録を見ずに入れた
    return out


def _entry_sec(body: dict) -> Optional[float]:
    value = body.get("entry_sec")
    if isinstance(value, (int, float)) and 0 <= value <= 24 * 3600:
        return round(float(value), 1)
    return None


def save_ground_truth(
    log_dir: Path, session_id: str, hand_id: int, body: Any,
    gt_repo: GroundTruthRepository, corr_repo: Optional[HandCorrectionRepository] = None,
) -> tuple[int, dict]:
    """PUT の本体。staff API（ADR-0043）と同じ規則: passthrough は要確認のハンドを拒む。"""
    if not isinstance(body, dict):
        return 400, {"code": "invalid_amount", "message": "本体が JSON オブジェクトではありません"}
    captured = _get_hand(log_dir, session_id, hand_id, corr_repo)
    if captured is None:
        return 404, {"code": "not_found", "message": f"hand_id={hand_id} は {session_id} にありません"}
    source = str(body.get("source") or "")
    try:
        validate_source(source)
    except GroundTruthError as e:
        return 400, {"code": "invalid_amount", "message": str(e)}
    annotator = str(body.get("annotator") or "staff").strip()[:64] or "staff"
    if source == SOURCE_PASSTHROUGH:
        if hand_has_needs_review(captured) and not _review_confirmed(captured, body):
            return 400, {
                "code": "invalid_amount",
                "message": "要確認のハンドは、要確認の行をすべて ✓ で確かめてから「記録どおり」にしてください"
                           "（ADR-0043）。違っていれば直して「保存」してください。",
            }
        hand_body = dict(captured)
    else:
        try:
            hand_body = validate_gt_hand(body.get("hand"))
        except GroundTruthError as e:
            return 400, {"code": "invalid_amount", "message": str(e)}
    entry_sec = _entry_sec(body)
    if entry_sec is not None:
        hand_body["entry_sec"] = entry_sec      # 入力にかかった時間（手間を測る）
    _keep_blind_entry(hand_body, gt_repo.get(session_id, hand_id))
    entry = gt_repo.upsert(session_id, hand_id, hand_body, annotator=annotator, source=source)
    accuracy = _accuracy_dict(measure_hand(dict(entry.hand, hand_id=hand_id), captured))
    return 200, {"saved": entry.to_dict(), "accuracy": accuracy}


def _keep_blind_entry(hand_body: dict, existing: Any) -> None:
    """ブラインドで先に入れた内容を残す（テスト方針 週 1: 先に記録を見ずに入れ、そのあと記録と照らし合わせて直す）。

    ブラインドの保存では、入れたアクションと勝者を `blind_entry` にも写す。そのあとの保存（照らし合わせ）では前の
    `blind_entry` をそのまま持ち越し、`reconciled` を付ける = 最後の内容が正解、`blind_entry` は記録を見ずに入れた内容
    （入れる側の誤りの率と、記録に引きずられていないかを測る）。
    """
    if hand_body.get("blind") is True:
        hand_body["blind_entry"] = {
            "actions": [dict(a) for a in hand_body.get("actions") or []],
            "winner_seat": hand_body.get("winner_seat"),
            "entry_sec": hand_body.get("entry_sec"),
        }
        return
    previous = getattr(existing, "hand", None) or {}
    if isinstance(previous.get("blind_entry"), dict):
        hand_body["blind"] = True
        hand_body["blind_entry"] = previous["blind_entry"]
        hand_body["reconciled"] = True


_STREET_ORDER = ("preflop", "flop", "turn", "river")
_BOARD_STREET = {3: "flop", 4: "turn", 5: "river"}


def street_lint(captured: dict, rows: list[dict], nxt: Optional[dict], board: Optional[list],
                show_record: bool = True) -> list[str]:
    """真のアクションのストリートが、ボードの枚数・記録と食い違っていないか（店舗 9d1d8536 ハンド 2: 記憶で入れて
    フロップのチェック 3 つが抜け、ストリートが 1 つずれた）。見つけた食い違いを文で返す。

    `show_record=False`（ブラインドで入れている間）は記録を使う検査をしない（記録の中身を見せない）。
    """
    if not rows or nxt is None:
        return []
    msgs: list[str] = []
    slots = list(board if board is not None else captured.get("board") or [])
    # 配った枚数 = 最後に分かっている札の位置（途中の読めない札 "??" も配った札）
    dealt_n = max((i + 1 for i, c in enumerate(slots) if isinstance(c, str) and c and c != "??"), default=0)
    dealt = _BOARD_STREET.get(dealt_n)
    betting = [r for r in rows if r.get("street") in _STREET_ORDER]
    last = betting[-1]["street"] if betting else "preflop"
    if (dealt is not None and nxt.get("hand_over") and nxt.get("foldout_winner") is not None
            and _STREET_ORDER.index(dealt) > _STREET_ORDER.index(last)):
        msgs.append(f"ボードは {dealt_n} 枚（{_STREET_JA[dealt]}まで配った）のに、アクションは{_STREET_JA[last]}で"
                    "全員降りて終わっています。どこかのストリートのアクション（チェックなど）が抜けていませんか")
    if show_record:
        reached = {r.get("street") for r in rows}
        recorded = [a.get("street") for a in captured.get("actions") or [] if isinstance(a, dict)]
        # ボードに無いストリートの記録の行は記録の誤り（店舗 d0f055fb ハンド 2: 記録がストリートを 1 つ先に進めていた）
        missing = [s for s in _STREET_ORDER[1:] if s in recorded and s not in reached
                   and (dealt is None or _STREET_ORDER.index(s) <= _STREET_ORDER.index(dealt))]
        allin = nxt.get("hand_over") and nxt.get("foldout_winner") is None and _STREET_ORDER.index(last) < 3
        if missing and not allin:
            msgs.append("記録には" + "・".join(_STREET_JA[s] for s in missing) + "のアクションがあるのに、"
                        "こちらにはありません（ストリートがずれていないか、発話と札の時刻で確かめてください）")
    return msgs


def hand_lint(captured: dict, rows: list[dict], nxt: Optional[dict], board: Optional[list],
              holes: Optional[dict], show_record: bool = True) -> list[str]:
    """入れた真のアクションの食い違い（ストリート・ショーダウンで降りた席・札）を文で返す（2026-09-30 の洗い直しで
    見つかった入力ミスの形）。"""
    msgs = street_lint(captured, rows, nxt, board, show_record)
    seat = (nxt or {}).get("mucked_stronger")
    if seat is not None:
        msgs.append(f"席{seat} は手札が一番強いのに、ショーダウンで見せずに降りた（フォールド）ことになっています。"
                    "降りた席が合っているか確かめてください（見せずに降りたなら、そのままで構いません）")
    msgs.extend(card_lint(captured, board, holes))
    return msgs


def card_lint(captured: dict, board: Optional[list], holes: Optional[dict]) -> list[str]:
    """RFID が読んだ札（記録のボード・手札）が、入れた真のアクションの札に無い（店舗 d0f055fb ハンド 5: RFID は
    リバーを 9s と読み、9s のタグはほかのハンドでは毎回正しいのに、真のアクションは 9c）。札を直したのが正しい
    こともある（読めていない札・登録の誤り）ので、止めずに知らせるだけ。"""
    from core.hand_log import UNKNOWN_CARD

    msgs: list[str] = []
    if board is not None:
        given = {c for c in board if isinstance(c, str)}
        read = [c for c in captured.get("board") or [] if isinstance(c, str) and c and c != UNKNOWN_CARD]
        lost = [c for c in read if c not in given]
        if lost:
            msgs.append("RFID はボードに " + "・".join(lost) + " を読んでいますが、入れたボードにありません"
                        "（打ち間違いでないか確かめてください）")
    for p in captured.get("players") or []:
        if not isinstance(p, dict) or not isinstance(p.get("seat"), int) or holes is None or p["seat"] not in holes:
            continue
        given = {c for c in holes.get(p["seat"]) or [] if isinstance(c, str)}
        read = [c for c in p.get("hole_cards") or [] if isinstance(c, str) and c and c != UNKNOWN_CARD]
        lost = [c for c in read if c not in given]
        if lost and given:
            msgs.append(f"RFID は席{p['seat']} の手札に " + "・".join(lost) + " を読んでいますが、入れた手札にありません"
                        "（打ち間違いでないか確かめてください）")
    return msgs


_STREET_JA = {"preflop": "プリフロップ", "flop": "フロップ", "turn": "ターン", "river": "リバー",
              "showdown": "ショーダウン"}


def _review_confirmed(captured: dict, body: dict) -> bool:
    """要確認の行（と、ハンド全体が要確認ならハンド全体）をすべて ✓ で確かめたか（C-2 ガードの通り方）。"""
    rows = body.get("confirmed_rows")
    confirmed = {int(i) for i in rows if isinstance(i, int)} if isinstance(rows, list) else set()
    needed = {i for i, a in enumerate(captured.get("actions") or []) if isinstance(a, dict) and a.get("needs_review")}
    if not needed <= confirmed:
        return False
    return not captured.get("review_required") or body.get("confirmed_hand") is True


# ───────────────────────── HTTP ─────────────────────────

_ROUTE_SESSIONS = re.compile(r"^/api/sessions/?$")
_ROUTE_HANDS = re.compile(r"^/api/sessions/([^/]+)/hands/?$")
_ROUTE_HAND = re.compile(r"^/api/sessions/([^/]+)/hands/(\d+)/?$")
_ROUTE_LEGAL = re.compile(r"^/api/sessions/([^/]+)/hands/(\d+)/legal/?$")
_ROUTE_AUDIO = re.compile(r"^/api/sessions/([^/]+)/audio/([^/]+)$")
_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


class GroundTruthServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self, address: tuple[str, int], log_dir: Path,
        corrections: Optional[Path] = None, blind_every: int = BLIND_EVERY,
        corpus_factory: Optional[Callable[[Path], Any]] = None,
    ) -> None:
        self.log_dir = Path(log_dir)
        self.blind_every = blind_every
        self.gt_repo = GroundTruthRepository(self.log_dir)
        self.corr_repo: Optional[HandCorrectionRepository] = None
        if corrections is not None and Path(corrections).is_file():
            self.corr_repo = HandCorrectionRepository(corrections)
        # 読み上げ集（`tools/read_corpus.py`）。マイクとモデルを使うので、画面を開いたときに作る
        self._corpus: Any = None
        self._corpus_factory = corpus_factory
        self._corpus_lock = threading.Lock()
        # 台本のハンド（`tools/test_script.py`）
        from tools.test_script import ScriptApp

        self.script = ScriptApp(self.log_dir)
        super().__init__(address, _Handler)

    def corpus(self) -> Any:
        with self._corpus_lock:
            if self._corpus is None:
                if self._corpus_factory is not None:
                    self._corpus = self._corpus_factory(self.log_dir)
                else:
                    from tools.read_corpus import CorpusApp

                    self._corpus = CorpusApp(self.log_dir)
            return self._corpus

    def server_close(self) -> None:
        if self._corpus is not None:
            self._corpus.close()
        super().server_close()


class _Handler(BaseHTTPRequestHandler):
    server: GroundTruthServer
    server_version = "GroundTruthUI/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:      # noqa: A003 — 規約名
        logger.debug("%s - " + fmt, self.address_string(), *args)

    # ――― 応答 ―――

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_page(self, page: Optional[str] = None) -> None:
        body = (_PAGE if page is None else page).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> Any:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > _MAX_BODY:
            return None
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None

    def _send_audio(self, path: Path) -> None:
        """WAV を返す（iPad の Safari は Range で少しずつ取りに来るので 206 で応える）。"""
        data = path.read_bytes()
        size = len(data)
        start, end = 0, size - 1
        m = _RANGE_RE.match(self.headers.get("Range") or "")
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        body = data[start:end + 1]
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "max-age=3600")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def _session(raw: str) -> Optional[str]:
        sid = unquote(raw)
        return sid if _SESSION_RE.match(sid) else None

    # ――― ルーティング ―――

    def do_GET(self) -> None:          # noqa: N802 — BaseHTTPRequestHandler の規約
        self._guarded(self._get)

    def do_POST(self) -> None:         # noqa: N802
        self._guarded(self._post)

    def _guarded(self, handle: Any) -> None:
        """例外で応答を返さずに接続を切らない（画面には「Load failed」としか出ず、原因が分からなかった。
        店舗 2026-09-30 の台本の画面）。中身を 500 で返し、窓（ログ）にも残す。"""
        try:
            handle()
        except (BrokenPipeError, ConnectionResetError):
            pass                           # 画面側が先に閉じた
        except Exception as e:  # noqa: BLE001
            logger.exception("%s %s でエラー", self.command, self.path)
            try:
                self._send_json(500, {"code": "server_error", "message": f"サーバでエラー: {type(e).__name__}: {e}"})
            except Exception:  # noqa: BLE001 — 送れなければ諦める（ヘッダを送ったあとなど）
                pass

    def _get(self) -> None:
        path = urlsplit(self.path).path
        srv = self.server
        if path in ("/", "/index.html"):
            self._send_page()
            return
        if path in ("/corpus", "/corpus/"):
            from tools.read_corpus import CORPUS_PAGE

            self._send_page(CORPUS_PAGE)
            return
        if path in ("/script", "/script/"):
            from tools.test_script import SCRIPT_PAGE

            self._send_page(SCRIPT_PAGE)
            return
        if path.startswith("/api/script/"):
            self._send_json(*srv.script.route("GET", path))
            return
        if path.startswith("/api/corpus/"):
            status, payload = srv.corpus().route("GET", path)
            if isinstance(payload, Path):
                self._send_audio(payload)
            else:
                self._send_json(status, payload)
            return
        if path == "/favicon.ico":
            body = _FAVICON.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Cache-Control", "max-age=86400")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if _ROUTE_SESSIONS.match(path):
            self._send_json(200, {"sessions": list_sessions(srv.log_dir, srv.gt_repo)})
            return
        m = _ROUTE_HANDS.match(path)
        if m:
            sid = self._session(m.group(1))
            data = list_hands(srv.log_dir, sid, srv.gt_repo, srv.corr_repo) if sid else None
            if data is None:
                self._send_json(404, {"code": "not_found", "message": "セッションのログがありません"})
            else:
                self._send_json(200, data)
            return
        m = _ROUTE_HAND.match(path)
        if m:
            sid = self._session(m.group(1))
            data = (hand_detail(srv.log_dir, sid, int(m.group(2)), srv.gt_repo, srv.corr_repo, srv.blind_every)
                    if sid else None)
            if data is None:
                self._send_json(404, {"code": "not_found", "message": "ハンドがありません"})
            else:
                self._send_json(200, data)
            return
        m = _ROUTE_AUDIO.match(path)
        if m:
            sid = self._session(m.group(1))
            name = unquote(m.group(2))
            audio = srv.log_dir / "audio" / sid / name if sid and _AUDIO_RE.match(name) else None
            if audio is None or not audio.is_file():
                self._send_json(404, {"code": "not_found", "message": "音声がありません"})
            else:
                self._send_audio(audio)
            return
        self._send_json(404, {"code": "not_found", "message": path})

    def _post(self) -> None:
        path = urlsplit(self.path).path
        srv = self.server
        if path.startswith("/api/corpus/"):
            self._send_json(*srv.corpus().route("POST", path, self._read_json() or {}))
            return
        if path.startswith("/api/script/"):
            self._send_json(*srv.script.route("POST", path, self._read_json() or {}))
            return
        m = _ROUTE_LEGAL.match(path)
        if m:
            sid = self._session(m.group(1))
            captured = _get_hand(srv.log_dir, sid, int(m.group(2)), srv.corr_repo) if sid else None
            if captured is None:
                self._send_json(404, {"code": "not_found", "message": "ハンドがありません"})
                return
            body = self._read_json()
            actions = body.get("actions") if isinstance(body, dict) else None
            if not isinstance(actions, list):
                self._send_json(400, {"code": "invalid_amount", "message": "actions が要ります"})
                return
            board, holes = _gt_cards(body)
            result = replay_legal(captured, actions, board=board, holes=holes, button=_gt_button(body))
            result["lint"] = hand_lint(captured, result["actions"], result["next"], board, holes,
                                       show_record=body.get("blind") is not True)
            self._send_json(200, result)
            return
        if _ROUTE_HAND.match(path):
            self.do_PUT()
            return
        self._send_json(404, {"code": "not_found", "message": path})

    def do_PUT(self) -> None:          # noqa: N802
        path = urlsplit(self.path).path
        srv = self.server
        m = _ROUTE_HAND.match(path)
        if not m:
            self._send_json(404, {"code": "not_found", "message": path})
            return
        sid = self._session(m.group(1))
        if sid is None:
            self._send_json(404, {"code": "not_found", "message": "セッションのログがありません"})
            return
        body = self._read_json()
        if body is None:
            self._send_json(400, {"code": "invalid_amount", "message": "本体が JSON ではありません"})
            return
        status, payload = save_ground_truth(
            srv.log_dir, sid, int(m.group(2)), body, srv.gt_repo, srv.corr_repo,
        )
        self._send_json(status, payload)


def make_server(
    log_dir: Path, host: str = "127.0.0.1", port: int = 8791,
    corrections: Optional[Path] = None, blind_every: int = BLIND_EVERY,
    corpus_factory: Optional[Callable[[Path], Any]] = None,
) -> GroundTruthServer:
    return GroundTruthServer((host, port), log_dir, corrections, blind_every, corpus_factory)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="真のアクション入力（ground truth）の画面")
    ap.add_argument("--log-dir", default="./logs", help="ログディレクトリ（既定 ./logs）")
    ap.add_argument("--host", default="127.0.0.1", help="bind host（iPad から使うなら 0.0.0.0）")
    ap.add_argument("--port", type=int, default=8791, help="bind port（既定 8791）")
    ap.add_argument("--corrections", default="hand_corrections.json",
                    help="ハンド訂正の保存先（あれば訂正を重ねて表示・比較する, ADR-0036）")
    ap.add_argument("--blind-every", type=int, default=BLIND_EVERY,
                    help=f"N ハンドに 1 つ記録を見ずに先に入れる（既定 {BLIND_EVERY} = 全部。0 = しない）。"
                         "保存したあとで記録と照らし合わせて直す")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log_dir = Path(args.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    server = make_server(log_dir, args.host, args.port, Path(args.corrections), args.blind_every)
    print(f"[ground-truth] http://{args.host}:{args.port}/  (logs: {log_dir.resolve()})")
    print("[ground-truth] Ctrl+C で終了")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


# ───────────────────────── 画面 ─────────────────────────

_FAVICON = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    '<rect x="4" y="2" width="24" height="28" rx="4" fill="#e8eaed"/>'
    '<text x="16" y="22" font-size="16" font-weight="700" text-anchor="middle" fill="#1f6feb">✓</text></svg>'
)

_PAGE = r"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>真のアクション入力</title>
<style>
 :root { color-scheme: dark; }
 * { box-sizing: border-box; }
 body { margin:0; padding:12px 16px 40px; font:16px/1.5 system-ui,-apple-system,"Hiragino Sans",sans-serif;
        background:#12151a; color:#e8eaed; }
 h1 { font-size:18px; margin:0 0 8px; }
 h2 { font-size:15px; margin:18px 0 6px; color:#c9d1d9; }
 .muted { color:#9aa0a6; } .small { font-size:13px; }
 a { color:#58a6ff; }
 .top { display:flex; flex-wrap:wrap; justify-content:space-between; align-items:baseline; gap:8px; }
 .bar { display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin-bottom:10px; }
 .bar select, .bar input { max-width:100%; }
 select, input[type=text], input[type=number] { font:inherit; color:#e8eaed; background:#1e232b;
        border:1px solid #3a414d; border-radius:8px; padding:8px 10px; min-height:44px; }
 input[type=number] { width:110px; }
 button { font:inherit; color:#e8eaed; background:#2d333b; border:1px solid #3a414d; border-radius:8px;
        padding:8px 14px; min-height:44px; cursor:pointer; }
 button.primary { background:#1f6feb; border-color:#1f6feb; }
 button.ok { background:#238636; border-color:#238636; }
 button.danger { background:#6e3b3b; border-color:#6e3b3b; }
 button:disabled { opacity:.45; cursor:default; }
 button.sm { min-height:36px; padding:4px 10px; font-size:14px; }
 table { border-collapse:collapse; width:100%; }
 .tbl { overflow-x:auto; -webkit-overflow-scrolling:touch; max-width:100%; }
 td, th { padding:6px 8px; border-bottom:1px solid #2d333b; text-align:left; vertical-align:middle; }
 td select { padding:6px 4px; min-width:0; }
 td input[type=number] { width:88px; }
 @media (max-width: 480px) {
   body { padding:10px 12px 40px; font-size:15px; }
   .card { font-size:17px; min-width:40px; padding:5px 6px; }
   td, th { padding:5px 4px; }
 }
 th { color:#9aa0a6; font-weight:400; font-size:13px; }
 tr.row { cursor:pointer; } tr.row:active { background:#1a1f27; }
 .tag { display:inline-block; font-size:12px; padding:1px 8px; border-radius:999px; margin-right:4px; white-space:nowrap; }
 .t-ok { background:#1f3a24; color:#7ee787; } .t-edit { background:#1f2f3a; color:#58a6ff; }
 .t-warn { background:#3a2f1f; color:#e3b341; } .t-bad { background:#3a1f1f; color:#ff7b72; }
 .t-none { background:#23262b; color:#9aa0a6; }
 .card { display:inline-block; background:#1e232b; border:1px solid #3a414d; border-radius:8px; padding:6px 8px;
        font-size:20px; font-weight:700; min-width:48px; text-align:center; margin:2px; cursor:pointer; }
 .card.sm { font-size:15px; padding:2px 5px; min-width:34px; cursor:default; }
 .card.red { color:#ff7b72; }
 .card.empty { color:#9aa0a6; border-style:dashed; font-weight:400; }
 .cols { display:grid; grid-template-columns: 1fr 1.3fr; gap:18px; }
 .cols > * { min-width:0; }
 @media (max-width: 900px) { .cols { grid-template-columns: 1fr; } }
 .cap td, .cap th { white-space:nowrap; }
 .cap td.raw { white-space:normal; min-width:140px; }
 .cap td.raw .tag { white-space:normal; }
 .panel { background:#161a20; border:1px solid #2d333b; border-radius:12px; padding:12px 14px; }
 .seatrow { display:flex; flex-wrap:wrap; align-items:center; gap:8px; padding:6px 0; border-bottom:1px solid #2d333b; }
 .seatrow .no { min-width:120px; }
 .chips { display:flex; flex-wrap:wrap; gap:6px; }
 .chip { border-radius:999px; padding:6px 14px; }
 .chip.on { background:#1f6feb; border-color:#1f6feb; }
 tr.err td { background:#3a1f1f; }
 tr.errmsg td { color:#ff7b72; font-size:13px; }
 tr.warn td { background:#2a2416; }
 tr.diffrow td { background:#2b2a12; }
 .t-diff { background:#3a3514; color:#e3d341; }
 .lint { background:#2a2416; border:1px solid #6b5a2e; border-radius:12px; padding:10px 12px; margin-top:10px; color:#e3b341; font-size:14px; }
 .reconcile { background:#1f2a3a; border:1px solid #2d4a6e; border-radius:12px; padding:10px 12px; margin:0 0 10px; }
 td.street { color:#9aa0a6; font-size:13px; white-space:nowrap; }
 td.tools { white-space:nowrap; }
 .quick { background:#1a2230; border:1px solid #2d3a4d; border-radius:12px; padding:12px 14px; margin-top:10px; }
 .quick .who { font-weight:700; margin-bottom:8px; }
 .quick .btns { display:flex; flex-wrap:wrap; gap:8px; align-items:center; }
 .actions-bottom { display:flex; flex-wrap:wrap; gap:10px; margin-top:18px; align-items:center; }
 .rowadd { display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin-top:8px; }
 .hidden { display:none !important; }
 #modal { position:fixed; inset:0; background:rgba(0,0,0,.6); display:flex; align-items:flex-end; justify-content:center; z-index:10; }
 .sheet { background:#161a20; border:1px solid #3a414d; border-radius:16px 16px 0 0; padding:12px 14px 24px; width:100%; max-width:720px; }
 .sheet-h { display:flex; gap:8px; align-items:center; margin-bottom:8px; }
 .sheet-h span { flex:1; font-weight:700; }
 .suitrow { display:grid; grid-template-columns: repeat(13, 1fr); gap:4px; margin:4px 0; }
 .suitrow.red button { color:#ff7b72; }
 button.pick { padding:0; min-height:44px; min-width:0; font-weight:700; font-size:14px; }
 button.pick.used { opacity:.3; } button.pick.cur { background:#1f6feb; border-color:#1f6feb; }
 #toast { position:fixed; left:50%; bottom:24px; transform:translateX(-50%); background:#238636; color:#fff;
        padding:10px 18px; border-radius:999px; z-index:20; max-width:90vw; }
 #toast.bad { background:#b62324; }
 .summary { display:flex; flex-wrap:wrap; gap:14px; color:#c9d1d9; font-size:14px; margin:6px 0 12px; }
 .summary b { font-size:18px; }
 .cap td { font-size:14px; }
 .err { color:#ff7b72; }
 .tl { max-height:52vh; overflow-y:auto; -webkit-overflow-scrolling:touch; margin-top:6px; }
 .tl .item { display:flex; gap:8px; align-items:center; padding:4px 0; border-bottom:1px solid #23272e; font-size:14px; }
 .tl .t { color:#9aa0a6; min-width:56px; font-variant-numeric:tabular-nums; font-size:13px; }
 .tl .noise { opacity:.5; }
 .tl .sys { color:#9aa0a6; }
 .tl .gap { min-width:44px; }
 button.play { min-height:34px; min-width:44px; padding:2px 8px; }
 button.chk { min-height:34px; min-width:44px; padding:2px 8px; }
 button.chk.on { background:#238636; border-color:#238636; }
 button.unsure.on { background:#9e6a03; border-color:#9e6a03; }
 .blind { background:#241d33; border:1px solid #4d3a6e; border-radius:12px; padding:10px 12px; margin:6px 0; }
</style></head>
<body>
<div id="app">読み込み中…</div>
<div id="modal" class="hidden"></div>
<div id="toast" class="hidden"></div>
<script>
const RANKS = ["A","K","Q","J","T","9","8","7","6","5","4","3","2"];
const SUITS = ["s","h","d","c"];
const SUIT_SYM = {s:"♠", h:"♥", d:"♦", c:"♣"};
const GT_ACTIONS = ["fold","check","call","bet","raise","allin"];
const ACTION_JA = {fold:"フォールド", check:"チェック", call:"コール", bet:"ベット", raise:"レイズ", allin:"オールイン"};
const STREET_JA = {preflop:"プリフロップ", flop:"フロップ", turn:"ターン", river:"リバー", showdown:"ショーダウン"};
const BOARD_LABEL = ["フロップ","フロップ","フロップ","ターン","リバー"];
// follow: いちばん新しいセッションを見ている（新しいセッションが始まったらそちらに移る）。ロールダウンで前のセッションを
// 選んだら移らない。
const S = {sessions:[], sid:null, follow:true, hands:null, summary:null, view:"list", hand:null, gt:null, legal:null,
           dirty:false, annotator:"", picker:null, busy:false,
           openedAt:0, confirmed:new Set(), confirmedHand:false, revealed:false, audio:null};
const $ = (id) => document.getElementById(id);
function esc(s){ return String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }
// 文字の欄・ロールダウンを触っている間は、裏で届いた結果（一覧の 5 秒ごとの読み直し・手番の確認）で画面を作り直さない。
// 作り直すとキーボードやロールダウンが閉じ、打ちかけの文字も消える（店舗 2026-09-30: セッションを選ぶロールダウンが
// 閉じる・キーボードが消える）。離れたら作り直す
function editing(){
  const a = document.activeElement, app = $("app");
  if (!a || !app || !app.contains(a)) return false;
  if (a.tagName === "SELECT" || a.tagName === "TEXTAREA") return true;
  return a.tagName === "INPUT" && !["checkbox", "radio", "button", "submit", "range"].includes((a.type || "").toLowerCase());
}
let pendingRender = null;
function whenFree(fn){ if (editing()) pendingRender = fn; else { pendingRender = null; fn(); } }
document.addEventListener("focusout", () => setTimeout(() => {
  if (pendingRender && !editing()) { const fn = pendingRender; pendingRender = null; fn(); }
}, 0));
function fmtTime(iso){ const m = String(iso||"").match(/T(\d\d:\d\d:\d\d)/); return m ? m[1] : (iso || "—"); }
function pct(a, b){ return b ? Math.round(1000 * a / b) / 10 + "%" : "—"; }
function cardHtml(c, cls, onclick){
  const cl = cls || "";
  if (!c) return `<span class="card empty ${cl}" ${onclick?`onclick="${onclick}"`:""}>＋</span>`;
  if (c === "??") return `<span class="card ${cl}" title="読めなかった札">?</span>`;
  const red = c[1] === "h" || c[1] === "d";
  const rank = c[0] === "T" ? "10" : c[0];
  return `<span class="card ${red?"red":""} ${cl}" ${onclick?`onclick="${onclick}"`:""}>${rank}${SUIT_SYM[c[1]]||c[1]}</span>`;
}
const OFFLINE = "サーバにつながりません。PC の「真のアクション入力 (iPad から)」の黒い窓が開いているか確かめてください（閉じていたら起動し直して、この画面を再読み込み）";
async function api(path, opts){
  let r;
  try { r = await fetch(path, Object.assign({headers:{"Content-Type":"application/json"}}, opts || {})); }
  catch (e) { throw new Error(OFFLINE); }
  let d = null; try { d = await r.json(); } catch (e) {}
  if (!r.ok) throw new Error((d && d.message) || ("HTTP " + r.status));
  return d;
}
function toast(msg, bad){
  const t = $("toast"); t.textContent = msg; t.className = bad ? "bad" : "";
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.add("hidden"), bad ? 6000 : 3000);
}
function sidPath(){ return "/api/sessions/" + encodeURIComponent(S.sid); }

// ――― 一覧 ―――
async function loadSessions(){
  const d = await api("/api/sessions");
  S.sessions = d.sessions || [];
  const newest = S.sessions.length ? S.sessions[0].session_id : null;
  if ((S.follow || !S.sessions.some(s => s.session_id === S.sid)) && S.sid !== newest) {
    S.sid = newest; S.hands = null; S.summary = null;
  }
}
async function loadHands(background){
  const show = () => { if (S.view === "list") renderList(); };
  if (!S.sid) { S.hands = []; S.summary = null; background ? whenFree(show) : show(); return; }
  try {
    const d = await api(sidPath() + "/hands");
    S.hands = d.hands || []; S.summary = d.summary || null; S.script = !!d.script;
  } catch (e) { S.hands = []; S.summary = null; }
  background ? whenFree(show) : show();
}
async function selectSession(sid){
  S.sid = sid; S.follow = S.sessions.length > 0 && sid === S.sessions[0].session_id;
  S.hands = null; renderList(); await loadHands();
}
async function refreshAll(){ try { await loadSessions(); await loadHands(); } catch (e) { toast("読み込めません: " + e.message, true); } }
function setAnnotator(v){ S.annotator = v.trim(); try { localStorage.setItem("gt_annotator", S.annotator); } catch (e) {} }
function renderList(){
  const sess = S.sessions.map(s => `<option value="${esc(s.session_id)}" ${s.session_id===S.sid?"selected":""}>${esc(s.updated_at||"")}  ${esc(s.session_id)}（${s.hands} ハンド / 入力 ${s.annotated}）</option>`).join("");
  const sm = S.summary;
  const summary = sm ? `<div class="summary">
      <div>入力 <b>${sm.annotated}</b> / ${sm.hands} ハンド</div>
      <div>要確認 <b>${sm.review}</b></div>
      <div>ハンド一致 <b>${pct(sm.hands_match, sm.annotated)}</b></div>
      <div>アクション一致 <b>${pct(sm.action_correct, sm.action_total)}</b> <span class="muted small">(${sm.action_correct}/${sm.action_total})</span></div>
      <div>ボード一致 <b>${pct(sm.board_match, sm.annotated)}</b></div>
      <div>勝者一致 <b>${pct(sm.winner_match, sm.winner_total)}</b></div>
      ${sm.entry_sec_avg != null ? `<div>入力の時間 平均 <b>${Math.round(sm.entry_sec_avg)}</b> 秒</div>` : ""}
      ${sm.blind ? `<div>ブラインド <b>${sm.blind}</b></div>` : ""}</div>` : "";
  const next = (S.hands||[]).filter(h => !h.ground_truth).sort((a,b) => a.hand_id - b.hand_id)[0];
  const rows = (S.hands||[]).map(h => {
    const st = h.ground_truth
      ? (h.ground_truth.source === "captured-passthrough" ? '<span class="tag t-ok">記録どおり</span>' : '<span class="tag t-edit">修正済</span>')
      : '<span class="tag t-none">未入力</span>';
    const rv = h.has_needs_review ? '<span class="tag t-warn">要確認</span>' : "";
    const acc = h.accuracy ? (h.accuracy.all_match ? '<span class="tag t-ok">一致</span>' : `<span class="tag t-bad">差分 ${h.accuracy.mismatches}</span>`) : "";
    const bl = h.ground_truth && h.ground_truth.blind
      ? (h.ground_truth.reconciled ? '<span class="tag t-edit">ブラインド→照合済</span>' : '<span class="tag t-warn">照らし合わせ待ち</span>') : "";
    return `<tr class="row" onclick="openHand(${h.hand_id})"><td>#${h.hand_id}</td><td>${fmtTime(h.started_at)}</td>
      <td>${(h.seats||[]).join(" ")}</td><td>${(h.board||[]).map(c => cardHtml(c, "sm")).join("")}</td>
      <td>${h.winner_seat != null ? "席 " + h.winner_seat : "—"}</td><td>${rv} ${st} ${bl} ${acc}</td></tr>`;
  }).join("");
  $("app").innerHTML = `<div class="top"><h1>真のアクション入力</h1><span class="small"><a href="/script">台本 →</a>　<a href="/corpus">読み上げ集 →</a></span></div>
    <div class="bar">
      <select onchange="selectSession(this.value)">${sess || "<option>セッションがありません</option>"}</select>
      <button class="sm" onclick="refreshAll()">↻ 読み直す</button>
      <label class="small muted">入力者 <input type="text" value="${esc(S.annotator)}" onchange="setAnnotator(this.value)" style="width:120px"></label>
      ${next ? `<button class="primary sm" onclick="openHand(${next.hand_id})">次の未入力 #${next.hand_id} →</button>` : ""}
    </div>
    ${S.script ? `<div class="reconcile">このセッションは<b>台本のハンド</b>です。正解は台本なので入力は要りません（<a href="/script">台本の画面</a>）。</div>` : ""}
    ${summary}
    ${S.hands === null ? "<p class='muted'>読み込み中…</p>" :
      (rows ? `<div class="tbl"><table><tr><th>#</th><th>時刻</th><th>席</th><th>ボード</th><th>勝者</th><th>状態</th></tr>${rows}</table></div>`
            : "<p class='muted'>このセッションにはまだハンドがありません。ハンドが終わると 5 秒以内にここに出ます。</p>")}`;
}

// ――― 編集 ―――
function normCards(arr, n){ const out = []; for (let i = 0; i < n; i++) out.push((arr && arr[i] && arr[i] !== "??") ? arr[i] : null); return out; }
function buildGt(d){
  const cap = d.captured, g = d.ground_truth, src = g || cap;
  const players = (cap.players || []).filter(p => typeof p.seat === "number").map(p => {
    const gp = (g && (g.players || []).find(x => x.seat === p.seat)) || p;
    return {seat:p.seat, name:p.name || "", hole_cards: normCards(gp.hole_cards, 2), showed_down: !!gp.showed_down};
  });
  const blind = !!d.blind && !g;              // 記録を見ずに入れる（アクションと勝者は空から）
  const actions = blind ? [] : (src.actions || []).filter(a => GT_ACTIONS.includes(a.action) && typeof a.seat === "number")
                  .map(a => ({seat:a.seat, action:a.action, amount:a.amount || 0, unsure: !!a.unsure}));
  // ボタン: 入れた真のアクションで選んだ席（ディーラーが動かし忘れたハンド）か、記録のボタン
  const button = (g && g.button_seat != null) ? g.button_seat : (cap.button_seat ?? null);
  return {board: normCards(src.board, 5), players, actions, button_seat: button,
          winner_seat: blind ? null : (src.winner_seat === undefined ? null : src.winner_seat), notes: (g && g.notes) || ""};
}
async function openHand(hid){
  try {
    const d = await api(sidPath() + "/hands/" + hid);
    S.hand = d; S.gt = buildGt(d); S.legal = d.legal; S.dirty = false; S.view = "edit";
    S.openedAt = Date.now(); S.confirmed = new Set(); S.confirmedHand = false; S.revealed = false;
    applyLegal();
    renderEdit();
    window.scrollTo(0, 0);
  } catch (e) { toast("ハンドを開けません: " + e.message, true); }
}
function backToList(){
  if (S.dirty && !confirm("保存していない変更があります。一覧に戻りますか？")) return;
  S.view = "list"; S.hand = null; renderList(); loadHands();
}
function applyLegal(){
  // 反映できた行は pokerkit の正規形（チェック / コールの別・コールの額）に揃える
  const L = S.legal; if (!L || !L.actions) return;
  L.actions.forEach((la, i) => { const a = S.gt.actions[i]; if (a) { a.action = la.action; a.amount = la.amount; a.street = la.street; } });
  const n = L.next;
  if (n && n.hand_over && n.foldout_winner != null) S.gt.winner_seat = n.foldout_winner;
  // ショーダウン: 入れたボードと手札で勝者が決まるなら入れる（未定のときだけ。選び直した席は変えない）
  else if (n && n.hand_over && n.showdown_winner != null && S.gt.winner_seat == null) S.gt.winner_seat = n.showdown_winner;
}
let legalTimer = null;
function refreshLegal(){
  clearTimeout(legalTimer);
  legalTimer = setTimeout(async () => {
    try {
      const body = {actions: S.gt.actions.map(a => ({seat:a.seat, action:a.action, amount:a.amount})),
                    button_seat: S.gt.button_seat,
                    board: S.gt.board.map(c => c || "??"),
                    players: S.gt.players.map(p => ({seat: p.seat, hole_cards: p.hole_cards.filter(Boolean)})),
                    blind: isBlind()};
      S.legal = await api(sidPath() + "/hands/" + S.hand.captured.hand_id + "/legal", {method:"POST", body: JSON.stringify(body)});
      applyLegal();
    } catch (e) { toast("手番の確認に失敗: " + e.message, true); }
    whenFree(() => { if (S.view === "edit") renderEdit(); });
  }, 60);
}
function touch(){ S.dirty = true; refreshLegal(); }
function setRowSeat(i, v){ S.gt.actions[i].seat = parseInt(v, 10); touch(); }
function setRowAction(i, v){ const a = S.gt.actions[i]; a.action = v; if (!["bet","raise","allin"].includes(v)) a.amount = 0; touch(); }
function setRowAmount(i, v){ S.gt.actions[i].amount = parseInt(v, 10) || 0; touch(); }
function setRowStreet(i, v){ S.gt.actions[i].street = v; S.dirty = true; renderEdit(); }
function delRow(i){ S.gt.actions.splice(i, 1); touch(); }
function insRow(i){ const a = S.gt.actions[i]; S.gt.actions.splice(i, 0, {seat: a ? a.seat : S.gt.players[0].seat, action:"check", amount:0}); touch(); }
function addRow(){
  // 最後に 1 行足す（次の手番が分かればその席、分からなければ最後の行の次の席）
  const g = S.gt, L = S.legal || {}, n = L.next;
  const seats = (g.players || []).map(p => p.seat);
  let seat = seats.length ? seats[0] : 1;
  if (n && n.actor_seat != null && !L.error) seat = n.actor_seat;
  else if (g.actions.length && seats.length) {
    const i = seats.indexOf(g.actions[g.actions.length - 1].seat);
    seat = seats[(i + 1) % seats.length];
  }
  g.actions.push({seat, action: "check", amount: 0});
  touch();
}
function addQuick(act){
  const n = S.legal && S.legal.next; if (!n || n.actor_seat == null) return;
  let amount = 0;
  if (act === "bet" || act === "raise") {
    amount = parseInt(($("qamt") || {}).value, 10) || 0;
    if (!amount) { toast("ベット / レイズの額（トータル）を入れてください", true); return; }
  }
  S.gt.actions.push({seat: n.actor_seat, action: act, amount});
  // 額の欄から追加したときは欄を離れて、追加した行をすぐ見せる（入力中は作り直さないので）
  if (document.activeElement && document.activeElement.id === "qamt") document.activeElement.blur();
  touch();
}
function addMuck(seat){
  // ショーダウンで手札を見せずに降りた = street=showdown のフォールド（ライブの記録と同じ形, ADR-0062）
  S.gt.actions.push({seat, action: "fold", amount: 0});
  touch();
}
function isBlind(){ return !!(S.hand && S.hand.blind && !S.revealed); }
function reveal(){
  if (!confirm("記録を見ると、このハンドはブラインドではなくなります。見ますか？")) return;
  S.revealed = true; renderEdit();
}
function toggleConfirm(i){ if (S.confirmed.has(i)) S.confirmed.delete(i); else S.confirmed.add(i); renderEdit(); }
function toggleConfirmHand(){ S.confirmedHand = !S.confirmedHand; renderEdit(); }
function reviewItems(){
  const cap = S.hand.captured;
  const rows = (cap.actions || []).map((a, i) => a.needs_review ? i : -1).filter(i => i >= 0);
  return {rows, hand: !!cap.review_required};
}
function allConfirmed(){
  const r = reviewItems();
  return r.rows.every(i => S.confirmed.has(i)) && (!r.hand || S.confirmedHand);
}
// 記録と同じ行か（ストリート・席・アクション・ベット / レイズの額が同じ行を順に対応させる）。違う行を黄色で見せる
function rowKey(a){ return [a.street || "", a.seat, a.action, ["bet","raise"].includes(a.action) ? (a.amount || 0) : ""].join("|"); }
function matchRows(xs, ys){
  const n = xs.length, m = ys.length;
  const dp = Array.from({length: n + 1}, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--)
    dp[i][j] = xs[i] === ys[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
  const a = new Set(), b = new Set();
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (xs[i] === ys[j]) { a.add(i); b.add(j); i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) i++; else j++;
  }
  return [a, b];
}
function toggleUnsure(i){ const a = S.gt.actions[i]; a.unsure = !a.unsure; S.dirty = true; renderEdit(); }
function playAudio(name){
  try { if (S.audio) S.audio.pause(); } catch (e) {}
  S.audio = new Audio(sidPath() + "/audio/" + encodeURIComponent(name));
  S.audio.play().catch(e => toast("再生できません: " + e.message, true));
}
function renderTimeline(items, blind){
  if (!items || !items.length) return "<p class='muted small'>このハンドの間の発話・札の記録はありません。</p>";
  return `<div class="tl">${items.map(it => {
    const t = (it.t >= 0 ? "+" : "") + Number(it.t).toFixed(1) + "s";
    if (it.kind === "speech") {
      const play = it.audio ? `<button class="play sm" onclick="playAudio('${esc(it.audio)}')" title="音声を聞く">▶</button>` : '<span class="gap"></span>';
      const conf = it.confidence != null ? ` <span class="muted small">${Number(it.confidence).toFixed(2)}</span>` : "";
      const parsed = blind ? "" : ` <span class="muted small">→ ${it.parsed && it.parsed.length ? esc(it.parsed.join("、")) : (it.noise ? "雑音" : "（読まない）")}</span>`;
      return `<div class="item ${it.noise ? "noise" : ""}"><span class="t">${t}</span>${play}<span>「${esc(it.text)}」${conf}${parsed}</span></div>`;
    }
    return `<div class="item sys"><span class="t">${t}</span><span class="gap"></span><span>${esc(it.text)}</span></div>`;
  }).join("")}</div>`;
}
// 選んである席をもう一度押しても外さない（オーナー 2026-09-29: 確かめるつもりで押すと未定になっていた）。未定は専用のボタン
function setWinner(seat){ S.gt.winner_seat = seat; S.dirty = true; renderEdit(); }
// ボタンの席（ディーラーがボタンを動かし忘れたハンドは実際の席に。手番の順が変わる, 2026-09-29）
function setButton(seat){ S.gt.button_seat = seat; touch(); }
function setShowed(seat){ const p = S.gt.players.find(x => x.seat === seat); p.showed_down = !p.showed_down; S.dirty = true; renderEdit(); }
function setNotes(v){ S.gt.notes = v; S.dirty = true; }
function resetToCaptured(){
  if (!confirm("入力した内容を捨てて、記録の内容に戻しますか？")) return;
  S.gt = buildGt({captured: S.hand.captured, ground_truth: null}); touch();
}

function renderEdit(){
  const d = S.hand, cap = d.captured, g = S.gt, L = S.legal || {};
  const seats = g.players.map(p => p.seat);
  const la = L.actions || [], err = L.error, n = L.next;
  const replayOk = !!n;
  const hidden = isBlind();
  const capActions = (cap.actions || []).filter(a => GT_ACTIONS.includes(a.action));
  const [gtSame, capSame] = hidden ? [new Set(), new Set()]
    : matchRows(g.actions.map((a, i) => rowKey(Object.assign({}, a, la[i] || {}))), capActions.map(rowKey));
  const capSameRows = new Set(capActions.map((a, j) => capSame.has(j) ? a : null).filter(Boolean));
  const rows = g.actions.map((a, i) => {
    const l = la[i];
    const isErr = err && err.index === i;
    const differs = !hidden && !isErr && !gtSame.has(i);
    const street = l ? (STREET_JA[l.street] || l.street) : (isErr ? "" : (replayOk ? "?" : ""));
    const streetCell = replayOk ? `<td class="street">${street}</td>`
      : `<td class="street"><select onchange="setRowStreet(${i}, this.value)">${["preflop","flop","turn","river"].map(s => `<option value="${s}" ${a.street===s?"selected":""}>${STREET_JA[s]}</option>`).join("")}</select></td>`;
    const needAmt = ["bet","raise","allin"].includes(a.action);
    const amtCell = needAmt
      ? `<input type="number" inputmode="numeric" value="${(a.action === "allin" && l && l.total) || a.amount || ""}" onchange="setRowAmount(${i}, this.value)" ${a.action==="allin"?"disabled":""}>`
      : (a.action === "call" ? `<span class="muted">${(l && l.total) || a.amount || ""}</span>` : "");
    return `<tr class="${isErr?"err":(differs?"diffrow":"")}">${streetCell}
      <td><select onchange="setRowSeat(${i}, this.value)">${seats.map(s => `<option value="${s}" ${s===a.seat?"selected":""}>席 ${s}</option>`).join("")}</select></td>
      <td><select onchange="setRowAction(${i}, this.value)">${GT_ACTIONS.map(x => `<option value="${x}" ${x===a.action?"selected":""}>${ACTION_JA[x]}</option>`).join("")}</select></td>
      <td>${amtCell}</td>
      <td class="tools">${differs ? '<span class="tag t-diff">記録と違う</span>' : ""}<button class="sm unsure ${a.unsure?"on":""}" onclick="toggleUnsure(${i})" title="自信なし">?</button> <button class="sm" onclick="insRow(${i})" title="この前に挿入">＋</button> <button class="sm danger" onclick="delRow(${i})">✕</button></td></tr>
      ${isErr ? `<tr class="errmsg"><td colspan="5">⚠ ${esc(err.message)}</td></tr>` : ""}`;
  }).join("");
  let quick = "";
  if (!replayOk) {
    quick = `<div class="quick"><div class="who err">手番の自動補完は使えません</div><div class="small muted">${esc((err && err.message) || "")}。ストリートは各行で選んでください。</div>
      </div>`;
  } else if (err) {
    quick = `<div class="quick"><div class="who err">赤い行を直してください</div><div class="small muted">その先の手番はまだ決められません。</div></div>`;
  } else if (n.hand_over) {
    const showdown = n.foldout_winner == null;
    const who = showdown
      ? `ショーダウン: 席 ${(n.active_seats||[]).join("・")} — 勝った席を下で選んでください`
      : `席 ${n.foldout_winner} の勝ち（ほかは全員フォールド）`;
    const mucks = showdown
      ? `<div class="btns">${(n.active_seats||[]).map(s => `<button onclick="addMuck(${s})">席 ${s} が見せずに降りた</button>`).join("")}</div>`
      : "";
    quick = `<div class="quick"><div class="who">ベッティング終了 — ${who}</div>${mucks}<div class="small muted">ポット ${n.pot}。手札を見せずに降りた人がいれば、その席の「見せずに降りた」（ショーダウンのフォールド）。行が足りなければ「＋ 行を追加」、多ければ ✕ で直せます。</div></div>`;
  } else {
    const legal = n.legal_actions || [];
    const cc = n.amount_to_call > 0 ? `コール ${n.call_total || n.amount_to_call}` : "チェック";
    const br = legal.includes("raise") ? "レイズ" : "ベット";
    quick = `<div class="quick"><div class="who">次: 席 ${n.actor_seat} の番 <span class="muted small">${STREET_JA[n.street]||n.street} ／ ポット ${n.pot}</span></div>
      <div class="btns">
        <button onclick="addQuick('fold')">フォールド</button>
        <button onclick="addQuick('check')">${cc}</button>
        ${legal.includes("bet") || legal.includes("raise") ? `<input id="qamt" type="number" inputmode="numeric" placeholder="${n.min_raise}〜${n.max_raise}" onkeydown="if(event.key==='Enter'){addQuick('${legal.includes("raise")?"raise":"bet"}')}"><button class="primary" onclick="addQuick('${legal.includes("raise")?"raise":"bet"}')">${br}</button>` : ""}
        ${legal.includes("allin") ? `<button onclick="addQuick('allin')">オールイン ${n.max_raise}</button>` : ""}
      </div></div>`;
  }
  const boardHtml = g.board.map((c, i) => `<div style="text-align:center">${cardHtml(c, "", `openPicker('board',${i})`)}<div class="small muted">${BOARD_LABEL[i]}</div></div>`).join("");
  const playersHtml = g.players.map(p => `<div class="seatrow"><div class="no">席 ${p.seat} <span class="muted small">${esc(p.name)}</span></div>
      <div>${cardHtml(p.hole_cards[0], "", `openPicker('hole',${p.seat},0)`)}${cardHtml(p.hole_cards[1], "", `openPicker('hole',${p.seat},1)`)}</div>
      <button class="sm chip ${p.showed_down?"on":""}" onclick="setShowed(${p.seat})">見せた</button></div>`).join("");
  const buttonHtml = `<div class="chips">${seats.map(s => `<button class="chip ${g.button_seat===s?"on":""}" onclick="setButton(${s})">席 ${s}</button>`).join("")}
      <span class="muted small" style="align-self:center">${g.button_seat !== cap.button_seat ? `記録は席 ${cap.button_seat ?? "—"}（ディーラーがボタンを動かし忘れた）` : "記録どおり。ディーラーがボタンを動かし忘れたときは実際の席を選ぶ（手番の順が変わります）"}</span></div>`;
  const sdw = L.next && L.next.hand_over && L.next.foldout_winner == null ? L.next.showdown_winner : null;
  const winnerHtml = `<div class="chips">${seats.map(s => `<button class="chip ${g.winner_seat===s?"on":""}" onclick="setWinner(${s})">席 ${s}</button>`).join("")}
      <button class="chip ${g.winner_seat==null?"on":""}" onclick="setWinner(null)">未定</button>
      ${sdw != null ? `<span class="muted small" style="align-self:center">手札で判定: 席 ${sdw}</span>` : ""}</div>`;
  const capRows = (cap.actions || []).map((a, i) => `<tr class="${GT_ACTIONS.includes(a.action) && !capSameRows.has(a) ? "diffrow" : (a.needs_review?"warn":"")}">
      <td>${a.needs_review ? `<button class="chk ${S.confirmed.has(i)?"on":""}" onclick="toggleConfirm(${i})" title="確かめた">✓</button>` : ""}</td>
      <td class="street">${STREET_JA[a.street]||a.street||""}</td><td>席 ${a.seat}</td>
      <td>${ACTION_JA[a.action]||a.action}</td><td>${(["call","allin"].includes(a.action) && a.total) || a.amount || ""}</td>
      <td class="muted small raw">${esc(a.raw_text||"")}${a.needs_review?` <span class="tag t-warn">要確認${a.reason?": "+esc(a.reason):""}</span>`:""}</td></tr>`).join("");
  const capPlayers = (cap.players || []).map(p => `席 ${p.seat}: ${(p.hole_cards||[]).map(c => cardHtml(c, "sm")).join("") || "<span class='muted'>—</span>"}`).join(" ｜ ");
  const lint = (L.lint || []).map(m => `<div class="lint">⚠ ${esc(m)}</div>`).join("");
  const gtd = d.ground_truth;
  const reconcile = gtd && gtd.blind && !gtd.reconciled
    ? `<div class="reconcile">ブラインドで入れた内容を保存しました。<b>記録と違う行（黄色）</b>を左の ▶ の音声で確かめ、正しい方に直して「保存」してください（直すところが無ければ、そのまま「保存」）。</div>`
    : "";
  const gtMeta = d.ground_truth ? `<span class="tag ${d.ground_truth.source==="captured-passthrough"?"t-ok":"t-edit"}">${d.ground_truth.source==="captured-passthrough"?"記録どおり":"修正済"} ${esc(d.ground_truth.annotated_at||"")} ${esc(d.ground_truth.annotator||"")}</span>` : '<span class="tag t-none">未入力</span>';
  const blind = isBlind();
  const canPass = !blind && (!d.has_needs_review || allConfirmed());
  const ri = reviewItems();
  const leftPanel = blind ? `
      <div class="panel cap">
        <h2 style="margin-top:0">ブラインド</h2>
        <div class="blind">まず<b>記録を見ずに</b>入れて「保存」してください。保存したあとで記録と照らし合わせ、違う行を
          音声で確かめて直します（記録に引きずられずに正解を作るため）。下の発話は ▶ で聞けます。
          <button class="sm" onclick="reveal()">記録を見る</button></div>
        <h2>発話と札の流れ <span class="muted small">+秒 = 手札が配られてから</span></h2>
        ${renderTimeline(d.timeline, true)}
      </div>` : `
      <div class="panel cap">
        <h2 style="margin-top:0">記録（システムが取ったもの）</h2>
        <div>ボード: ${(cap.board||[]).map(c => cardHtml(c, "sm")).join("") || "<span class='muted'>—</span>"}</div>
        <div class="small" style="margin:4px 0">${capPlayers}</div>
        <div class="small">勝者: ${cap.winner_seat != null ? "席 " + cap.winner_seat : "—"} ${cap.winner_source ? `<span class="muted">(${esc(cap.winner_source)})</span>` : ""}
          ${cap.announced_hand ? `／ 役名 ${esc(cap.announced_hand)}` : ""} ／ ポット ${cap.pot_total ?? "—"}
          ${d.has_needs_review ? '<span class="tag t-warn">要確認</span>' : ""}</div>
        <div class="tbl" style="margin-top:8px"><table><tr><th>確認</th><th>ストリート</th><th>席</th><th>アクション</th><th>額</th><th>聞き取り</th></tr>${capRows || "<tr><td colspan='6' class='muted'>アクションなし</td></tr>"}</table></div>
        ${ri.hand ? `<div class="small" style="margin-top:6px">ハンド全体（勝者・ボード・手札）も確かめた <button class="chk ${S.confirmedHand?"on":""}" onclick="toggleConfirmHand()">✓</button></div>` : ""}
        <h2>発話と札の流れ <span class="muted small">+秒 = 手札が配られてから ／ ▶ で聞く</span></h2>
        ${renderTimeline(d.timeline, false)}
      </div>`;
  $("app").innerHTML = `
    <div class="bar"><button onclick="backToList()">← 一覧</button><h1 style="margin:0">ハンド #${cap.hand_id}</h1>
      <span class="muted small">${fmtTime(cap.started_at)} ／ ボタン 席 ${cap.button_seat ?? "—"} ／ ブラインド ${(cap.blinds||{}).sb ?? "?"}/${(cap.blinds||{}).bb ?? "?"}</span> ${gtMeta}</div>
    <div class="cols">
      ${leftPanel}
      <div class="panel">
        ${reconcile}
        <h2 style="margin-top:0">実際（真のアクション）</h2>
        <h2>ボード</h2><div style="display:flex;gap:6px;flex-wrap:wrap">${boardHtml}</div>
        <h2>手札（分かる席だけ）</h2>${playersHtml}
        <h2>ボタン</h2>${buttonHtml}
        <h2>アクション <span class="muted small">コールの額とストリートは自動。ベット / レイズはトータルの額</span></h2>
        <div class="tbl"><table><tr><th>ストリート</th><th>席</th><th>アクション</th><th>額</th><th></th></tr>${rows || "<tr><td colspan='5' class='muted'>まだありません（下のボタンで足す）</td></tr>"}</table></div>
        <div class="rowadd"><button class="sm" onclick="addRow()">＋ 行を追加</button>
          <span class="muted small">最後に 1 行足します（席・アクション・額はあとで変えられます。行の ＋ はその行の前に入れます）</span></div>
        ${quick}${lint}
        <h2>勝った席</h2>${winnerHtml}
        <h2>メモ</h2><input type="text" style="width:100%" value="${esc(g.notes)}" placeholder="気づいたこと（任意）" oninput="setNotes(this.value)" onchange="setNotes(this.value)">
        <div class="actions-bottom">
          ${blind ? "" : `<button class="ok" onclick="savePassthrough()" ${canPass?"":"disabled"}>✓ 記録どおり</button>`}
          <button class="primary" onclick="saveEdited()">保存（この内容が真）</button>
          ${blind ? "" : '<button class="sm" onclick="resetToCaptured()">記録の内容に戻す</button>'}
          ${blind || canPass ? "" : '<span class="small muted">要確認の行を左の ✓ ですべて確かめると「記録どおり」にできます。違っていれば直して保存してください。</span>'}
        </div>
      </div>
    </div>`;
}

// ――― カード選択 ―――
function openPicker(kind, a, b){
  const g = S.gt;
  const used = new Map();
  g.board.forEach(c => { if (c) used.set(c, "ボード"); });
  g.players.forEach(p => p.hole_cards.forEach(c => { if (c) used.set(c, "席 " + p.seat); }));
  const cur = kind === "board" ? g.board[a] : g.players.find(p => p.seat === a).hole_cards[b];
  S.picker = {kind, a, b};
  const title = kind === "board" ? `ボード ${a + 1} 枚目（${BOARD_LABEL[a]}）` : `席 ${a} の手札 ${b + 1} 枚目`;
  $("modal").innerHTML = `<div class="sheet"><div class="sheet-h"><span>${title}</span>
      <button class="sm" onclick="pickCard(null)">消す</button><button class="sm" onclick="closePicker()">閉じる</button></div>
    ${SUITS.map(s => `<div class="suitrow ${s==="h"||s==="d"?"red":""}">${RANKS.map(r => {
        const c = r + s; const u = used.has(c) && c !== cur;
        return `<button class="pick ${u?"used":""} ${c===cur?"cur":""}" onclick="pickCard('${c}')" title="${u?esc(used.get(c))+" で使用":""}">${r==="T"?"10":r}${SUIT_SYM[s]}</button>`;
      }).join("")}</div>`).join("")}</div>`;
  $("modal").classList.remove("hidden");
}
function closePicker(){ $("modal").classList.add("hidden"); S.picker = null; }
function pickCard(c){
  const p = S.picker; if (!p) return;
  const g = S.gt;
  if (c) { g.board = g.board.map(x => x === c ? null : x); g.players.forEach(pl => { pl.hole_cards = pl.hole_cards.map(x => x === c ? null : x); }); }
  if (p.kind === "board") g.board[p.a] = c; else g.players.find(pl => pl.seat === p.a).hole_cards[p.b] = c;
  S.dirty = true; closePicker(); renderEdit(); refreshLegal();   // 札が変わればショーダウンの判定も変わる
}
$("modal").addEventListener("click", (e) => { if (e.target === $("modal")) closePicker(); });

// ――― 保存 ―――
function gtPayload(){
  const g = S.gt;
  return {board: g.board.filter(Boolean),
          actions: g.actions.map(a => ({seat:a.seat, action:a.action, amount:a.amount || 0, street:a.street, unsure: a.unsure || undefined})),
          players: g.players.map(p => ({seat:p.seat, name:p.name, hole_cards: p.hole_cards.filter(Boolean), showed_down: p.showed_down})),
          winner_seat: g.winner_seat, notes: g.notes || "", blind: isBlind() || undefined,
          button_seat: g.button_seat != null && g.button_seat !== S.hand.captured.button_seat ? g.button_seat : undefined};
}
async function saveWith(body){
  if (S.busy) return; S.busy = true;
  try {
    const d = await api(sidPath() + "/hands/" + S.hand.captured.hand_id, {method:"PUT", body: JSON.stringify(body)});
    const a = d.accuracy || {};
    toast(a.all_match ? `保存しました — 記録と一致` : `保存しました — 記録との差分 ${a.mismatches}（アクション ${a.action_correct}/${a.action_total}${a.board_match?"":"・ボード"}${a.winner_match===false?"・勝者":""}）`);
    S.dirty = false;
    if (body.hand && body.hand.blind) {        // ブラインドで入れた → 記録と照らし合わせる
      await openHand(S.hand.captured.hand_id);
      return;
    }
    S.view = "list"; S.hand = null; renderList(); await refreshAll();
  } catch (e) { toast("保存できません: " + e.message, true); }
  finally { S.busy = false; }
}
function entrySec(){ return Math.round((Date.now() - S.openedAt) / 100) / 10; }
function savePassthrough(){
  saveWith({source:"captured-passthrough", annotator: S.annotator || "staff", entry_sec: entrySec(),
            confirmed_rows: [...S.confirmed], confirmed_hand: S.confirmedHand});
}
function saveEdited(){
  const L = S.legal;
  if (L && L.error && L.next && !confirm("赤い行（反映できないアクション）があります。このまま保存しますか？")) return;
  if (S.gt.winner_seat == null && !confirm("勝った席が未定です。このまま保存しますか？")) return;
  saveWith({source:"manual-edit", annotator: S.annotator || "staff", hand: gtPayload(), entry_sec: entrySec()});
}

// ――― 起動 ―――
try { S.annotator = localStorage.getItem("gt_annotator") || ""; } catch (e) {}
refreshAll();
// ハンドだけでなくセッションの一覧も読み直す（ロガーをあとから起動したセッションも出す, 店舗 2026-09-30）
setInterval(async () => {
  if (S.view !== "list" || S.busy || editing()) return;
  try { await loadSessions(); } catch (e) {}
  loadHands(true);
}, 5000);
</script></body></html>
"""


if __name__ == "__main__":
    sys.exit(main())
