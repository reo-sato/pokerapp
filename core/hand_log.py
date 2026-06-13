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
    source: dict  # {"camera": bool, "audio": bool, "rfid": bool}
    needs_review: bool
    confidence: float = 0.0  # 0.0–1.0 (FR-42: RFID+audio+camera 合意度)

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
            "confidence": self.confidence,
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
    board_source: str  # ボード情報のソース: "rfid" | "ocr" | "manual" | ""
    players: list[dict]  # {seat, name, hole_cards, stack_start, stack_end, result}
    pot_total: int
    winner_seat: int
    actions: list[ActionRecord]
    review_required: bool  # いずれかのアクションに needs_review=True があれば True
    # main/side pot スナップショット [{"amount": int, "eligible_seats": [int,...]}]。
    # rules-aware backend が end_hand 時に算出（legacy は []）。additive（F3 / R5）。
    pots: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "hand_id": self.hand_id,
            "session_id": self.session_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "blinds": self.blinds,
            "board": self.board,
            "board_source": self.board_source,
            "players": self.players,
            "pot_total": self.pot_total,
            "pots": self.pots,
            "winner_seat": self.winner_seat,
            "actions": [a.to_dict() for a in self.actions],
            "review_required": self.review_required,
        }
