from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ActionRecord:
    """1プレイヤー・1回のアクションを表す。

    監査フィールド（ADR-A G2, action schema 1.0 の optional）は rules-aware 経路のみが埋める。
    None のフィールドは to_dict に出さない（legacy 経路の出力は従来どおり不変）。
    """

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
    # ――― 監査フィールド（ADR-0009 §7 / ADR-A G2。needs_review の理由を逆引き可能にする）―――
    actor_source: Optional[str] = None      # "rfid" | "spoken_seat" | "engine_prior" | "unresolved"
    corrected_from: Optional[str] = None    # 射影で action が変わった場合の修復前 raw ASR action
    reason: Optional[str] = None            # 射影/合成/競合の短い理由（"+区切りで複合）
    asr_confidence: Optional[float] = None  # Whisper 信頼度（欠測は None のまま）
    apply_ok: Optional[bool] = None         # pokerkit が受理したか（False = state 非反映のレコード）

    def to_dict(self) -> dict:
        data = {
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
        if self.actor_source is not None:
            data["actor_source"] = self.actor_source
        if self.corrected_from is not None:
            data["corrected_from"] = self.corrected_from
        if self.reason:
            data["reason"] = self.reason
        if self.asr_confidence is not None:
            data["asr_confidence"] = self.asr_confidence
        if self.apply_ok is not None:
            data["apply_ok"] = self.apply_ok
        return data


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
    # split pot（チョップ）時の授与内訳 [{"seat": int, "amount": int}]（ADR-D S7, additive）。
    # 単独勝者の従来ハンドでは None = 出力に含めない（後方互換）。
    pot_awards: Optional[list] = None

    def to_dict(self) -> dict:
        data = {
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
        if self.pot_awards is not None:
            data["pot_awards"] = self.pot_awards
        return data
