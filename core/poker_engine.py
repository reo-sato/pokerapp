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

import copy
import logging
from typing import Optional, Protocol, runtime_checkable

from core.constants import STREET_ORDER
from core.engine_types import LegalContext
from core.game_state import GameStateManager, PlayerState, Street

logger = logging.getLogger(__name__)

# LegalContext は core.engine_types に移設（循環 import 回避）。後方互換のため再エクスポート。
__all__ = ["LegalContext", "PokerEngine", "PokerkitGameState", "create_game_state"]


@runtime_checkable
class PokerEngine(Protocol):
    """hand logger が依存する game-state 境界（legacy / pokerkit 共通の安定 I/F）。"""

    def new_hand(self) -> int: ...
    def advance_street(self, street: Street) -> None: ...
    def end_hand(self, winner_seat: int) -> None: ...
    def end_hand_split(self, winner_seats: list[int]) -> dict[int, int]: ...
    def apply_action(self, seat: int, action: str, amount: int = 0) -> None: ...
    def get_current_player(self) -> int: ...
    def get_stacks(self) -> dict[int, int]: ...
    def get_stack(self, seat: int) -> int: ...
    def get_player_name(self, seat: int) -> str: ...
    def get_active_seats(self) -> list[int]: ...
    def update_stack(self, seat: int, new_stack: int) -> None: ...
    def rebuy(self, seat: int, amount: int) -> None: ...

    # additive（R3 推定/訂正が読む, ADR-0009 §2）。legacy は rules-aware でない stub を返す。
    def legal_context(self) -> LegalContext: ...
    def is_legal_actor(self, seat: int) -> bool: ...
    def fold_through(self, until_seat: int, max_folds: Optional[int] = None) -> list[int]: ...
    def pots(self) -> list[dict]: ...
    def committed(self, seat: int) -> int: ...

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
        try:
            engine = PokerkitGameState(players, sb, bb)
        except ImportError:
            # 既定 backend が pokerkit (Phase G) のため、未導入環境でも起動だけは
            # 落とさない。rules-aware 機能（actor 推定/合法手射影等）は無効になる。
            logger.warning(
                "pokerkit が import できないため legacy backend にフォールバックします"
                "（`pip install pokerkit` で rules-aware を有効化）"
            )
            return GameStateManager(players, sb, bb)
        logger.info("Using pokerkit game-state backend")
        return engine
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
        pot_total = sum(self._hand_start_stacks) - sum(st.stacks)
        # side-pot スナップショット（HandSummary 用 additive 情報）
        self._final_pots = self._snapshot_pots(pot_total)
        for s in self._seats:
            self._stacks[s] = st.stacks[self._seat_to_idx[s]]
        self._stacks[winner_seat] += pot_total
        self._hand_active = False
        logger.info(
            "Hand %d ended (pokerkit). Seat %d wins pot %d. New stack: %d",
            self._hand_id, winner_seat, pot_total, self._stacks[winner_seat],
        )

    def end_hand_split(self, winner_seats: list[int]) -> dict[int, int]:
        """announced chop（split pot, ADR-D S7）: pot を勝者間で等分して push する。

        端数チップは読み上げ順の先頭勝者に寄せる（実運用の odd-chip ルールは店により
        異なるため、決定的な単純規則に固定して監査可能にする）。seat→授与額を返す。
        """
        if not winner_seats:
            raise ValueError("winner_seats must not be empty")
        for seat in winner_seats:
            if seat not in self._players:
                raise ValueError(f"Unknown seat: {seat}")
        st = self._state
        if st is None:
            raise RuntimeError("end_hand_split called without an active hand")
        pot_total = sum(self._hand_start_stacks) - sum(st.stacks)
        self._final_pots = self._snapshot_pots(pot_total)
        for s in self._seats:
            self._stacks[s] = st.stacks[self._seat_to_idx[s]]
        share, remainder = divmod(pot_total, len(winner_seats))
        awards: dict[int, int] = {}
        for i, seat in enumerate(winner_seats):
            amount = share + (remainder if i == 0 else 0)
            awards[seat] = awards.get(seat, 0) + amount
            self._stacks[seat] += amount
        self._hand_active = False
        logger.info("Hand %d ended (pokerkit, chop). Awards: %s", self._hand_id, awards)
        return awards

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
        elif a == "allin":
            # raise 可なら max へ raise。不可だが call 可ならショートスタックの call-all-in。
            # （両方不可は実質ありえないが安全側で error）。これにより desync を防ぐ。
            if st.can_complete_bet_or_raise_to():
                st.complete_bet_or_raise_to(st.max_completion_betting_or_raising_to_amount)
            elif st.can_check_or_call():
                st.check_or_call()
            else:
                raise ValueError("allin not legal")
        elif a in ("bet", "raise"):
            # R2: amount は "to" 総額前提（raw ASR からの射影は R3 apply_corrections）。
            if amount is None or not st.can_complete_bet_or_raise_to(amount):
                raise ValueError(f"bet/raise to {amount!r} not legal")
            st.complete_bet_or_raise_to(amount)
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
            bb=self._bb,
            committed=st.bets[st.actor_index] if st.actor_index < len(st.bets) else 0,
        )

    def is_legal_actor(self, seat: int) -> bool:
        st = self._state
        return (
            st is not None
            and st.actor_index is not None
            and self._idx_to_seat.get(st.actor_index) == seat
        )

    def fold_through(self, until_seat: int, max_folds: Optional[int] = None) -> list[int]:
        """現 actor から until_seat が手番になるまで中間席を silent fold 合成し、folded した席列を返す（ADR-0009 §4）。

        ディーラー未宣言の fold（最頻のズレ）を、物理/明示証拠が指す actor へ追いつくために中間席を
        fold して表現する。`max_folds` を超える / until_seat に到達できない（途中で手番が消える・fold
        不可）場合は ValueError を投げ、**状態は呼び出し前に巻き戻す（atomic）**（呼び出し側は prior
        維持 + needs_review）。誤 fold が以降の手番を壊さないための atomicity と、過剰合成を防ぐ
        `max_folds`（ISSUE-0009: 既定の上限は呼び出し側 actor 推定が渡す）が D2b の安全装置。
        """
        st = self._state
        if st is None or not self._hand_active:
            raise ValueError("No active hand")
        if until_seat not in self._players:
            raise ValueError(f"Unknown seat: {until_seat}")

        snapshot = copy.deepcopy(st)  # 失敗時の atomic 巻き戻し用
        folded: list[int] = []
        try:
            while True:
                if st.actor_index is None:
                    raise ValueError("Hand ended before reaching until_seat")
                cur = self._idx_to_seat[st.actor_index]
                if cur == until_seat:
                    return folded
                if max_folds is not None and len(folded) >= max_folds:
                    raise ValueError(
                        f"fold_through exceeds max_folds={max_folds} reaching seat {until_seat}"
                    )
                if not st.can_fold():
                    raise ValueError(f"Cannot fold seat {cur} to reach {until_seat}")
                st.fold()
                folded.append(cur)
                if len(folded) > len(self._seats):
                    raise ValueError("fold_through exceeded table size (no convergence)")
        except ValueError:
            self._state = snapshot  # 中途半端な fold を残さない
            raise

    def _snapshot_pots(self, pot_total: int) -> list[dict]:
        """end_hand(_split) 時点の main/side pot スナップショットを作る。

        betting round 途中で winner が宣言された場合（全員 fold 等）、pokerkit は bet 未回収で
        `st.pots` が空/過少になる。その場合は実コミット総額（pot_total = 開始スタック合計 −
        現スタック合計）を単一 pot として合成する（ADR-A S6: pot_total の権威は常に engine）。
        """
        st = self._state
        pots = [
            {"amount": p.amount, "eligible_seats": [self._idx_to_seat[i] for i in p.player_indices]}
            for p in st.pots
        ]
        collected = sum(p["amount"] for p in pots)
        if pot_total > 0 and collected < pot_total:
            eligible = [s for s in self._seats if st.statuses[self._seat_to_idx[s]]]
            if pots:
                # 回収済み pot + 未回収 bet の残差を最後の pot 相当として追記。
                pots.append({"amount": pot_total - collected, "eligible_seats": eligible})
            else:
                pots = [{"amount": pot_total, "eligible_seats": eligible}]
        return pots

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
