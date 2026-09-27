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

使い方:

    python tools/ground_truth_ui.py                          # http://127.0.0.1:8791/
    python tools/ground_truth_ui.py --host 0.0.0.0 --port 8791   # iPad から http://<PC の IP>:8791/

ハンドの一覧は 5 秒ごとに読み直すので、卓の脇でハンドが終わるたびに入れられる。
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
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
            {"source": gt.source, "annotated_at": gt.annotated_at, "annotator": gt.annotator}
            if gt is not None else None
        ),
        "accuracy": accuracy,
    }


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
    }
    rows.sort(key=lambda r: r["hand_id"], reverse=True)
    return {"session_id": session_id, "hands": rows, "summary": summary}


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


def hand_detail(
    log_dir: Path, session_id: str, hand_id: int, gt_repo: GroundTruthRepository,
    corr_repo: Optional[HandCorrectionRepository] = None,
) -> Optional[dict]:
    captured = _get_hand(log_dir, session_id, hand_id, corr_repo)
    if captured is None:
        return None
    gt = gt_repo.get(session_id, hand_id)
    initial = _gt_actions(gt.hand) if gt is not None else _gt_actions(captured)
    return {
        "session_id": session_id,
        "captured": captured,
        "ground_truth": gt.to_dict() if gt is not None else None,
        "has_needs_review": hand_has_needs_review(captured),
        "legal": replay_legal(captured, initial),
    }


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


def replay_legal(captured: dict, actions: list[dict]) -> dict:
    """GT のアクション列を pokerkit で流し、各行のストリートと額（コールは自動）、次の手番を返す。

    返り値: `{"actions": [{seat, action, amount, street}], "next": {...} | None, "error": {index, message} | None}`。
    `error` はその行から先を反映できなかった理由（手番違い・額の範囲外など）。`next` は最後に反映できた
    ところの手番（`hand_over` ならベッティングは終わり、`foldout_winner` はほかが全員降りた勝者）。
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
        button = captured.get("button_seat")
        if isinstance(button, int) and any(p.seat == button for p in players):
            gs.set_button(button)
        gs.new_hand()
    except (ImportError, ValueError, RuntimeError) as e:
        return {"actions": [], "next": None,
                "error": {"index": -1, "message": f"pokerkit で再現できません: {e}"}}

    out: list[dict] = []
    error: Optional[dict] = None
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
            error = {"index": i, "message": "ベッティングは終わっています（この行は入りません）"}
            break
        if seat != ctx.actor_seat:
            error = {"index": i, "message": f"手番は席{ctx.actor_seat} です（席{seat} の番ではありません）"}
            break
        street = gs.street
        amount = 0
        try:
            if act == "fold":
                if "fold" in ctx.legal_actions:
                    gs.apply_action(seat, "fold")
                else:
                    gs.force_fold(seat)              # チェックできるときに降りた（実際にある）
            elif act in ("check", "call"):
                act = "call" if ctx.amount_to_call > 0 else "check"
                amount = ctx.amount_to_call
                gs.apply_action(seat, act)
            elif act == "allin":
                amount = ctx.max_raise if ctx.max_raise else ctx.amount_to_call
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
        except ValueError as e:
            error = {"index": i, "message": str(e)}
            break
        out.append({"seat": seat, "action": act, "amount": amount, "street": street})

    ctx = gs.legal_context()
    try:
        active = gs.get_active_seats()
    except Exception:  # noqa: BLE001
        active = []
    nxt = {
        "street": gs.street,
        "actor_seat": ctx.actor_seat,
        "legal_actions": sorted(ctx.legal_actions),
        "amount_to_call": ctx.amount_to_call,
        "min_raise": ctx.min_raise,
        "max_raise": ctx.max_raise,
        "pot": gs.pot,
        "active_seats": active,
        "hand_over": ctx.actor_seat is None,
        "foldout_winner": active[0] if len(active) == 1 else None,
    }
    return {"actions": out, "next": nxt, "error": error}


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
    notes = hand.get("notes")
    if isinstance(notes, str) and notes.strip():
        out["notes"] = notes.strip()[:2000]
    return out


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
        if hand_has_needs_review(captured):
            return 400, {
                "code": "invalid_amount",
                "message": "要確認のハンドは「記録どおり」にできません。内容を確かめて「保存」してください（ADR-0043）。",
            }
        hand_body = captured
    else:
        try:
            hand_body = validate_gt_hand(body.get("hand"))
        except GroundTruthError as e:
            return 400, {"code": "invalid_amount", "message": str(e)}
    entry = gt_repo.upsert(session_id, hand_id, hand_body, annotator=annotator, source=source)
    accuracy = _accuracy_dict(measure_hand(dict(entry.hand, hand_id=hand_id), captured))
    return 200, {"saved": entry.to_dict(), "accuracy": accuracy}


# ───────────────────────── HTTP ─────────────────────────

_ROUTE_SESSIONS = re.compile(r"^/api/sessions/?$")
_ROUTE_HANDS = re.compile(r"^/api/sessions/([^/]+)/hands/?$")
_ROUTE_HAND = re.compile(r"^/api/sessions/([^/]+)/hands/(\d+)/?$")
_ROUTE_LEGAL = re.compile(r"^/api/sessions/([^/]+)/hands/(\d+)/legal/?$")


class GroundTruthServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self, address: tuple[str, int], log_dir: Path,
        corrections: Optional[Path] = None,
    ) -> None:
        self.log_dir = Path(log_dir)
        self.gt_repo = GroundTruthRepository(self.log_dir)
        self.corr_repo: Optional[HandCorrectionRepository] = None
        if corrections is not None and Path(corrections).is_file():
            self.corr_repo = HandCorrectionRepository(corrections)
        super().__init__(address, _Handler)


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

    def _send_page(self) -> None:
        body = _PAGE.encode("utf-8")
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

    @staticmethod
    def _session(raw: str) -> Optional[str]:
        sid = unquote(raw)
        return sid if _SESSION_RE.match(sid) else None

    # ――― ルーティング ―――

    def do_GET(self) -> None:          # noqa: N802 — BaseHTTPRequestHandler の規約
        path = urlsplit(self.path).path
        srv = self.server
        if path in ("/", "/index.html"):
            self._send_page()
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
            data = hand_detail(srv.log_dir, sid, int(m.group(2)), srv.gt_repo, srv.corr_repo) if sid else None
            if data is None:
                self._send_json(404, {"code": "not_found", "message": "ハンドがありません"})
            else:
                self._send_json(200, data)
            return
        self._send_json(404, {"code": "not_found", "message": path})

    def do_POST(self) -> None:         # noqa: N802
        path = urlsplit(self.path).path
        srv = self.server
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
            self._send_json(200, replay_legal(captured, actions))
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
    corrections: Optional[Path] = None,
) -> GroundTruthServer:
    return GroundTruthServer((host, port), log_dir, corrections)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="真のアクション入力（ground truth）の画面")
    ap.add_argument("--log-dir", default="./logs", help="ログディレクトリ（既定 ./logs）")
    ap.add_argument("--host", default="127.0.0.1", help="bind host（iPad から使うなら 0.0.0.0）")
    ap.add_argument("--port", type=int, default=8791, help="bind port（既定 8791）")
    ap.add_argument("--corrections", default="hand_corrections.json",
                    help="ハンド訂正の保存先（あれば訂正を重ねて表示・比較する, ADR-0036）")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log_dir = Path(args.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    server = make_server(log_dir, args.host, args.port, Path(args.corrections))
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
 .panel { background:#161a20; border:1px solid #2d333b; border-radius:12px; padding:12px 14px; }
 .seatrow { display:flex; flex-wrap:wrap; align-items:center; gap:8px; padding:6px 0; border-bottom:1px solid #2d333b; }
 .seatrow .no { min-width:120px; }
 .chips { display:flex; flex-wrap:wrap; gap:6px; }
 .chip { border-radius:999px; padding:6px 14px; }
 .chip.on { background:#1f6feb; border-color:#1f6feb; }
 tr.err td { background:#3a1f1f; }
 tr.errmsg td { color:#ff7b72; font-size:13px; }
 tr.warn td { background:#2a2416; }
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
const S = {sessions:[], sid:null, hands:null, summary:null, view:"list", hand:null, gt:null, legal:null,
           dirty:false, annotator:"", picker:null, busy:false};
const $ = (id) => document.getElementById(id);
function esc(s){ return String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }
function fmtTime(iso){ const m = String(iso||"").match(/T(\d\d:\d\d:\d\d)/); return m ? m[1] : (iso || "—"); }
function pct(a, b){ return b ? Math.round(1000 * a / b) / 10 + "%" : "—"; }
function cardHtml(c, cls, onclick){
  const cl = cls || "";
  if (!c) return `<span class="card empty ${cl}" ${onclick?`onclick="${onclick}"`:""}>＋</span>`;
  const red = c[1] === "h" || c[1] === "d";
  const rank = c[0] === "T" ? "10" : c[0];
  return `<span class="card ${red?"red":""} ${cl}" ${onclick?`onclick="${onclick}"`:""}>${rank}${SUIT_SYM[c[1]]||c[1]}</span>`;
}
async function api(path, opts){
  const r = await fetch(path, Object.assign({headers:{"Content-Type":"application/json"}}, opts || {}));
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
  if (!S.sid || !S.sessions.some(s => s.session_id === S.sid)) S.sid = S.sessions.length ? S.sessions[0].session_id : null;
}
async function loadHands(){
  if (!S.sid) { S.hands = []; S.summary = null; if (S.view === "list") renderList(); return; }
  try {
    const d = await api(sidPath() + "/hands");
    S.hands = d.hands || []; S.summary = d.summary || null;
  } catch (e) { S.hands = []; S.summary = null; }
  if (S.view === "list") renderList();
}
async function selectSession(sid){ S.sid = sid; S.hands = null; renderList(); await loadHands(); }
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
      <div>勝者一致 <b>${pct(sm.winner_match, sm.winner_total)}</b></div></div>` : "";
  const next = (S.hands||[]).filter(h => !h.ground_truth).sort((a,b) => a.hand_id - b.hand_id)[0];
  const rows = (S.hands||[]).map(h => {
    const st = h.ground_truth
      ? (h.ground_truth.source === "captured-passthrough" ? '<span class="tag t-ok">記録どおり</span>' : '<span class="tag t-edit">修正済</span>')
      : '<span class="tag t-none">未入力</span>';
    const rv = h.has_needs_review ? '<span class="tag t-warn">要確認</span>' : "";
    const acc = h.accuracy ? (h.accuracy.all_match ? '<span class="tag t-ok">一致</span>' : `<span class="tag t-bad">差分 ${h.accuracy.mismatches}</span>`) : "";
    return `<tr class="row" onclick="openHand(${h.hand_id})"><td>#${h.hand_id}</td><td>${fmtTime(h.started_at)}</td>
      <td>${(h.seats||[]).join(" ")}</td><td>${(h.board||[]).map(c => cardHtml(c, "sm")).join("")}</td>
      <td>${h.winner_seat != null ? "席 " + h.winner_seat : "—"}</td><td>${rv} ${st} ${acc}</td></tr>`;
  }).join("");
  $("app").innerHTML = `<h1>真のアクション入力</h1>
    <div class="bar">
      <select onchange="selectSession(this.value)">${sess || "<option>セッションがありません</option>"}</select>
      <button class="sm" onclick="refreshAll()">↻ 読み直す</button>
      <label class="small muted">入力者 <input type="text" value="${esc(S.annotator)}" onchange="setAnnotator(this.value)" style="width:120px"></label>
      ${next ? `<button class="primary sm" onclick="openHand(${next.hand_id})">次の未入力 #${next.hand_id} →</button>` : ""}
    </div>
    ${summary}
    ${S.hands === null ? "<p class='muted'>読み込み中…</p>" :
      (rows ? `<div class="tbl"><table><tr><th>#</th><th>時刻</th><th>席</th><th>ボード</th><th>勝者</th><th>状態</th></tr>${rows}</table></div>`
            : "<p class='muted'>このセッションにはまだハンドがありません。ハンドが終わると 5 秒以内にここに出ます。</p>")}`;
}

// ――― 編集 ―――
function normCards(arr, n){ const out = []; for (let i = 0; i < n; i++) out.push((arr && arr[i]) ? arr[i] : null); return out; }
function buildGt(d){
  const cap = d.captured, g = d.ground_truth, src = g || cap;
  const players = (cap.players || []).filter(p => typeof p.seat === "number").map(p => {
    const gp = (g && (g.players || []).find(x => x.seat === p.seat)) || p;
    return {seat:p.seat, name:p.name || "", hole_cards: normCards(gp.hole_cards, 2), showed_down: !!gp.showed_down};
  });
  const actions = (src.actions || []).filter(a => GT_ACTIONS.includes(a.action) && typeof a.seat === "number")
                  .map(a => ({seat:a.seat, action:a.action, amount:a.amount || 0}));
  return {board: normCards(src.board, 5), players, actions,
          winner_seat: (src.winner_seat === undefined ? null : src.winner_seat), notes: (g && g.notes) || ""};
}
async function openHand(hid){
  try {
    const d = await api(sidPath() + "/hands/" + hid);
    S.hand = d; S.gt = buildGt(d); S.legal = d.legal; S.dirty = false; S.view = "edit";
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
}
let legalTimer = null;
function refreshLegal(){
  clearTimeout(legalTimer);
  legalTimer = setTimeout(async () => {
    try {
      const body = {actions: S.gt.actions.map(a => ({seat:a.seat, action:a.action, amount:a.amount}))};
      S.legal = await api(sidPath() + "/hands/" + S.hand.captured.hand_id + "/legal", {method:"POST", body: JSON.stringify(body)});
      applyLegal();
    } catch (e) { toast("手番の確認に失敗: " + e.message, true); }
    renderEdit();
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
  touch();
}
function setWinner(seat){ S.gt.winner_seat = (S.gt.winner_seat === seat) ? null : seat; S.dirty = true; renderEdit(); }
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
  const rows = g.actions.map((a, i) => {
    const l = la[i];
    const isErr = err && err.index === i;
    const street = l ? (STREET_JA[l.street] || l.street) : (isErr ? "" : (replayOk ? "?" : ""));
    const streetCell = replayOk ? `<td class="street">${street}</td>`
      : `<td class="street"><select onchange="setRowStreet(${i}, this.value)">${["preflop","flop","turn","river"].map(s => `<option value="${s}" ${a.street===s?"selected":""}>${STREET_JA[s]}</option>`).join("")}</select></td>`;
    const needAmt = ["bet","raise","allin"].includes(a.action);
    const amtCell = needAmt
      ? `<input type="number" inputmode="numeric" value="${a.amount || ""}" onchange="setRowAmount(${i}, this.value)" ${a.action==="allin"?"disabled":""}>`
      : (a.action === "call" ? `<span class="muted">${a.amount || ""}</span>` : "");
    return `<tr class="${isErr?"err":""}">${streetCell}
      <td><select onchange="setRowSeat(${i}, this.value)">${seats.map(s => `<option value="${s}" ${s===a.seat?"selected":""}>席 ${s}</option>`).join("")}</select></td>
      <td><select onchange="setRowAction(${i}, this.value)">${GT_ACTIONS.map(x => `<option value="${x}" ${x===a.action?"selected":""}>${ACTION_JA[x]}</option>`).join("")}</select></td>
      <td>${amtCell}</td>
      <td class="tools"><button class="sm" onclick="insRow(${i})" title="この前に挿入">＋</button> <button class="sm danger" onclick="delRow(${i})">✕</button></td></tr>
      ${isErr ? `<tr class="errmsg"><td colspan="5">⚠ ${esc(err.message)}</td></tr>` : ""}`;
  }).join("");
  let quick = "";
  if (!replayOk) {
    quick = `<div class="quick"><div class="who err">手番の自動補完は使えません</div><div class="small muted">${esc((err && err.message) || "")}。ストリートは各行で選んでください。</div>
      </div>`;
  } else if (err) {
    quick = `<div class="quick"><div class="who err">赤い行を直してください</div><div class="small muted">その先の手番はまだ決められません。</div></div>`;
  } else if (n.hand_over) {
    const who = n.foldout_winner != null
      ? `席 ${n.foldout_winner} の勝ち（ほかは全員フォールド）`
      : `ショーダウン: 席 ${(n.active_seats||[]).join("・")} — 勝った席を下で選んでください`;
    quick = `<div class="quick"><div class="who">ベッティング終了 — ${who}</div><div class="small muted">ポット ${n.pot}。行が足りなければ「＋ 行を追加」、多ければ ✕ で直せます。</div></div>`;
  } else {
    const legal = n.legal_actions || [];
    const cc = n.amount_to_call > 0 ? `コール ${n.amount_to_call}` : "チェック";
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
  const winnerHtml = `<div class="chips">${seats.map(s => `<button class="chip ${g.winner_seat===s?"on":""}" onclick="setWinner(${s})">席 ${s}</button>`).join("")}
      <span class="muted small" style="align-self:center">${g.winner_seat==null?"（未定）":""}</span></div>`;
  const capRows = (cap.actions || []).map(a => `<tr class="${a.needs_review?"warn":""}"><td class="street">${STREET_JA[a.street]||a.street||""}</td><td>席 ${a.seat}</td>
      <td>${ACTION_JA[a.action]||a.action}</td><td>${a.amount||""}</td>
      <td class="muted small raw">${esc(a.raw_text||"")}${a.needs_review?` <span class="tag t-warn">要確認${a.reason?": "+esc(a.reason):""}</span>`:""}</td></tr>`).join("");
  const capPlayers = (cap.players || []).map(p => `席 ${p.seat}: ${(p.hole_cards||[]).map(c => cardHtml(c, "sm")).join("") || "<span class='muted'>—</span>"}`).join(" ｜ ");
  const gtMeta = d.ground_truth ? `<span class="tag ${d.ground_truth.source==="captured-passthrough"?"t-ok":"t-edit"}">${d.ground_truth.source==="captured-passthrough"?"記録どおり":"修正済"} ${esc(d.ground_truth.annotated_at||"")} ${esc(d.ground_truth.annotator||"")}</span>` : '<span class="tag t-none">未入力</span>';
  const canPass = !d.has_needs_review;
  $("app").innerHTML = `
    <div class="bar"><button onclick="backToList()">← 一覧</button><h1 style="margin:0">ハンド #${cap.hand_id}</h1>
      <span class="muted small">${fmtTime(cap.started_at)} ／ ボタン 席 ${cap.button_seat ?? "—"} ／ ブラインド ${(cap.blinds||{}).sb ?? "?"}/${(cap.blinds||{}).bb ?? "?"}</span> ${gtMeta}</div>
    <div class="cols">
      <div class="panel cap">
        <h2 style="margin-top:0">記録（システムが取ったもの）</h2>
        <div>ボード: ${(cap.board||[]).map(c => cardHtml(c, "sm")).join("") || "<span class='muted'>—</span>"}</div>
        <div class="small" style="margin:4px 0">${capPlayers}</div>
        <div class="small">勝者: ${cap.winner_seat != null ? "席 " + cap.winner_seat : "—"} ${cap.winner_source ? `<span class="muted">(${esc(cap.winner_source)})</span>` : ""}
          ${cap.announced_hand ? `／ 役名 ${esc(cap.announced_hand)}` : ""} ／ ポット ${cap.pot_total ?? "—"}
          ${d.has_needs_review ? '<span class="tag t-warn">要確認</span>' : ""}</div>
        <div class="tbl" style="margin-top:8px"><table><tr><th>ストリート</th><th>席</th><th>アクション</th><th>額</th><th>聞き取り</th></tr>${capRows || "<tr><td colspan='5' class='muted'>アクションなし</td></tr>"}</table></div>
      </div>
      <div class="panel">
        <h2 style="margin-top:0">実際（真のアクション）</h2>
        <h2>ボード</h2><div style="display:flex;gap:6px;flex-wrap:wrap">${boardHtml}</div>
        <h2>手札（分かる席だけ）</h2>${playersHtml}
        <h2>アクション <span class="muted small">コールの額とストリートは自動。ベット / レイズはトータルの額</span></h2>
        <div class="tbl"><table><tr><th>ストリート</th><th>席</th><th>アクション</th><th>額</th><th></th></tr>${rows || "<tr><td colspan='5' class='muted'>まだありません（下のボタンで足す）</td></tr>"}</table></div>
        <div class="rowadd"><button class="sm" onclick="addRow()">＋ 行を追加</button>
          <span class="muted small">最後に 1 行足します（席・アクション・額はあとで変えられます。行の ＋ はその行の前に入れます）</span></div>
        ${quick}
        <h2>勝った席</h2>${winnerHtml}
        <h2>メモ</h2><input type="text" style="width:100%" value="${esc(g.notes)}" placeholder="気づいたこと（任意）" onchange="setNotes(this.value)">
        <div class="actions-bottom">
          <button class="ok" onclick="savePassthrough()" ${canPass?"":"disabled"}>✓ 記録どおり</button>
          <button class="primary" onclick="saveEdited()">保存（この内容が真）</button>
          <button class="sm" onclick="resetToCaptured()">記録の内容に戻す</button>
          ${canPass ? "" : '<span class="small muted">要確認のハンドは「記録どおり」にできません。内容を確かめて保存してください。</span>'}
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
  S.dirty = true; closePicker(); renderEdit();
}
$("modal").addEventListener("click", (e) => { if (e.target === $("modal")) closePicker(); });

// ――― 保存 ―――
function gtPayload(){
  const g = S.gt;
  return {board: g.board.filter(Boolean),
          actions: g.actions.map(a => ({seat:a.seat, action:a.action, amount:a.amount || 0, street:a.street})),
          players: g.players.map(p => ({seat:p.seat, name:p.name, hole_cards: p.hole_cards.filter(Boolean), showed_down: p.showed_down})),
          winner_seat: g.winner_seat, notes: g.notes || ""};
}
async function saveWith(body){
  if (S.busy) return; S.busy = true;
  try {
    const d = await api(sidPath() + "/hands/" + S.hand.captured.hand_id, {method:"PUT", body: JSON.stringify(body)});
    const a = d.accuracy || {};
    toast(a.all_match ? `保存しました — 記録と一致` : `保存しました — 記録との差分 ${a.mismatches}（アクション ${a.action_correct}/${a.action_total}${a.board_match?"":"・ボード"}${a.winner_match===false?"・勝者":""}）`);
    S.dirty = false; S.view = "list"; S.hand = null; renderList(); await refreshAll();
  } catch (e) { toast("保存できません: " + e.message, true); }
  finally { S.busy = false; }
}
function savePassthrough(){ saveWith({source:"captured-passthrough", annotator: S.annotator || "staff"}); }
function saveEdited(){
  const L = S.legal;
  if (L && L.error && L.next && !confirm("赤い行（反映できないアクション）があります。このまま保存しますか？")) return;
  if (S.gt.winner_seat == null && !confirm("勝った席が未定です。このまま保存しますか？")) return;
  saveWith({source:"manual-edit", annotator: S.annotator || "staff", hand: gtPayload()});
}

// ――― 起動 ―――
try { S.annotator = localStorage.getItem("gt_annotator") || ""; } catch (e) {}
refreshAll();
setInterval(() => { if (S.view === "list" && !S.busy) loadHands(); }, 5000);
</script></body></html>
"""


if __name__ == "__main__":
    sys.exit(main())
