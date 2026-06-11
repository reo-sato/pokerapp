from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

from core.constants import STREET_ORDER
from core.engine_types import LegalContext

logger = logging.getLogger(__name__)


class Street(str, Enum):
    PREFLOP = "preflop"
    FLOP = "flop"
    TURN = "turn"
    RIVER = "river"
    SHOWDOWN = "showdown"


@dataclass
class PlayerState:
    seat: int
    name: str
    stack: int
    is_active: bool = True  # フォールドしたら False


class GameStateManager:
    """Phase 1 簡易版: PokerKit不使用。スタック/ポット/ターン管理のみ担当。

    Phase 3 以降で PokerKit ラッパーに差し替える予定のため、外部 I/F は変えない。
    """

    def __init__(
        self,
        players: list[PlayerState],
        sb: int,
        bb: int,
    ) -> None:
        if not players:
            raise ValueError("players must not be empty")
        self._players: dict[int, PlayerState] = {p.seat: p for p in players}
        self._sb = sb
        self._bb = bb
        self._hand_id: int = 0
        self._street: Street = Street.PREFLOP
        self._pot: int = 0
        # is_active な席番号を昇順で保持（フォールドで除外）
        self._active_seats: list[int] = sorted(self._players.keys())
        self._turn_idx: int = 0  # _active_seats 上のインデックス

    # ――― ハンド管理 ―――

    def new_hand(self) -> int:
        """新ハンドを開始し、インクリメントされた hand_id を返す。

        - ポットを 0 にリセット
        - 全席を is_active=True に戻す
        - ストリートを PREFLOP に戻す
        - ラウンドロビンのインデックスを先頭に戻す
        """
        self._hand_id += 1
        self._street = Street.PREFLOP
        self._pot = 0
        self._turn_idx = 0
        for ps in self._players.values():
            ps.is_active = True
        self._active_seats = sorted(self._players.keys())
        logger.info("New hand started: hand_id=%d", self._hand_id)
        return self._hand_id

    def advance_street(self, street: Street) -> None:
        """ストリートを更新する（PREFLOP→FLOP→TURN→RIVER→SHOWDOWN の順方向のみ）。

        逆方向・同一ストリートへの遷移は ValueError を送出する。
        """
        current_idx = STREET_ORDER.index(self._street.value)
        new_idx = STREET_ORDER.index(street.value)
        if new_idx <= current_idx:
            raise ValueError(
                f"Invalid street transition: {self._street.value!r} → {street.value!r}"
            )
        self._street = street
        self._turn_idx = 0  # 新ストリートではターン順をリセット
        logger.debug("Street advanced to %s", street)

    def end_hand(self, winner_seat: int) -> None:
        """ポットを winner_seat のスタックに加算し、ポットを 0 にする。

        # Phase 1: メインポットのみ対象。サイドポットは扱わない。
        # Phase 3 以降で PokerKit の CHIPS_PUSHING/CHIPS_PULLING に差し替える。
        """
        if winner_seat not in self._players:
            raise ValueError(f"Unknown seat: {winner_seat}")
        self._players[winner_seat].stack += self._pot
        logger.info(
            "Hand %d ended. Seat %d wins pot %d. New stack: %d",
            self._hand_id,
            winner_seat,
            self._pot,
            self._players[winner_seat].stack,
        )
        self._pot = 0

    # ――― アクション適用 ―――

    def apply_action(self, seat: int, action: str, amount: int = 0) -> None:
        """アクションをスタック・ポットに反映し、ターンを次のプレイヤーに進める。

        ターン管理方針:
        - このメソッドが常に advance_turn() を担う。呼び出し側で advance_turn() を
          別途呼ぶ必要はない（呼び忘れバグを防ぐため）。
        - fold: _active_seats からの除外処理が実質的なターン進行を兼ねる。
        - それ以外: 処理後に advance_turn() を呼び出す。

        Raises:
            ValueError: 対象席が存在しない / fold 済み席への再アクション /
                        amount が負 / 未知の action
        """
        if seat not in self._players:
            raise ValueError(f"Unknown seat: {seat}")
        ps = self._players[seat]
        if not ps.is_active:
            raise ValueError(f"Seat {seat} has already folded and cannot act")
        action_lower = action.lower()

        if action_lower in ("bet", "raise", "call", "allin"):
            if amount < 0:
                raise ValueError(f"Amount must be non-negative, got {amount}")
            actual = min(amount, ps.stack)  # スタック超過はオールインとして扱う
            ps.stack -= actual
            self._pot += actual
            logger.debug(
                "Seat %d %s %d (stack: %d, pot: %d)",
                seat, action_lower, actual, ps.stack, self._pot,
            )
            self.advance_turn()
        elif action_lower == "fold":
            ps.is_active = False
            if seat in self._active_seats:
                idx = self._active_seats.index(seat)
                self._active_seats.remove(seat)
                # fold した席より後ろを指していたインデックスを補正する。
                # これにより _turn_idx は自然に「fold した席の次」を指す。
                if self._turn_idx > idx:
                    self._turn_idx -= 1
                if self._active_seats:
                    self._turn_idx %= len(self._active_seats)
            logger.debug("Seat %d folded. Active seats: %s", seat, self._active_seats)
            # fold は上記インデックス調整がターン進行を兼ねるため advance_turn() 不要
        elif action_lower == "check":
            self.advance_turn()
        else:
            raise ValueError(f"Unknown action: {action!r} (seat={seat})")

    # ――― ターン管理 ―――

    def get_current_player(self) -> int:
        """現在アクションターンの席番号を返す（is_active な席のみ対象）。

        # Phase 1: BTN/SB/BB の順序は考慮しない単純ラウンドロビン。
        # Phase 3 で PokerKit の actor_index に差し替える。
        """
        if not self._active_seats:
            raise RuntimeError("No active seats remaining")
        return self._active_seats[self._turn_idx % len(self._active_seats)]

    def advance_turn(self) -> int:
        """次の is_active な席に進み、その席番号を返す。

        # Phase 1: 単純ラウンドロビン（BTN・ブラインドの優先順位なし）。
        """
        if not self._active_seats:
            raise RuntimeError("No active seats remaining")
        self._turn_idx = (self._turn_idx + 1) % len(self._active_seats)
        return self._active_seats[self._turn_idx]

    # ――― 照会 ―――

    @property
    def hand_id(self) -> int:
        return self._hand_id

    @property
    def street(self) -> str:
        return self._street.value

    @property
    def pot(self) -> int:
        return self._pot

    def get_stack(self, seat: int) -> int:
        if seat not in self._players:
            raise ValueError(f"Unknown seat: {seat}")
        return self._players[seat].stack

    def get_stacks(self) -> dict[int, int]:
        return {seat: ps.stack for seat, ps in self._players.items()}

    def get_player_name(self, seat: int) -> str:
        if seat not in self._players:
            raise ValueError(f"Unknown seat: {seat}")
        return self._players[seat].name

    def set_player_name(self, seat: int, name: str) -> None:
        """席の表示名を更新する (E3 seat change 用, additive)。ルール状態には影響しない。"""
        if seat not in self._players:
            raise ValueError(f"Unknown seat: {seat}")
        self._players[seat].name = name

    def get_active_seats(self) -> list[int]:
        return list(self._active_seats)

    # ――― rules-aware 境界（additive stub, ADR-0009 §2）―――
    # legacy はポーカールール状態機械を持たないため rules-aware ではない。
    # IntegrationThread は空の legal_context を以て legacy 経路（従来挙動）へ分岐する。

    def legal_context(self) -> LegalContext:
        """legacy は合法手プリオールを持たない（空コンテキスト = rules-aware でない印）。"""
        return LegalContext(
            actor_seat=None, legal_actions=frozenset(),
            amount_to_call=0, min_raise=0, max_raise=0,
        )

    def is_legal_actor(self, seat: int) -> bool:
        return bool(self._active_seats) and seat == self.get_current_player()

    def fold_through(self, until_seat: int, max_folds: int | None = None) -> list[int]:
        raise NotImplementedError("fold_through is not supported by the legacy backend")

    def pots(self) -> list[dict]:
        return []  # legacy はサイドポットを扱わない

    def committed(self, seat: int) -> int:
        return 0  # legacy はストリート別コミット額を追跡しない

    # ――― 手動修正 ―――

    def update_stack(self, seat: int, new_stack: int) -> None:
        """スタック修正ボタン用。"""
        if seat not in self._players:
            raise ValueError(f"Unknown seat: {seat}")
        if new_stack < 0:
            raise ValueError(f"Stack must be non-negative, got {new_stack}")
        self._players[seat].stack = new_stack
        logger.info("Stack updated: seat=%d, new_stack=%d", seat, new_stack)

    def rebuy(self, seat: int, amount: int) -> None:
        """リバイ・アドオン: スタックに amount を加算する。"""
        if seat not in self._players:
            raise ValueError(f"Unknown seat: {seat}")
        if amount <= 0:
            raise ValueError(f"Rebuy amount must be positive, got {amount}")
        self._players[seat].stack += amount
        logger.info("Rebuy: seat=%d, amount=%d, new_stack=%d", seat, amount, self._players[seat].stack)
