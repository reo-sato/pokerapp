"""真のアクション（`logs/<sid>.ground_truth.json`, ADR-0043）を pokerkit で流す。

- 真のアクションの入力画面（`tools/ground_truth_ui.py:replay_legal`）が手番・ストリート・コールの額を補うのと同じ規則。
- `materialize_truth`: 真のアクションのハンドを、ライブの記録と同じ形（ポット・持ち点・収支つき）にする。お客さんの画面に
  真のアクションを見せる（`viewer_api.show_truth`）・ロガーのスタックを真のアクションで直す（`ground_truth.feed_stacks`）
  に使う（2026-10-19, オーナー: 実践環境のテストで全部のハンドに真のアクションを入れ、お客さんに見せる）。

真のアクションの行は席・アクション・額（ベット / レイズはトータル、コールは自動）だけを持つ。持ち点・ブラインド・ボタンは
同じハンドのライブの記録から取る（真のアクションの `button_seat` があればそれ）。
"""
from __future__ import annotations

import json
import logging
from copy import deepcopy
from pathlib import Path
from typing import Any, Optional

from core.game_state import PlayerState
from core.hand_log import UNKNOWN_CARD
from core.poker_engine import PokerkitGameState

logger = logging.getLogger(__name__)

GT_ACTIONS = ("fold", "check", "call", "bet", "raise", "allin")
# 記録に持ち点が無いハンド（台本・古い記録）は十分大きい持ち点で流す（額の範囲で弾かない）
FALLBACK_STACK = 10_000_000


def players_for_replay(captured: dict) -> list[PlayerState]:
    players: list[PlayerState] = []
    for p in captured.get("players") or []:
        if not isinstance(p, dict) or not isinstance(p.get("seat"), int):
            continue
        stack = p.get("stack_start")
        stack = int(stack) if isinstance(stack, (int, float)) and stack > 0 else FALLBACK_STACK
        players.append(PlayerState(seat=p["seat"], name=str(p.get("name") or f"席{p['seat']}"), stack=stack))
    return sorted(players, key=lambda p: p.seat)


def showdown_muck_error(active: list[int], seat: int, act: str) -> Optional[str]:
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


def start_state(captured: dict, button: Optional[int] = None) -> PokerkitGameState:
    """記録の持ち点・ブラインド・ボタン（`button` があればそれ）で始めたハンド。始められなければ ValueError など。"""
    players = players_for_replay(captured)
    if len(players) < 2:
        raise ValueError("記録に席が 2 つ未満です")
    blinds = captured.get("blinds") if isinstance(captured.get("blinds"), dict) else {}
    try:
        sb = max(1, int(blinds.get("sb") or 1))
        bb = max(sb, int(blinds.get("bb") or 2))
    except (TypeError, ValueError):
        sb, bb = 1, 2
    gs = PokerkitGameState(players, sb, bb)
    if blinds.get("bb_ante"):
        gs.set_bb_ante(True)               # 記録のハンドに BB アンティがあった（BB の人が BB と同額）
    if button is None:
        button = captured.get("button_seat")
    if isinstance(button, int) and any(p.seat == button for p in players):
        gs.set_button(button)
    gs.new_hand()
    return gs


def active_seats(gs: Any, mucked: list[int]) -> list[int]:
    """ショーダウンに残っている席（降りた・見せずにマックした席を除く）。"""
    try:
        return [s for s in gs.get_active_seats() if s not in mucked]
    except Exception:  # noqa: BLE001
        return []


def apply_rows(gs: Any, actions: list) -> tuple[list[dict], list[int], Optional[dict]]:
    """真のアクションの行を順に流す。返り値: (反映できた行, 見せずにマックした席, 止まった理由 | None)。

    行は `{seat, action, amount, total, street, pot_after, stack_after}`。`amount` はコールなら追加額（記録と同じ）、
    `total` はそのストリートでその人が出した合計。ベッティングが終わったあとのフォールドは street=showdown のマック。
    """
    out: list[dict] = []
    mucked: list[int] = []
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
            reason = showdown_muck_error(active_seats(gs, mucked), seat, act)
            if reason is not None:
                error = {"index": i, "message": reason}
                break
            mucked.append(seat)
            out.append({"seat": seat, "action": "fold", "amount": 0, "street": "showdown",
                        "pot_after": gs.pot, "stack_after": gs.get_stack(seat)})
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
        out.append({"seat": seat, "action": act, "amount": amount, "total": total, "street": street,
                    "pot_after": gs.pot, "stack_after": gs.get_stack(seat)})
    return out, mucked, error


def _known(cards: Any) -> list[str]:
    return [c for c in cards or [] if isinstance(c, str) and c and c != UNKNOWN_CARD]


def showdown_awards(
    captured: dict, gs: Any, active: list[int], board: Optional[list], holes: Optional[dict],
) -> Optional[tuple[dict[int, int], list[list[int]]]]:
    """ショーダウンの配当を入力したボードと手札（無ければ記録の札）で判定する（`core/showdown.py`）。
    読めていない札があっても、どの札でも同じ配当なら決める。決まらなければ None。"""
    from core.showdown import award_with_unknown_cards

    cards = _known(board if board is not None else captured.get("board"))
    recorded = {p.get("seat"): p.get("hole_cards") or [] for p in captured.get("players") or []
                if isinstance(p, dict)}
    hands = {s: _known((holes or {}).get(s) if holes and s in holes else recorded.get(s)) for s in active}
    try:
        pots = gs.current_pots() or [{"amount": gs.pot, "eligible_seats": active}]
        return award_with_unknown_cards(hands, cards, pots, gs.acting_order())
    except Exception:  # noqa: BLE001 — 判定できなければ未定のまま（人が選ぶ）
        return None


def showdown_winner(
    captured: dict, gs: Any, active: list[int], board: Optional[list], holes: Optional[dict],
) -> Optional[int]:
    """ショーダウンの勝者の 1 席（main pot の勝者が 1 人のとき）。引き分け・決まらないときは None。"""
    decided = showdown_awards(captured, gs, active, board, holes)
    winners = decided[1][0] if decided and decided[1] else []
    return winners[0] if len(winners) == 1 else None


def _gt_holes(gt: dict) -> dict[int, list[str]]:
    holes: dict[int, list[str]] = {}
    for p in gt.get("players") or []:
        if isinstance(p, dict) and isinstance(p.get("seat"), int) and p.get("hole_cards"):
            holes[p["seat"]] = list(p["hole_cards"])
    return holes


def materialize_truth(gt: dict, captured: dict) -> Optional[dict]:
    """真のアクションのハンドを、ライブの記録と同じ形のハンドにする（元は変えない）。

    終わっていない（行が途中で止まる・ベッティングが終わっていない・ショーダウンの勝者が決まらない）ときは None。
    勝者は、ほかが全員降りた → その席 / ショーダウン → 手札とボードの判定（pot ごと）、判定が真のアクションの勝者と
    違う・判定できないときは真のアクションの勝者に全部。
    """
    actions = gt.get("actions")
    if not isinstance(actions, list) or not actions:
        return None
    button = gt.get("button_seat") if isinstance(gt.get("button_seat"), int) else None
    try:
        gs = start_state(captured, button)
    except (ImportError, ValueError, RuntimeError) as e:
        logger.info("真のアクションを流せません（ハンド %s）: %s", captured.get("hand_id"), e)
        return None
    rows, mucked, error = apply_rows(gs, actions)
    if error is not None or gs.legal_context().actor_seat is not None:
        return None
    positions = gs.position_map()
    hand_seats = set(gs.seats_in_hand())
    active = active_seats(gs, mucked)
    # ボードは入力画面が記録の札で自動に入れる（真のアクションに無ければ記録のボード）
    board = gt.get("board") if isinstance(gt.get("board"), list) and gt.get("board") else captured.get("board")
    holes = _gt_holes(gt)
    winner = gt.get("winner_seat") if isinstance(gt.get("winner_seat"), int) else None
    pot = gs.pot
    awards: Optional[dict[int, int]] = None
    source = "fold"
    if len(active) == 1:
        winner = active[0]
    else:
        decided = showdown_awards(captured, gs, active, board, holes)
        main = decided[1][0] if decided and decided[1] else []
        if decided is not None and (winner is None or winner in main):
            awards = decided[0]
            source = "cards"
            if winner is None and len(main) == 1:
                winner = main[0]
        elif winner is not None and winner in active:
            source = "truth"
        else:
            return None
    try:
        if awards is not None:
            gs.end_hand_awards(awards)
        else:
            gs.end_hand(winner)
    except (ValueError, RuntimeError) as e:
        logger.info("真のアクションのハンドを閉じられません（ハンド %s）: %s", captured.get("hand_id"), e)
        return None
    end = gs.get_stacks()
    stack_start = {p.seat: p.stack for p in players_for_replay(captured)}

    live_players = {p.get("seat"): p for p in captured.get("players") or [] if isinstance(p, dict)}
    shown = {p.get("seat") for p in gt.get("players") or [] if isinstance(p, dict) and p.get("showed_down")}
    players = []
    for seat in sorted(hand_seats):
        live = live_players.get(seat, {})
        cards = holes.get(seat) or live.get("hole_cards") or None
        players.append({
            "seat": seat, "name": live.get("name") or f"席{seat}",
            **({"player_id": live["player_id"]} if live.get("player_id") else {}),
            "hole_cards": list(cards) if cards else None,
            "stack_start": stack_start.get(seat), "stack_end": end.get(seat),
            "result": end.get(seat, 0) - stack_start.get(seat, 0),
        })
    names = {p["seat"]: p["name"] for p in players}
    out_actions = []
    for row in rows:
        out_actions.append({
            "street": row["street"], "seat": row["seat"], "player_name": names.get(row["seat"]),
            "position": positions.get(row["seat"]), "action": row["action"], "amount": row["amount"],
            "pot_after": row["pot_after"], "stack_after": row["stack_after"], "confidence": 1.0,
            "needs_review": False,
        })
    hand: dict[str, Any] = {
        key: deepcopy(captured[key]) for key in ("session_id", "hand_id", "started_at", "ended_at", "blinds")
        if key in captured
    }
    hand.update({
        "board": list(board or []),
        "players": players,
        "actions": out_actions,
        "pot_total": pot,
        "pots": gs.pots(),
        "winner_seat": winner,
        "winner_source": source,
        "button_seat": gs.button_seat,
        "position_map": {str(s): name for s, name in positions.items()},
        "showdown": [{"seat": s, "hole_cards": holes.get(s) or live_players.get(s, {}).get("hole_cards")}
                     for s in sorted(shown | (set(active) if len(active) > 1 else set())) if s in hand_seats],
        "needs_review": False,
        "review_required": False,
        "truth": True,
    })
    if awards is not None and len([a for a in awards.values() if a]) > 1:
        hand["pot_awards"] = {str(s): a for s, a in awards.items() if a}
    return hand


def truth_deltas(gt: dict, captured: dict) -> Optional[dict[int, int]]:
    """真のアクションの収支 − ライブの記録の収支（席ごと, 0 の席は除く）。ハンドが終わっていなければ None。

    同じハンドの同じ持ち点から出した差なので、前のハンドの持ち点が違っていても・ハンドの間に買い足しがあっても、
    ロガーの持ち点に足せば真のアクションに合う（`ground_truth.feed_stacks`）。
    """
    truth = materialize_truth(gt, captured)
    if truth is None:
        return None
    live = {p.get("seat"): p for p in captured.get("players") or [] if isinstance(p, dict)}
    deltas: dict[int, int] = {}
    for p in truth["players"]:
        rec = live.get(p["seat"])
        if rec is None or not isinstance(rec.get("result"), (int, float)):
            continue
        d = int(p["result"]) - int(rec["result"])
        if d:
            deltas[p["seat"]] = d
    return deltas


# ――― お客さんの画面に真のアクションだけを見せる（`viewer_api.show_truth`, 2026-10-19）―――

_TRUTH_CACHE: dict[str, tuple[tuple[int, int], dict[int, dict]]] = {}


def load_truth(log_dir: "str | Path", session_id: str) -> dict[int, dict]:
    """`logs/<sid>.ground_truth.json` のハンド（hand_id → 真のアクション）。入力画面が別プロセスで書くので、
    ファイルが変わっていれば読み直す（mtime と size）。無い・読めないときは空。"""
    path = Path(log_dir) / f"{session_id}.ground_truth.json"
    try:
        st = path.stat()
    except OSError:
        return {}
    key = (st.st_mtime_ns, st.st_size)
    cached = _TRUTH_CACHE.get(str(path))
    if cached is not None and cached[0] == key:
        return cached[1]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning("真のアクションを読めません（前に読んだ内容を使います）: %s (%s)", path, e)
        return cached[1] if cached else {}
    hands: dict[int, dict] = {}
    for entry in (data.get("hands") if isinstance(data, dict) else None) or []:
        if isinstance(entry, dict) and isinstance(entry.get("hand_id"), int):
            hands[entry["hand_id"]] = entry
    _TRUTH_CACHE[str(path)] = (key, hands)
    return hands


def truth_view(hands: list[dict], truth: dict[int, dict]) -> list[dict]:
    """真のアクションを保存して終わっているハンドだけを、真のアクションの形にして返す（ほかは外す。オーナー
    2026-10-19: お客さんには真のアクションだけを見せる）。"""
    out = []
    for hand in hands:
        entry = truth.get(hand.get("hand_id"))
        made = materialize_truth(entry, hand) if entry else None
        if made is not None:
            out.append(made)
    return out
