"""core/poker_engine.py

R2 (ADR-0009): pokerkit.State を live ルール権威にした PokerkitGameState と、legacy
GameStateManager と差し替え可能にする PokerEngine Protocol / factory。

設計（ISSUE-0008 spike 済 / docs/contracts/hand-reconstruction.md §2-3）:

- **default は legacy**（`config.engine.backend`）。pokerkit backend は **default-off の preview**。
- pokerkit はダミーカードを自動配布して betting state machine を駆動する
  （カードは RFID が source。pokerkit のダミーは hole-card 記録には使わない）。
- **announced winner を優先**するため showdown / push automation は外し、`end_hand` で手動 push。
- **pokerkit は import を遅延**する（backend=pokerkit を選ぶまで未インストールでも動く）。
- raw ASR action → 合法手への射影（amount snap 等）は **R3 (apply_corrections)** で行う。本クラスの
  `apply_action` は legacy と同じく「不正なら ValueError」を契約とする（engine が needs_review を付与）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable

from core.constants import STREET_ORDER
from core.game_state import GameStateManager, PlayerState, Street

logger = logging.getLogger(__name__)


@dataclass
class LegalContext:
    """ある時点の合法手プリオール（推定・訂正が読む, ADR-0009）。"""

    actor_seat: Optional[int]
    legal_actions: frozenset[str]   # subset of {"fold","check","call","bet","raise","allin"}
    amount_to_call: int
    min_raise: int                  # "to" total（raise 不可なら 0）
    max_raise: int                  # "to" total（= all-in 額。raise 不可なら 0）


@runtime_checkable
class PokerEngine(Protocol):
    """hand logger が依存する game-state 境界（legacy / pokerkit 共通の安定 I/F）。"""

    def new_hand(self) -> int: ...
    def advance_street(self, street: Street) -> None: ...
    def end_hand(self, winner_seat: int) -> None: ...
    def apply_action(self, seat: int, action: str, amount: int = 0) -> None: ...
    def get_current_player(self) -> int: ...
    def get_stacks(self) -> dict[int, int]: ...
    def get_stack(self, seat: int) -> int: ...
    def get_player_name(self, seat: int) -> str: ...
    def get_active_seats(self) -> list[int]: ...
    def update_stack(self, seat: int, new_stack: int) -> None: ...
    def rebuy(self, seat: int, amount: int) -> None: ...

    @property
    def hand_id(self) -> int: ...
    @property
    def street(self) -> str: ...
    @property
    def pot(self) -> int: ...


def create_game_state(
    backend: str, players: list[PlayerState], sb: int, bb: int
) -> PokerEngine:
    """backend 文字列から game-state 実装を生成する。

    - "pokerkit": `PokerkitGameState`（preview）
    - それ以外（既定 "legacy"）: 既存 `GameStateManager`
    """
    if backend == "pokerkit":
        logger.info("Using pokerkit game-state backend (preview)")
        return PokerkitGameState(players, sb, bb)
    return GameStateManager(players, sb, bb)


class PokerkitGameState:
    """pokerkit.State を権威にした GameStateManager 互換実装（R2, preview）。

    legacy との既知の差（より正確側）:
    - **ブラインドを自動 post** する（legacy は post しない）。stack_start は post 後を反映。
    - **合法手のみ受理**（不正 action/amount は ValueError）。raw ASR の射影は R3。
    - street は betting 完了で **自動進行**（`advance_street` の外部シグナルは cross-check 扱い）。
    - mid-hand の `update_stack` / `rebuy` は次ハンドから反映（pokerkit state は hand 単位）。
    """

    def __init__(self, players: list[PlayerState], sb: int, bb: int) -> None:
        from pokerkit import Automation  # 遅延 import（backend 選択時のみ pokerkit 必須）

        if not players:
            raise ValueError("players must not be empty")
        self._sb = sb
        self._bb = bb
        self._players: dict[int, PlayerState] = {p.seat: p for p in players}
        self._seats: list[int] = sorted(self._players)          # 安定 seat 順
        self._seat_to_idx: dict[int, int] = {s: i for i, s in enumerate(self._seats)}
        self._idx_to_seat: dict[int, int] = {i: s for i, s in enumerate(self._seats)}
        self._stacks: dict[int, int] = {s: self._players[s].stack for s in self._seats}  # 永続（hand 跨ぎ）
        self._hand_id: int = 0
        self._state = None
        self._hand_active: bool = False
        self._hand_start_stacks: list[int] = []
        self._final_pots: list[dict] = []
        # showdown / push 系 automation は外す（announced winner を手動 push するため）
        self._automations = (
            Automation.ANTE_POSTING,
            Automation.BET_COLLECTION,
            Automation.BLIND_OR_STRADDLE_POSTING,
            Automation.CARD_BURNING,
            Automation.HOLE_DEALING,
            Automation.BOARD_DEALING,
        )

    # ――― ハンド管理 ―――

    def new_hand(self) -> int:
        from pokerkit import NoLimitTexasHoldem

        self._hand_id += 1
        stacks = [self._stacks[s] for s in self._seats]
        self._hand_start_stacks = list(stacks)
        self._state = NoLimitTexasHoldem.create_state(
            self._automations, True, 0, (self._sb, self._bb), self._bb, stacks, len(stacks),
        )
        self._hand_active = True
        self._final_pots = []
        logger.info("New hand (pokerkit) started: hand_id=%d", self._hand_id)
        return self._hand_id

    def advance_street(self, street: Street) -> None:
        # pokerkit は betting 完了で自動進行する。外部シグナル（RFID/audio）は cross-check 扱いで no-op。
        cur = self.street
        try:
            if STREET_ORDER.index(street.value) <= STREET_ORDER.index(cur):
                logger.debug("advance_street(%s) no-op (pokerkit current=%s)", street.value, cur)
                return
        except ValueError:
            return
        logger.debug(
            "advance_street(%s) requested; pokerkit が betting 完了で自動進行 (current=%s)",
            street.value, cur,
        )

    def end_hand(self, winner_seat: int) -> None:
        if winner_seat not in self._players:
            raise ValueError(f"Unknown seat: {winner_seat}")
        st = self._state
        if st is None:
            raise RuntimeError("end_hand called without an active hand")
        # side-pot スナップショット（HandSummary 用 additive 情報）
        self._final_pots = [
            {"amount": p.amount, "eligible_seats": [self._idx_to_seat[i] for i in p.player_indices]}
            for p in st.pots
        ]
        pot_total = sum(self._hand_start_stacks) - sum(st.stacks)
        for s in self._seats:
            self._stacks[s] = st.stacks[self._seat_to_idx[s]]
        self._stacks[winner_seat] += pot_total
        self._hand_active = False
        logger.info(
            "Hand %d ended (pokerkit). Seat %d wins pot %d. New stack: %d",
            self._hand_id, winner_seat, pot_total, self._stacks[winner_seat],
        )

    # ――― アクション適用 ―――

    def apply_action(self, seat: int, action: str, amount: int = 0) -> None:
        st = self._state
        if st is None or not self._hand_active:
            raise ValueError("No active hand")
        actor = self.get_current_player()
        if seat != actor:
            raise ValueError(f"Seat {seat} is not the actor (pokerkit actor={actor})")
        a = action.lower()
        if a == "fold":
            if not st.can_fold():
                raise ValueError("fold not legal")
            st.fold()
        elif a in ("check", "call"):
            if not st.can_check_or_call():
                raise ValueError("check/call not legal")
            st.check_or_call()
        elif a in ("bet", "raise", "allin"):
            target = st.max_completion_betting_or_raising_to_amount if a == "allin" else amount
            # R2: amount は "to" 総額前提（raw ASR からの射影は R3 apply_corrections）。
            if target is None or not st.can_complete_bet_or_raise_to(target):
                raise ValueError(f"bet/raise to {target!r} not legal")
            st.complete_bet_or_raise_to(target)
        else:
            raise ValueError(f"Unknown action: {action!r} (seat={seat})")

    # ――― 照会 ―――

    def get_current_player(self) -> int:
        st = self._state
        if st is None or st.actor_index is None:
            raise RuntimeError("No actor (no active hand or hand over)")
        return self._idx_to_seat[st.actor_index]

    def legal_context(self) -> LegalContext:
        """合法手プリオール（ADR-0009 §B / R3 が読む）。"""
        st = self._state
        if st is None or st.actor_index is None:
            return LegalContext(None, frozenset(), 0, 0, 0)
        legal: set[str] = set()
        if st.can_fold():
            legal.add("fold")
        if st.can_check_or_call():
            legal.add("check" if st.checking_or_calling_amount == 0 else "call")
        if st.can_complete_bet_or_raise_to():
            legal.add("raise" if any(st.bets) else "bet")
            legal.add("allin")
        return LegalContext(
            actor_seat=self._idx_to_seat[st.actor_index],
            legal_actions=frozenset(legal),
            amount_to_call=st.checking_or_calling_amount,
            min_raise=st.min_completion_betting_or_raising_to_amount or 0,
            max_raise=st.max_completion_betting_or_raising_to_amount or 0,
        )

    def is_legal_actor(self, seat: int) -> bool:
        st = self._state
        return (
            st is not None
            and st.actor_index is not None
            and self._idx_to_seat.get(st.actor_index) == seat
        )

    def pots(self) -> list[dict]:
        """最後の end_hand 時点の main/side pot スナップショット（HandSummary.pots 用）。"""
        return list(self._final_pots)

    def committed(self, seat: int) -> int:
        """当該ストリートのコミット額（pokerkit bets）。"""
        st = self._state
        if st is None:
            return 0
        idx = self._seat_to_idx[seat]
        return st.bets[idx] if idx < len(st.bets) else 0

    @property
    def hand_id(self) -> int:
        return self._hand_id

    @property
    def street(self) -> str:
        st = self._state
        if st is None:
            return Street.PREFLOP.value
        si = st.street_index
        if si is None:
            return Street.SHOWDOWN.value
        names = [Street.PREFLOP.value, Street.FLOP.value, Street.TURN.value, Street.RIVER.value]
        return names[si] if si < len(names) else Street.SHOWDOWN.value

    @property
    def pot(self) -> int:
        st = self._state
        if st is None or not self._hand_active:
            return 0
        return sum(self._hand_start_stacks) - sum(st.stacks)

    def get_stack(self, seat: int) -> int:
        return self.get_stacks()[seat]

    def get_stacks(self) -> dict[int, int]:
        st = self._state
        if st is not None and self._hand_active:
            return {s: st.stacks[self._seat_to_idx[s]] for s in self._seats}
        return dict(self._stacks)

    def get_player_name(self, seat: int) -> str:
        if seat not in self._players:
            raise ValueError(f"Unknown seat: {seat}")
        return self._players[seat].name

    def get_active_seats(self) -> list[int]:
        st = self._state
        if st is None or not self._hand_active:
            return list(self._seats)
        return [s for s in self._seats if st.statuses[self._seat_to_idx[s]]]

    # ――― 手動修正（pokerkit は hand 単位のため次ハンドから反映） ―――

    def update_stack(self, seat: int, new_stack: int) -> None:
        if seat not in self._players:
            raise ValueError(f"Unknown seat: {seat}")
        if new_stack < 0:
            raise ValueError(f"Stack must be non-negative, got {new_stack}")
        if self._hand_active:
            logger.warning("update_stack mid-hand (pokerkit): 次ハンドから反映 (seat=%d)", seat)
        self._stacks[seat] = new_stack

    def rebuy(self, seat: int, amount: int) -> None:
        if seat not in self._players:
            raise ValueError(f"Unknown seat: {seat}")
        if amount <= 0:
            raise ValueError(f"Rebuy amount must be positive, got {amount}")
        if self._hand_active:
            logger.warning("rebuy mid-hand (pokerkit): 次ハンドから反映 (seat=%d)", seat)
        self._stacks[seat] += amount
