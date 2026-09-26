from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ActionRecord:
    """1プレイヤー・1回のアクションを表す。

    監査フィールド（ADR-0047 G2, action schema の optional）は rules-aware 経路のみが埋める。
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
    # そのアクションを行った席の **ポジション名**（BTN/SB/BB/UTG…, 仕様 §6.2, ISSUE-0032）。
    # ボタンを持たない backend（legacy）では空文字。additive。
    position: str = ""
    # ――― 監査フィールド（ADR-0009 §7 / ADR-0047 G2。needs_review の理由を逆引き可能にする）―――
    # actor_source: "spoken_seat" | "spoken_position" | "engine_prior" | "unresolved"。
    # "rfid" は旧記録にのみ現れる（RFID の検出は actor の証拠にしない = ISSUE-0033 / ADR-0056）。
    actor_source: Optional[str] = None
    corrected_from: Optional[str] = None    # 射影で action が変わった場合の修復前 raw ASR action
    reason: Optional[str] = None            # 射影/合成/競合の短い理由（"+区切りで複合）
    asr_confidence: Optional[float] = None  # Whisper 信頼度（欠測は None のまま）
    apply_ok: Optional[bool] = None         # pokerkit が受理したか（False = state 非反映のレコード）
    # そのアクションになった発話（Whisper の書き起こし、または CLI で打った読み上げ文）。
    # 合成した fold には無い。音声テストで「何と聞こえて何になったか」を追うため（ADR-0060, additive）。
    raw_text: Optional[str] = None

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
            "position": self.position,
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
        if self.raw_text:
            data["raw_text"] = self.raw_text
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
    # このハンドの **ディーラーボタンの席**（仕様 FR-05b, ISSUE-0032）。ボタンを持たない
    # backend（legacy）では None。`position_map` は seat → ポジション名（仕様 §6.1）。additive。
    button_seat: Optional[int] = None
    position_map: dict = field(default_factory=dict)
    # ボード各枚の **配布時刻**（RFID が最初にそのカードを検出した時刻）。
    # [{"index": 1..5, "card": "Qc", "dealt_at": ISO8601}]。index 昇順。
    # ターン/リバーの配布時刻はベッティングラウンドの区切りとして**アクションの時刻と対応**するため
    # 記録する（音声の時系列とハンド履歴を突き合わせて再生するため, ADR-0055）。
    # フロップは 3 枚の最小値がラウンドの開始。RFID 以外のソースでは空リスト。additive。
    board_timeline: list = field(default_factory=list)
    # split pot（チョップ）時の授与内訳 [{"seat": int, "amount": int}]（ADR-0050 S7, additive）。
    # 単独勝者の従来ハンドでは None = 出力に含めない（後方互換）。ショーダウンを手札で判定して
    # 2 人以上に配ったとき（引き分け・side pot の勝者が別）も入る（ADR-0062）。
    pot_awards: Optional[list] = None
    # 勝者を自動で決めたときの決まり方（ADR-0062, additive）: "fold"（ほかが全員フォールド / マック）|
    # "cards"（ショーダウンを RFID の手札とボードで判定）| "estimated"（決まらないまま次の手札が配られた
    # = 仮, 要確認）。ディーラーの宣言（`w` / 「ウィナー」）で決めたハンドは None = 出力に含めない。
    winner_source: Optional[str] = None
    # ショーダウンで見せた手札の役 [{"seat", "hole_cards", "hand", "best"}]（winner_source="cards" のとき）。
    showdown: Optional[list] = None
    # ディーラーが言った勝った役の名前（pokerkit の役名, 2026-09-26 additive）。言わなければ None = 出力に
    # 含めない。判定と違えば review_required。winner_source="announced" = 役名から勝者を決めた（要確認）。
    announced_hand: Optional[str] = None

    def to_dict(self) -> dict:
        data = {
            "hand_id": self.hand_id,
            "session_id": self.session_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "blinds": self.blinds,
            "board": self.board,
            "board_source": self.board_source,
            "board_timeline": self.board_timeline,
            "button_seat": self.button_seat,
            "position_map": {str(k): v for k, v in self.position_map.items()},
            "players": self.players,
            "pot_total": self.pot_total,
            "pots": self.pots,
            "winner_seat": self.winner_seat,
            "actions": [a.to_dict() for a in self.actions],
            "review_required": self.review_required,
        }
        if self.pot_awards is not None:
            data["pot_awards"] = self.pot_awards
        if self.winner_source is not None:
            data["winner_source"] = self.winner_source
        if self.showdown is not None:
            data["showdown"] = self.showdown
        if self.announced_hand is not None:
            data["announced_hand"] = self.announced_hand
        return data
