from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ActionRecord:
    """1プレイヤー・1回のアクションを表す。"""

    hand_id: int
    timestamp: str  # ISO 8601 形式
    street: str
    seat: int
    player_name: str
    action: str
    amount: int
    pot_after: int
    stack_after: int
    source: dict  # {"camera": bool, "audio": bool}
    needs_review: bool

    def to_dict(self) -> dict:
        return {
            "hand_id": self.hand_id,
            "timestamp": self.timestamp,
            "street": self.street,
            "seat": self.seat,
            "player_name": self.player_name,
            "action": self.action,
            "amount": self.amount,
            "pot_after": self.pot_after,
            "stack_after": self.stack_after,
            "source": self.source,
            "needs_review": self.needs_review,
        }


@dataclass
class HandSummary:
    """ハンド終了後に確定した情報を表す。"""

    hand_id: int
    session_id: str
    started_at: str  # ISO 8601
    ended_at: str  # ISO 8601
    blinds: dict  # {"sb": int, "bb": int}
    board: list[str]  # ショーダウン時のボードカード（未確定時は空リスト）
    players: list[dict]  # {seat, name, hole_cards, stack_start, stack_end, result}
    pot_total: int
    winner_seat: int
    actions: list[ActionRecord]
    review_required: bool  # いずれかのアクションに needs_review=True があれば True

    def to_dict(self) -> dict:
        return {
            "hand_id": self.hand_id,
            "session_id": self.session_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "blinds": self.blinds,
            "board": self.board,
            "players": self.players,
            "pot_total": self.pot_total,
            "winner_seat": self.winner_seat,
            "actions": [a.to_dict() for a in self.actions],
            "review_required": self.review_required,
        }
