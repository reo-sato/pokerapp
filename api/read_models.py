"""api/read_models.py

M1 viewer API の read model (ADR-0013, docs/contracts/viewer-api.md)。

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
    summaries: list[dict] = []
    for session in session_repo.list_sessions():
        hands_played = sum(
            1
            for hand_id in session_repo.list_hand_ids(session.session_id)
            if player_id
            in session_repo.resolve_seat_map_for_hand(session.session_id, hand_id).values()
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
) -> list[dict]:
    """player が着席していた hand の HandSummary dict を hand_id 昇順で返す。

    seat assignment はあるが hand log に対応 hand が無いものは除外する。
    unknown session は SessionNotFoundError（session_repo 経由）。
    """
    seated_hand_ids = {
        hand_id
        for hand_id in session_repo.list_hand_ids(session_id)
        if player_id in session_repo.resolve_seat_map_for_hand(session_id, hand_id).values()
    }
    if not seated_hand_ids:
        return []
    log = _load_hand_log(log_dir, session_id)
    if log is None:
        return []
    hands = [h for h in log.get("hands", []) if h.get("hand_id") in seated_hand_ids]
    return sorted(hands, key=lambda h: h["hand_id"])


def get_hand(session_id: str, hand_id: int, log_dir: str | Path) -> dict:
    """hand log から HandSummary dict を 1 件返す。

    session レイヤ未登録の legacy session_id（timestamp 形式）でも log が存在すれば返す
    （viewer-api.md 備考）。不在は HandNotFoundError（code: not_found）。
    """
    log = _load_hand_log(log_dir, session_id)
    if log is not None:
        for hand in log.get("hands", []):
            if hand.get("hand_id") == hand_id:
                return hand
    raise HandNotFoundError(
        f"hand_id={hand_id} は session_id={session_id} の hand log に存在しません。"
    )
