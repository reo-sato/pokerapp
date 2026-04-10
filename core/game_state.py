from __future__ import annotations

import logging

from core.constants import STREET_ORDER, Street
from core.hand_log import PlayerState

logger = logging.getLogger(__name__)


class GameStateManager:
    """Phase 1 簡易版: PokerKit不使用。スタック/ポット/ターン管理のみ担当。

    spec.md v4.0 以降は GameState クラスを使用すること。
    後方互換性のためこのクラスは残している。
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
        """新ハンドを開始し、インクリメントされた hand_id を返す。"""
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
        """ストリートを更新する（PREFLOP→FLOP→TURN→RIVER→SHOWDOWN の順方向のみ）。"""
        current_idx = STREET_ORDER.index(self._street.value)
        new_idx = STREET_ORDER.index(street.value)
        if new_idx <= current_idx:
            raise ValueError(
                f"Invalid street transition: {self._street.value!r} → {street.value!r}"
            )
        self._street = street
        self._turn_idx = 0
        logger.debug("Street advanced to %s", street)

    def end_hand(self, winner_seat: int) -> None:
        """ポットを winner_seat のスタックに加算し、ポットを 0 にする。"""
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
        """アクションをスタック・ポットに反映し、ターンを次のプレイヤーに進める。"""
        if seat not in self._players:
            raise ValueError(f"Unknown seat: {seat}")
        ps = self._players[seat]
        if not ps.is_active:
            raise ValueError(f"Seat {seat} has already folded and cannot act")
        action_lower = action.lower()

        if action_lower in ("bet", "raise", "call", "allin"):
            if amount < 0:
                raise ValueError(f"Amount must be non-negative, got {amount}")
            actual = min(amount, ps.stack)
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
                if self._turn_idx > idx:
                    self._turn_idx -= 1
                if self._active_seats:
                    self._turn_idx %= len(self._active_seats)
            logger.debug("Seat %d folded. Active seats: %s", seat, self._active_seats)
        elif action_lower == "check":
            self.advance_turn()
        else:
            raise ValueError(f"Unknown action: {action!r} (seat={seat})")

    # ――― ターン管理 ―――

    def get_current_player(self) -> int:
        """現在アクションターンの席番号を返す（is_active な席のみ対象）。"""
        if not self._active_seats:
            raise RuntimeError("No active seats remaining")
        return self._active_seats[self._turn_idx % len(self._active_seats)]

    def advance_turn(self) -> int:
        """次の is_active な席に進み、その席番号を返す。"""
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

    def get_active_seats(self) -> list[int]:
        return list(self._active_seats)

    # ――― 手動修正 ―――

    def update_stack(self, seat: int, new_stack: int) -> None:
        if seat not in self._players:
            raise ValueError(f"Unknown seat: {seat}")
        if new_stack < 0:
            raise ValueError(f"Stack must be non-negative, got {new_stack}")
        self._players[seat].stack = new_stack
        logger.info("Stack updated: seat=%d, new_stack=%d", seat, new_stack)

    def rebuy(self, seat: int, amount: int) -> None:
        if seat not in self._players:
            raise ValueError(f"Unknown seat: {seat}")
        if amount <= 0:
            raise ValueError(f"Rebuy amount must be positive, got {amount}")
        self._players[seat].stack += amount
        logger.info("Rebuy: seat=%d, amount=%d, new_stack=%d", seat, amount, self._players[seat].stack)


class GameState:
    """spec.md v4.0: ディーラーボタン管理・ポジション算出対応のゲーム状態クラス。

    Phase 1 以降はこちらを使用する。GameStateManager は後方互換用に残す。
    """

    # ── 状態フィールド（spec FR-35） ──
    all_seats: list[int]           # 全席（バスト含む）
    busted_seats: set[int]         # バストアウト席
    folded_seats: set[int]         # 現ハンドのフォールド済み席
    all_in_seats: set[int]         # オールイン済み席
    button_seat: int               # 現在のディーラーボタン席番号
    position_map: dict[int, str]   # {席番号: "BTN"/"SB"/"BB"/"UTG"...}
    turn_order: list[int]          # 現ストリートのアクション順（フォールド/AI スキップ済み）
    current_turn_idx: int          # turn_order 上の現在手番インデックス
    last_aggressor: int | None     # 最後に bet/raise した席
    call_amount: int               # 現在のコール額（本ストリートの最大ベット）
    invested: dict[int, int]       # 各席の現ストリート投資額
    board_cards: list[str]         # ボードカード（"Ah","Kd" 等）
    last_mentioned_seat: int | None  # 音声で直近に言及された席番号

    def __init__(
        self,
        players: list[PlayerState],
        sb: int,
        bb: int,
        button_seat: int,
    ) -> None: ...

    # ── ディーラーボタン管理（spec FR-05b–05f） ──

    def advance_button(self) -> None:
        """ハンド終了時に呼び出す。ボタンを次のアクティブ席へ1席進める。"""
        ...

    def build_position_map(self) -> dict[int, str]:
        """button_seat から全席のポジション名を算出して返す。"""
        ...

    def build_turn_order(self, street: Street) -> list[int]:
        """ストリートに応じたアクション順リストを返す。
        preflop: UTG から開始。postflop: SB から開始。
        フォールド/オールイン済み席はスキップ。
        """
        ...

    def current_turn_seat(self) -> int:
        """現在手番の席番号を返す。"""
        ...

    def _sb_seat(self) -> int: ...
    def _bb_seat(self) -> int: ...

    # ── ハンド管理 ──

    def new_hand(self) -> int:
        """新ハンドを開始し hand_id を返す。advance_button() を呼び出す。"""
        ...

    def advance_street(self, street: Street) -> None: ...

    def end_hand(self, winner_seat: int) -> None: ...

    # ── アクション適用 ──

    def apply_action(self, seat: int, action: str, amount: int = 0) -> None: ...

    # ── 照会 ──

    @property
    def hand_id(self) -> int: ...

    @property
    def street(self) -> str: ...

    @property
    def pot(self) -> int: ...

    def get_stack(self, seat: int) -> int: ...
    def get_stacks(self) -> dict[int, int]: ...
    def get_active_seats(self) -> list[int]: ...

    # ── 手動修正 ──

    def update_stack(self, seat: int, new_stack: int) -> None: ...
    def rebuy(self, seat: int, amount: int) -> None: ...
    def bust_out(self, seat: int) -> None:
        """席をバストアウトとしてマークし busted_seats に追加する。"""
        ...
