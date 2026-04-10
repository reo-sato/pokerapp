from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PlayerState:
    """プレイヤーの席情報と残スタックを表す。

    spec.md v4.0: core/game_state.py から移動。
    GameState.__init__ の players 引数として使用する。
    """

    seat: int
    name: str
    stack: int
    is_active: bool = True  # フォールドしたら False


@dataclass
class ActionRecord:
    """1プレイヤー・1回のアクションを表す。

    spec.md v4.0 追加フィールド:
    - position: ポジション名（"BTN"/"SB"/"BB"/"UTG" 等）
    - actor_confidence: アクター推定確率（0.0–1.0）
    """

    hand_id: int
    timestamp: str   # ISO 8601 形式
    street: str
    seat: int
    player_name: str
    action: str
    amount: int
    pot_after: int
    stack_after: int
    source: dict     # {"audio": bool, "rfid": bool}
    needs_review: bool
    confidence: float = 0.0    # 0.0–1.0 (センサー合意度)
    position: str = ""         # spec.md FR-05d: ポジション名
    actor_confidence: float = 0.0  # spec.md FR-26: アクター推定確率

    def to_dict(self) -> dict:
        return {
            "hand_id": self.hand_id,
            "timestamp": self.timestamp,
            "street": self.street,
            "seat": self.seat,
            "player_name": self.player_name,
            "position": self.position,
            "action": self.action,
            "amount": self.amount,
            "pot_after": self.pot_after,
            "stack_after": self.stack_after,
            "source": self.source,
            "actor_confidence": self.actor_confidence,
            "needs_review": self.needs_review,
            "confidence": self.confidence,
        }


@dataclass
class HandSummary:
    """ハンド終了後に確定した情報を表す。

    spec.md v4.0 追加フィールド:
    - button_seat: ハンド時点のディーラーボタン席番号
    - position_map: 席番号 → ポジション名マッピング
    """

    hand_id: int
    session_id: str
    started_at: str   # ISO 8601
    ended_at: str     # ISO 8601
    blinds: dict      # {"sb": int, "bb": int}
    board: list[str]  # ショーダウン時のボードカード（未確定時は空リスト）
    board_source: str  # ボード情報のソース: "rfid" | "manual" | ""
    players: list[dict]  # {seat, name, hole_cards, stack_start, stack_end, result, folded_street}
    pot_total: int
    winner_seat: int
    actions: list[ActionRecord]
    review_required: bool  # いずれかのアクションに needs_review=True があれば True
    button_seat: int = 0                               # spec.md FR-05h
    position_map: dict = field(default_factory=dict)   # spec.md FR-05h

    def to_dict(self) -> dict:
        return {
            "hand_id": self.hand_id,
            "session_id": self.session_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "blinds": self.blinds,
            "button_seat": self.button_seat,
            "position_map": self.position_map,
            "board": self.board,
            "board_source": self.board_source,
            "players": self.players,
            "pot_total": self.pot_total,
            "winner_seat": self.winner_seat,
            "actions": [a.to_dict() for a in self.actions],
            "review_required": self.review_required,
        }
