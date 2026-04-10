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


# プレイヤー数別ポジション名テーブル（FR-05c）
_POSITION_NAMES: dict[int, list[str]] = {
    2: ["BTN/SB", "BB"],
    3: ["BTN", "SB", "BB"],
    4: ["BTN", "SB", "BB", "CO"],
    5: ["BTN", "SB", "BB", "UTG", "CO"],
    6: ["BTN", "SB", "BB", "UTG", "HJ", "CO"],
    7: ["BTN", "SB", "BB", "UTG", "LJ", "HJ", "CO"],
    8: ["BTN", "SB", "BB", "UTG", "UTG+1", "LJ", "HJ", "CO"],
    9: ["BTN", "SB", "BB", "UTG", "UTG+1", "UTG+2", "LJ", "HJ", "CO"],
}


class GameState:
    """spec.md v4.0: ディーラーボタン管理・ポジション算出対応のゲーム状態クラス。

    Phase 1 以降はこちらを使用する。GameStateManager は後方互換用に残す。
    """

    def __init__(
        self,
        players: list[PlayerState],
        sb: int,
        bb: int,
        button_seat: int,
    ) -> None:
        if not players:
            raise ValueError("players must not be empty")
        self.all_seats: list[int] = sorted(p.seat for p in players)
        self._stacks: dict[int, int] = {p.seat: p.stack for p in players}
        self._sb = sb
        self._bb = bb
        self.button_seat: int = button_seat
        self.busted_seats: set[int] = set()
        self.folded_seats: set[int] = set()
        self.all_in_seats: set[int] = set()
        self.position_map: dict[int, str] = {}
        self.turn_order: list[int] = []
        self.current_turn_idx: int = 0
        self.last_aggressor: int | None = None
        self.call_amount: int = 0
        self.invested: dict[int, int] = {}
        self.board_cards: list[str] = []
        self.last_mentioned_seat: int | None = None
        self._hand_id: int = 0
        self._street: Street = Street.PREFLOP
        self._pot: int = 0

    # ── ディーラーボタン管理（spec FR-05b–05f） ──

    def advance_button(self) -> None:
        """ハンド終了時に呼び出す。ボタンを次のアクティブ席へ1席進める（FR-05b）。"""
        active = sorted(s for s in self.all_seats if s not in self.busted_seats)
        if len(active) < 2:
            return
        try:
            idx = active.index(self.button_seat)
        except ValueError:
            idx = 0
        self.button_seat = active[(idx + 1) % len(active)]

    def build_position_map(self) -> dict[int, str]:
        """button_seat から全席のポジション名を算出して返す（FR-05c）。"""
        active = [s for s in self.all_seats if s not in self.busted_seats]
        n = len(active)
        if n == 0:
            return {}
        names = _POSITION_NAMES.get(n, _POSITION_NAMES[9])
        try:
            btn_idx = active.index(self.button_seat)
        except ValueError:
            btn_idx = 0
        # btn_first: [BTN, SB, BB, UTG, ...]
        ordered = active[btn_idx:] + active[:btn_idx]
        return {ordered[i]: names[i] for i in range(min(len(ordered), len(names)))}

    def _sb_seat(self) -> int:
        """SB の席番号を返す。HU の場合 BTN = SB。"""
        active = [s for s in self.all_seats if s not in self.busted_seats]
        n = len(active)
        try:
            btn_idx = active.index(self.button_seat)
        except ValueError:
            btn_idx = 0
        if n == 2:
            return active[btn_idx]  # HU: BTN = SB
        return active[(btn_idx + 1) % n]

    def _bb_seat(self) -> int:
        """BB の席番号を返す。"""
        active = [s for s in self.all_seats if s not in self.busted_seats]
        n = len(active)
        try:
            btn_idx = active.index(self.button_seat)
        except ValueError:
            btn_idx = 0
        if n == 2:
            return active[(btn_idx + 1) % n]
        return active[(btn_idx + 2) % n]

    def build_turn_order(self, street: Street) -> list[int]:
        """ストリートに応じたアクション順リストを返す（FR-05d）。

        preflop: UTG から開始（n=2 は BTN/SB から）。
        postflop: SB から開始（n=2 は BB から）。
        フォールド/オールイン済み席はスキップ。
        """
        active = [s for s in self.all_seats if s not in self.busted_seats]
        n = len(active)
        if n == 0:
            return []
        try:
            btn_idx = active.index(self.button_seat)
        except ValueError:
            btn_idx = 0
        # btn_first: [BTN, SB, BB, UTG, ...]
        btn_first = active[btn_idx:] + active[:btn_idx]

        if street == Street.PREFLOP:
            # UTG = index 3 for n>=3; BTN/SB = index 0 for n==2
            start_idx = 3 % n if n >= 3 else 0
        else:
            # SB = index 1 for n>=3; BB = index 1 for n==2 (BB acts first postflop in HU)
            start_idx = 1

        ordered = btn_first[start_idx:] + btn_first[:start_idx]
        return [s for s in ordered if s not in self.folded_seats and s not in self.all_in_seats]

    def current_turn_seat(self) -> int:
        """現在手番の席番号を返す。"""
        if not self.turn_order:
            raise RuntimeError("No seats in turn order")
        return self.turn_order[self.current_turn_idx % len(self.turn_order)]

    def _advance_turn(self) -> None:
        if not self.turn_order:
            return
        self.current_turn_idx = (self.current_turn_idx + 1) % len(self.turn_order)

    # ── ハンド管理 ──

    def new_hand(self) -> int:
        """新ハンドを開始し hand_id を返す。2ハンド目以降は advance_button() を呼ぶ。"""
        if self._hand_id > 0:
            self.advance_button()
        self._hand_id += 1
        self._street = Street.PREFLOP
        self._pot = 0
        self.folded_seats = set()
        self.all_in_seats = set()
        self.call_amount = 0
        self.invested = {s: 0 for s in self.all_seats if s not in self.busted_seats}
        self.last_aggressor = None
        self.board_cards = []
        self.last_mentioned_seat = None
        self.position_map = self.build_position_map()
        self.turn_order = self.build_turn_order(Street.PREFLOP)
        self.current_turn_idx = 0
        logger.info("New hand started: hand_id=%d, button_seat=%d", self._hand_id, self.button_seat)
        return self._hand_id

    def advance_street(self, street: Street) -> None:
        """ストリートを更新し turn_order を再計算する。"""
        current_idx = STREET_ORDER.index(self._street.value)
        new_idx = STREET_ORDER.index(street.value)
        if new_idx <= current_idx:
            raise ValueError(
                f"Invalid street transition: {self._street.value!r} → {street.value!r}"
            )
        self._street = street
        self.call_amount = 0
        self.invested = {
            s: 0 for s in self.all_seats
            if s not in self.busted_seats and s not in self.folded_seats
        }
        self.turn_order = self.build_turn_order(street)
        self.current_turn_idx = 0
        logger.debug("Street advanced to %s", street)

    def end_hand(self, winner_seat: int) -> None:
        """ポットを winner_seat のスタックに加算し、ポットを 0 にする。"""
        if winner_seat not in self._stacks:
            raise ValueError(f"Unknown seat: {winner_seat}")
        self._stacks[winner_seat] += self._pot
        logger.info(
            "Hand %d ended. Seat %d wins pot %d. New stack: %d",
            self._hand_id, winner_seat, self._pot, self._stacks[winner_seat],
        )
        self._pot = 0

    # ── アクション適用 ──

    def apply_action(self, seat: int, action: str, amount: int = 0) -> None:
        """アクションをスタック・ポット・状態に反映する。"""
        if seat not in self._stacks:
            raise ValueError(f"Unknown seat: {seat}")
        action_lower = action.lower()

        if action_lower in ("bet", "raise"):
            actual = min(amount, self._stacks[seat])
            self._stacks[seat] -= actual
            self._pot += actual
            self.invested[seat] = self.invested.get(seat, 0) + actual
            self.call_amount = max(self.call_amount, self.invested[seat])
            self.last_aggressor = seat
            self._advance_turn()
        elif action_lower == "call":
            actual = min(amount, self._stacks[seat])
            self._stacks[seat] -= actual
            self._pot += actual
            self.invested[seat] = self.invested.get(seat, 0) + actual
            self._advance_turn()
        elif action_lower in ("allin", "all_in"):
            actual = self._stacks[seat]
            self._stacks[seat] = 0
            self._pot += actual
            self.invested[seat] = self.invested.get(seat, 0) + actual
            self.all_in_seats.add(seat)
            if seat in self.turn_order:
                idx = self.turn_order.index(seat)
                self.turn_order.remove(seat)
                if self.current_turn_idx > idx:
                    self.current_turn_idx = max(0, self.current_turn_idx - 1)
                if self.turn_order:
                    self.current_turn_idx %= len(self.turn_order)
        elif action_lower == "fold":
            self.folded_seats.add(seat)
            if seat in self.turn_order:
                idx = self.turn_order.index(seat)
                self.turn_order.remove(seat)
                if self.current_turn_idx > idx:
                    self.current_turn_idx = max(0, self.current_turn_idx - 1)
                if self.turn_order:
                    self.current_turn_idx %= len(self.turn_order)
        elif action_lower == "check":
            self._advance_turn()
        else:
            raise ValueError(f"Unknown action: {action!r} (seat={seat})")

    # ── 照会 ──

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
        if seat not in self._stacks:
            raise ValueError(f"Unknown seat: {seat}")
        return self._stacks[seat]

    def get_stacks(self) -> dict[int, int]:
        return dict(self._stacks)

    def get_active_seats(self) -> list[int]:
        return [
            s for s in self.all_seats
            if s not in self.busted_seats and s not in self.folded_seats
        ]

    # ── 手動修正 ──

    def update_stack(self, seat: int, new_stack: int) -> None:
        if seat not in self._stacks:
            raise ValueError(f"Unknown seat: {seat}")
        if new_stack < 0:
            raise ValueError(f"Stack must be non-negative, got {new_stack}")
        self._stacks[seat] = new_stack
        logger.info("Stack updated: seat=%d, new_stack=%d", seat, new_stack)

    def rebuy(self, seat: int, amount: int) -> None:
        if seat not in self._stacks:
            raise ValueError(f"Unknown seat: {seat}")
        if amount <= 0:
            raise ValueError(f"Rebuy amount must be positive, got {amount}")
        self._stacks[seat] += amount
        self.busted_seats.discard(seat)
        logger.info("Rebuy: seat=%d, amount=%d, new_stack=%d", seat, amount, self._stacks[seat])

    def bust_out(self, seat: int) -> None:
        """席をバストアウトとしてマークし busted_seats に追加する（FR-05e）。"""
        if seat not in self._stacks:
            raise ValueError(f"Unknown seat: {seat}")
        self.busted_seats.add(seat)
        if seat in self.turn_order:
            idx = self.turn_order.index(seat)
            self.turn_order.remove(seat)
            if self.current_turn_idx > idx:
                self.current_turn_idx = max(0, self.current_turn_idx - 1)
            if self.turn_order:
                self.current_turn_idx %= len(self.turn_order)
        logger.info("Seat %d busted out", seat)
