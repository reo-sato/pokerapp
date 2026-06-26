"""api/read_models.py

viewer API の read model (ADR-0017, docs/contracts/viewer-api.md)。

「player X のハンド」は sessions.json の seat_assignment (ADR-0008 の source of truth)
から導出し、hand log (logs/{session_id}.json) を (session_id, hand_id) で join する。
hand log 側 players[].player_id は best-effort であり帰属判定に使わない。
seat assignment はあるが hand log が無い hand (E3 前 / log 欠落) は静かに除外する。

fastapi に依存しない純関数群 (HTTP なしで単体テスト可能)。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from core.ground_truth import hand_has_needs_review
from core.ground_truth_repository import GroundTruthRepository
from core.hand_correction import apply_hand_corrections
from core.hand_correction_repository import HandCorrectionRepository
from core.ledger_repository import LedgerRepository
from core.session_repository import SessionRepository

logger = logging.getLogger(__name__)


class HandNotFoundError(Exception):
    """指定 (session_id, hand_id) の hand log が存在しない（error code: not_found）。"""


def _load_hand_log(log_dir: str | Path, session_id: str) -> dict | None:
    """logs/{session_id}.json を読む。不在・破損は None（gracefully-empty）。"""
    path = Path(log_dir) / f"{session_id}.json"
    if not path.exists():
        return None
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Could not load hand log %s (%s), treating as absent.", path, e)
        return None


def list_player_sessions(player_id: str, session_repo: SessionRepository) -> list[dict]:
    """player が 1 hand 以上着席した session の player_session_summary を返す（作成順）。

    形は docs/contracts/schemas/player_session_summary.schema.json (0.x)。
    """
    # merge を考慮し、survivor + 全 absorbed の equivalence class で seat 突合する（ADR-0030 D2）。
    cls = session_repo.player_repo.equivalence_class(player_id)
    summaries: list[dict] = []
    for session in session_repo.list_sessions():
        hands_played = sum(
            1
            for hand_id in session_repo.list_hand_ids(session.session_id)
            if cls & set(
                session_repo.resolve_seat_map_for_hand(session.session_id, hand_id).values()
            )
        )
        if hands_played == 0:
            continue
        summary = session.to_dict()
        summary["hands_played"] = hands_played
        summaries.append(summary)
    return summaries


def list_player_hands(
    player_id: str,
    session_id: str,
    session_repo: SessionRepository,
    log_dir: str | Path,
    correction_repo: "HandCorrectionRepository | None" = None,
) -> list[dict]:
    """player が着席していた hand の HandSummary dict を hand_id 昇順で返す。

    seat assignment はあるが hand log に対応 hand が無いものは除外する。
    `correction_repo` があれば訂正オーバーレイを適用した訂正済みビューを返す（ADR-0036）。
    unknown session は SessionNotFoundError（session_repo 経由）。
    """
    cls = session_repo.player_repo.equivalence_class(player_id)
    seated_hand_ids = {
        hand_id
        for hand_id in session_repo.list_hand_ids(session_id)
        if cls & set(session_repo.resolve_seat_map_for_hand(session_id, hand_id).values())
    }
    if not seated_hand_ids:
        return []
    log = _load_hand_log(log_dir, session_id)
    if log is None:
        return []
    hands = [h for h in log.get("hands", []) if h.get("hand_id") in seated_hand_ids]
    hands = sorted(hands, key=lambda h: h["hand_id"])
    if correction_repo is not None:
        hands = [
            apply_hand_corrections(h, correction_repo.list_for_hand(session_id, h["hand_id"]))
            for h in hands
        ]
    return hands


def get_player_session_ledger(
    player_id: str, session_id: str, ledger_repo: LedgerRepository
) -> dict:
    """player の session 会計参照（viewer API, ADR-0017）: entries + 中間集計。

    summary は verify-v1 ledger（ADR-0016）の settlement 由来の中間集計。totals は
    `compute_settlement` を当該 player に絞った値。確定状態は `list_settlements`（確定行のみ）で
    判定し、`settled` / `payment_status` / `settled_at` を additive に載せる（S4 mobile 表示）。
    field は SessionSettlement の名前に揃える（cash_in_total / order_total / entry_fee /
    point_spent_total / point_credited_total / net_due_to_store）。
    **注**: speculative 行は `settled_at` が常に埋まる（wall-clock）ため、確定/未確定の判定は
    `list_settlements` を使う（compute_settlement の settled_at では判定しない）。
    unknown session は SessionNotFoundError（ledger_repo 経由で透過）。
    """
    # settlement 行は survivor の canonical id でキーされる（ADR-0030 D2）ので query も解決する。
    canon = ledger_repo.player_repo.resolve_canonical(player_id)
    spec = next(
        (r for r in ledger_repo.compute_settlement(session_id) if r.player_id == canon),
        None,
    )
    committed = next(
        (s for s in ledger_repo.list_settlements(session_id) if s.player_id == canon),
        None,
    )
    base = committed or spec
    summary = {
        "cash_in_total": base.cash_in_total if base else 0,
        "order_total": base.order_total if base else 0,
        "entry_fee": base.entry_fee if base else 0,
        "point_spent_total": base.point_spent_total if base else 0,
        "point_credited_total": base.point_credited_total if base else 0,
        "net_due_to_store": base.net_due_to_store if base else 0,
        # 確定状態（committed のときのみ意味を持つ。未確定は settled=false）。
        "settled": committed is not None,
        # payment_status は committed 行由来で partial を取り得る（ADR-0023）。
        "payment_status": committed.payment_status if committed else None,
        "settled_at": committed.settled_at if committed else None,
        # 受領累計額（committed のみ意味を持つ。未確定は 0, ADR-0023）。
        "paid_amount": committed.paid_amount if committed else 0,
    }
    return {
        "entries": [e.to_dict() for e in ledger_repo.list_entries(session_id, player_id)],
        "summary": summary,
    }


def get_hand(
    session_id: str, hand_id: int, log_dir: str | Path,
    correction_repo: "HandCorrectionRepository | None" = None,
) -> dict:
    """hand log から HandSummary dict を 1 件返す。

    `correction_repo` があれば訂正オーバーレイを適用した訂正済みビューを返す（ADR-0036）。
    session レイヤ未登録の legacy session_id（timestamp 形式）でも log が存在すれば返す
    （viewer-api.md 備考）。不在は HandNotFoundError（code: not_found）。
    """
    log = _load_hand_log(log_dir, session_id)
    if log is not None:
        for hand in log.get("hands", []):
            if hand.get("hand_id") == hand_id:
                if correction_repo is not None:
                    return apply_hand_corrections(
                        hand, correction_repo.list_for_hand(session_id, hand_id)
                    )
                return hand
    raise HandNotFoundError(
        f"hand_id={hand_id} は session_id={session_id} の hand log に存在しません。"
    )


def list_measurement_rows(
    session_id: str,
    log_dir: str | Path,
    ground_truth_repo: GroundTruthRepository,
    correction_repo: "HandCorrectionRepository | None" = None,
) -> list[dict]:
    """ADR-0043: 計測タブの一覧行を返す。

    各 captured hand について `hand_id` / `winner_seat` / `winner_result` /
    `review_required` / `has_needs_review` / `ground_truth`（既存の GT メタ or None）を
    返す。captured 値は訂正オーバーレイ適用後（ADR-0036）。session 不在 / log 不在は空 list。
    """
    log = _load_hand_log(log_dir, session_id)
    if log is None:
        return []
    rows: list[dict] = []
    for raw in sorted(log.get("hands") or [], key=lambda h: h.get("hand_id", 0)):
        hand_id = raw.get("hand_id")
        if not isinstance(hand_id, int):
            continue
        if correction_repo is not None:
            hand = apply_hand_corrections(
                raw, correction_repo.list_for_hand(session_id, hand_id)
            )
        else:
            hand = raw
        winner_seat = hand.get("winner_seat")
        winner_result: "int | None" = None
        if winner_seat is not None:
            for p in hand.get("players") or []:
                if p.get("seat") == winner_seat:
                    result = p.get("result")
                    if isinstance(result, int):
                        winner_result = result
                    break
        gt = ground_truth_repo.get(session_id, hand_id)
        rows.append({
            "hand_id": hand_id,
            "winner_seat": winner_seat,
            "winner_result": winner_result,
            "review_required": bool(hand.get("review_required")),
            "has_needs_review": hand_has_needs_review(hand),
            "ground_truth": (
                {
                    "annotator": gt.annotator,
                    "annotated_at": gt.annotated_at,
                    "source": gt.source,
                }
                if gt is not None
                else None
            ),
        })
    return rows
