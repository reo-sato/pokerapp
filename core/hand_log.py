from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal, Optional

# ────────────────────────────────────────────────────────────────────────────
# 終局表現の Literal 型 (Phase 1)
# ────────────────────────────────────────────────────────────────────────────

ResolutionStatus = Literal["final", "provisional", "incomplete"]
"""ハンドが PHH 出力対象になるかどうかの 3 値。
- final: 終局確定、PHH 出力対象
- provisional: 仮確定、後で finalizer 再評価予定
- incomplete: 情報不足で finalization 不能
"""

ResolutionType = Literal[
    "fold_win",
    "showdown",
    "showdown_split",
    "sidepot_showdown",
    "legacy_winner_finalize",
]
"""ハンドの終局タイプ。

- fold_win:                live player が 1 人に絞れたケース
- showdown:                board + hole cards から rank 評価で 1 人勝者確定
- showdown_split:          同 rank で payouts を均等分配
- sidepot_showdown:        all-in を含む複数 pot の決済
- legacy_winner_finalize:  Phase 0–M3 の _finalize_hand(winner_seat) 経由で閉じた hand を
                           識別する migration marker。Phase 2 finalizer が再評価して
                           上記 4 種のいずれかへ昇格させる前提。canonical な
                           resolution type の集合に最終的に含まれるべき値ではない。
"""

PotType = Literal["main", "side"]
"""pot の種別。Phase 1 では未使用、Phase 2 で side pot 計算時に活用。"""

RevealedCardSource = Literal["rfid", "manual", "derived"]
"""hole cards の出所。derived は他の観測から逆算した場合 (Phase 2 以降)。"""


# ────────────────────────────────────────────────────────────────────────────
# Settlement 関連 dataclass (Phase 1)
#
# 暫定的に hand_log.py に同居している。Phase 2 で core/settlement.py や
# core/showdown_tracker.py のロジックが成熟したら、これらの dataclass の置き場所も
# 再評価する (ロジック側に移動して hand_log から re-export する選択肢もある)。
# ────────────────────────────────────────────────────────────────────────────


@dataclass
class RevealedHand:
    """showdown 時に見えた seat 別 hole cards。

    canonical な内部表現。muck 後でも finalization 用に保持する。
    JSON 出力時は ``HandSummary.showdown_revealed_cards`` (簡略表現) へ投影する。
    """

    seat: int
    cards: list[str]
    source: RevealedCardSource
    observed_at: Optional[float] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PotSettlement:
    """1 つの pot (main / side) の決済結果。

    eligible_seats: その pot を争う資格があった seat の集合。
    winning_seats: その中で実際に勝った seat。
    payouts: 各 seat への払出 (odd chip 配分も含む)。sum(payouts.values()) == amount。
    """

    amount: int
    eligible_seats: list[int]
    winning_seats: list[int]
    payouts: dict[int, int]
    pot_type: PotType = "main"

    def to_dict(self) -> dict:
        return asdict(self)


# ────────────────────────────────────────────────────────────────────────────
# Action / Hand summary
# ────────────────────────────────────────────────────────────────────────────


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
    """ハンド終了後に確定した情報を表す。

    Phase 1 から settlement 中心の field 群が追加された:
    ``resolution_status`` / ``resolution_type`` / ``seat_payouts`` /
    ``showdown_revealed_cards`` / ``pots``。

    ``winner_seat: int`` は Phase 1 では後方互換のための compatibility field として
    required のまま温存される。canonical な終局表現は
    ``seat_payouts`` / ``pots`` / ``resolution_status`` / ``resolution_type`` 側に移行する。
    """

    hand_id: int
    session_id: str
    started_at: str  # ISO 8601
    ended_at: str  # ISO 8601
    blinds: dict  # {"sb": int, "bb": int}
    board: list[str]  # ショーダウン時のボードカード（未確定時は空リスト）
    board_source: str  # ボード情報のソース: "rfid" | "ocr" | "manual" | ""
    players: list[dict]  # {seat, name, hole_cards, stack_start, stack_end, result}
    pot_total: int
    """終局時の **canonical pot total**。

    Phase 2-B 以降は ``betting_state.player_contrib_hand`` の総和 (SB/BB 含む全
    seat の hand 累積投入額) から算出する。``HandSummary`` の他フィールド
    (``pots``, ``seat_payouts``) との不変量:
      ``pot_total == sum(rec.amount for rec in betting_state.action_history)``
      ``pot_total == sum(p.amount for p in pots)`` (settlement 成功時)
      ``pot_total >= sum(seat_payouts.values())`` (settlement 成功時、rake 考慮なら等号は崩れる)

    Phase 1 では bet/raise/call/allin の action.amount を sum していたため
    blind-only + fold hand で 0 になるバグがあった (Phase 2-B で解消)。

    UI 表示用の途中経過 pot (street ごとの累積) や replay 中の動的 pot 表示は
    別管理。本 field は **終局時の固定値**として扱うこと。
    """

    winner_seat: int
    """**Compatibility field**。canonical な終局表現は ``seat_payouts`` / ``pots``。

    本 field の縮約ルール (Phase 2-B `_pick_primary_winner` 仕様):
      1. ``seat_payouts`` が非空 → **最大 payout の seat** (tie 時は **最小 seat 番号**)
      2. ``seat_payouts`` が空 (incomplete 等) → ``winner_seat_hint`` (音声 WINNER) を採用
      3. hint も無い → ``live_seats`` の最低 seat 番号
      4. live も無い → ``active_seats`` の最低 seat 番号
      5. それも無い → ``0`` (退化、実運用では到達しない)

    Phase 1 までは required ``int`` のまま温存。Phase 3+ で ``Optional[int]`` 化
    または ``primary_winner_seat`` へのリネームを検討。
    """
    actions: list[ActionRecord]
    review_required: bool  # いずれかのアクションに needs_review=True があれば True
    folded_seats: list[int] = field(default_factory=list)   # フォールドした席番号（順序付き）
    all_in_seats: list[int] = field(default_factory=list)   # オールインした席番号（順序付き）

    # ── Phase 1 で追加した settlement 中心の field 群 ───────────────────────
    # default は直接 HandSummary(...) を呼ぶ純粋な dataclass テスト用の値。
    # 通常の生成経路は integration/engine.py の _finalize_hand なので、
    # そこで明示的に値が立つ。
    resolution_status: ResolutionStatus = "final"
    """ハンドが final か。_finalize_hand 経由でも "final" を維持 (既存挙動)。"""

    resolution_type: Optional[ResolutionType] = None
    """終局タイプ。_finalize_hand 経由では "legacy_winner_finalize" を明示設定。
    default=None は dataclass の純粋構築用 (テスト・ad-hoc 利用) であり、通常の
    engine 経路では legacy_winner_finalize が立つ。Phase 2 finalizer が
    fold_win / showdown / showdown_split / sidepot_showdown のいずれかへ昇格させる。"""

    seat_payouts: dict[int, int] = field(default_factory=dict)
    """seat 別の正味払出。_finalize_hand 経由では {winner_seat: pot_total}。
    Phase 2 finalizer は ``pots`` から sum して整合させる。"""

    showdown_revealed_cards: dict[int, list[str]] = field(default_factory=dict)
    """JSON 出力用の簡略表現 (seat -> [card, ...])。canonical は内部の RevealedHand。
    Phase 2 で ShowdownTracker から投影される予定 (現状は空)。"""

    pots: list[PotSettlement] = field(default_factory=list)
    """各 pot (main / side) の決済結果。Phase 1 では意図的に空のまま残す
    (fake な eligible_seats を入れると Phase 2 で side pot を正しく計算した
    ときに矛盾する)。Phase 2 settlement.compute_pot_settlements() が埋める。"""

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
            "winner_seat": self.winner_seat,
            "actions": [a.to_dict() for a in self.actions],
            "review_required": self.review_required,
            "folded_seats": self.folded_seats,
            "all_in_seats": self.all_in_seats,
            # Phase 1 で追加した settlement 中心の field 群
            "resolution_status": self.resolution_status,
            "resolution_type": self.resolution_type,
            "seat_payouts": self.seat_payouts,
            "showdown_revealed_cards": self.showdown_revealed_cards,
            "pots": [p.to_dict() for p in self.pots],
        }
