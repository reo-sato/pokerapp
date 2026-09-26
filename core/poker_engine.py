"""core/poker_engine.py

R2 (ADR-0009): pokerkit.State を live ルール権威にした PokerkitGameState と、legacy
GameStateManager と差し替え可能にする PokerEngine Protocol / factory。

設計（ISSUE-0008 spike 済 / docs/contracts/hand-reconstruction.md §2-3）:

- **default は pokerkit**（`config.engine.backend`, Phase G で切替済 = ADR-0012）。`legacy` は
  config で選べる rollback path（未導入環境では warning + legacy 自動フォールバック）。
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
import math
from typing import Optional, Protocol, runtime_checkable

from core.constants import STREET_ORDER
from core.engine_types import LegalContext
from core.game_state import GameStateManager, PlayerState, Street
from core.positions import next_button, position_map, seat_order_from_button

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
    def set_player_name(self, seat: int, name: str) -> None: ...
    def get_active_seats(self) -> list[int]: ...
    def update_stack(self, seat: int, new_stack: int) -> None: ...
    def rebuy(self, seat: int, amount: int) -> None: ...

    # additive（R3 推定/訂正が読む, ADR-0009 §2）。legacy は rules-aware でない stub を返す。
    def legal_context(self) -> LegalContext: ...
    def is_hand_active(self) -> bool: ...
    def position_map(self) -> dict[int, str]: ...

    @property
    def button_seat(self) -> Optional[int]: ...
    def is_legal_actor(self, seat: int) -> bool: ...
    def fold_through(self, until_seat: int, max_folds: Optional[int] = None) -> list[int]: ...
    def pots(self) -> list[dict]: ...
    def committed(self, seat: int) -> int: ...

    # additive（勝者の自動判定, ADR-0062）。`rules_aware` が False の backend では使わない。
    rules_aware: bool
    def acting_order(self) -> list[int]: ...
    def current_pots(self) -> list[dict]: ...
    def end_hand_awards(self, awards: dict[int, int]) -> None: ...

    # additive（席の参加・休み、ブラインドの変更, 2026-09-26）。次のハンドから反映する。
    def sit_out(self, seat: int) -> bool: ...
    def sit_in(self, seat: int) -> bool: ...
    def seats_in_hand(self) -> list[int]: ...
    def set_blinds(self, sb: int, bb: int) -> None: ...

    @property
    def hand_id(self) -> int: ...
    @property
    def street(self) -> str: ...
    @property
    def pot(self) -> int: ...


def create_game_state(
    backend: str, players: list[PlayerState], sb: int, bb: int,
    button_seat: Optional[int] = None,
) -> PokerEngine:
    """backend 文字列から game-state 実装を生成する。

    - "pokerkit": `PokerkitGameState`（preview）
    - それ以外（既定 "legacy"）: 既存 `GameStateManager`

    `button_seat` は **最初のハンドで使うボタンの 1 つ手前**の席（省略時は最大の席番号 =
    ボタン導入前と同じ並び, ISSUE-0032）。legacy はボタンを持たないので無視される。
    """
    if backend == "pokerkit":
        try:
            engine = PokerkitGameState(players, sb, bb, button_seat=button_seat)
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

    # ルールの状態機械を持つ（手番・ベッティングの終わり・side pot が分かる）。勝者の自動判定は
    # これが True の backend だけで行う（ADR-0062）。
    rules_aware = True

    def __init__(
        self, players: list[PlayerState], sb: int, bb: int,
        button_seat: Optional[int] = None,
    ) -> None:
        from pokerkit import Automation  # 遅延 import（backend 選択時のみ pokerkit 必須）

        if not players:
            raise ValueError("players must not be empty")
        self._sb = sb
        self._bb = bb
        self._players: dict[int, PlayerState] = {p.seat: p for p in players}
        self._seats: list[int] = sorted(self._players)          # 安定 seat 順
        # ボタン（ISSUE-0032 / 仕様 FR-05b）。**次の `new_hand` で使う**席を持ち、ハンド開始時に
        # 1 つ進める。初期値 None は「最大の席番号から始める」= ボタン導入前と同じ並びになる
        # （`next_button` 参照。golden fixtures の 1 ハンド目が不変）。
        self._button_seat: Optional[int] = button_seat
        # pokerkit へ渡す並び（index 0=SB … 末尾=BTN）。`new_hand` で作り直す。
        self._order: list[int] = list(self._seats)
        self._seat_to_idx: dict[int, int] = {s: i for i, s in enumerate(self._order)}
        self._idx_to_seat: dict[int, int] = {i: s for i, s in enumerate(self._order)}
        self._stacks: dict[int, int] = {s: self._players[s].stack for s in self._seats}  # 永続（hand 跨ぎ）
        # 休みの席（操作で外した。次のハンドから配られない）。スタック 0 の席も配られない。
        self._sitting_out: set[int] = set()
        # いまのハンド（最後のハンド）に配られた席。ボタン・ブラインド・手番はこの席だけで回す。
        self._hand_seats: list[int] = list(self._seats)
        # 次のハンドから使うブラインド（ハンドの途中に変えたとき）
        self._pending_blinds: Optional[tuple[int, int]] = None
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

    def playing_seats(self) -> list[int]:
        """次のハンドに配られる席（休みでなく、チップがある席）。"""
        return [s for s in self._seats if s not in self._sitting_out and self._stacks[s] > 0]

    def new_hand(self) -> int:
        """新しいハンドを始める。配られる席が 2 つ未満なら ValueError（状態は変えない）。"""
        from pokerkit import NoLimitTexasHoldem

        playing = self.playing_seats()
        if len(playing) < 2:
            raise ValueError(
                "配られる席が 2 つ未満です（休み: "
                f"{sorted(self._sitting_out) or 'なし'} / スタック 0: "
                f"{[s for s in self._seats if self._stacks[s] <= 0] or 'なし'}）"
            )
        if self._pending_blinds is not None:
            self._sb, self._bb = self._pending_blinds
            self._pending_blinds = None
        self._hand_id += 1
        # ボタンを 1 つ進めてから並びを作る（ボタンの次が SB, 末尾が BTN）。前のボタンの席が
        # 抜けていたら（バースト・休み）、その次の席にボタンを置く。
        if self._button_seat is None or self._button_seat in playing:
            self._button_seat = next_button(playing, self._button_seat)
        else:
            later = [s for s in playing if s > self._button_seat]
            self._button_seat = later[0] if later else playing[0]
        self._hand_seats = list(playing)
        self._order = seat_order_from_button(playing, self._button_seat)
        self._seat_to_idx = {s: i for i, s in enumerate(self._order)}
        self._idx_to_seat = {i: s for i, s in enumerate(self._order)}
        stacks = [self._stacks[s] for s in self._order]
        self._hand_start_stacks = list(stacks)
        self._state = NoLimitTexasHoldem.create_state(
            self._automations, True, 0, (self._sb, self._bb), self._bb, stacks, len(stacks),
        )
        self._hand_active = True
        self._final_pots = []
        logger.info(
            "New hand (pokerkit) started: hand_id=%d button=seat %d (%s)",
            self._hand_id, self._button_seat,
            " ".join(f"{s}:{n}" for s, n in self.position_map().items()),
        )
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
        if winner_seat not in self._hand_seats:
            raise ValueError(f"Seat {winner_seat} is not in this hand")
        st = self._state
        if st is None:
            raise RuntimeError("end_hand called without an active hand")
        pot_total = sum(self._hand_start_stacks) - sum(st.stacks)
        # side-pot スナップショット（HandSummary 用 additive 情報）
        self._final_pots = self._snapshot_pots(pot_total)
        for s in self._hand_seats:
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
            if seat not in self._hand_seats:
                raise ValueError(f"Unknown seat: {seat}")
        st = self._state
        if st is None:
            raise RuntimeError("end_hand_split called without an active hand")
        pot_total = sum(self._hand_start_stacks) - sum(st.stacks)
        self._final_pots = self._snapshot_pots(pot_total)
        for s in self._hand_seats:
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

    def end_hand_awards(self, awards: dict[int, int]) -> None:
        """ショーダウンの判定どおりに pot を配って確定する（席 → 受け取る額, ADR-0062）。

        side pot の勝者が main pot と違う場合（短いスタックのオールイン）も正しく配れる。
        合計は pot と一致しなければならない（ずれていたら何も変えずに ValueError）。
        """
        st = self._state
        if st is None or not self._hand_active:
            raise RuntimeError("end_hand_awards called without an active hand")
        for seat in awards:
            if seat not in self._hand_seats:
                raise ValueError(f"Unknown seat: {seat}")
        pot_total = sum(self._hand_start_stacks) - sum(st.stacks)
        if sum(awards.values()) != pot_total or any(a < 0 for a in awards.values()):
            raise ValueError(f"awards {awards} do not add up to the pot {pot_total}")
        self._final_pots = self._snapshot_pots(pot_total)
        for s in self._hand_seats:
            self._stacks[s] = st.stacks[self._seat_to_idx[s]]
        for seat, amount in awards.items():
            self._stacks[seat] += amount
        self._hand_active = False
        logger.info("Hand %d ended (pokerkit, showdown). Awards: %s", self._hand_id, awards)

    def acting_order(self) -> list[int]:
        """このハンドのフロップ以降の手番の順（一番アウトオブポジション = ボタンの次の席が先頭、
        ボタンが最後。heads-up はボタンでない方が先頭）。"""
        return list(self._order)

    def current_pots(self) -> list[dict]:
        """進行中のハンドの main / side pot（`[{"amount", "eligible_seats"}]`, main pot が先頭）。
        フォールドした席は対象に入らない。状態は変えない。"""
        st = self._state
        if st is None or not self._hand_active:
            return []
        return self._snapshot_pots(sum(self._hand_start_stacks) - sum(st.stacks))

    # ――― アクション適用 ―――

    def snapshot(self) -> dict:
        """いまの状態の複製（札の離脱で入れたフォールドを取り消して組み直すため）。"""
        return copy.deepcopy(self.__dict__)

    def restore(self, snapshot: dict) -> None:
        """`snapshot()` の時点に戻す（同じオブジェクトのまま = 参照している側はそのまま使える）。"""
        self.__dict__.clear()
        self.__dict__.update(copy.deepcopy(snapshot))

    def seats_to_act(self) -> list[int]:
        """このベッティングラウンドでまだ行動する席（手番の順。先頭が actor）。"""
        st = self._state
        if st is None or not self._hand_active:
            return []
        return [self._idx_to_seat[i] for i in st.actor_indices]

    def force_fold(self, seat: int) -> None:
        """チェックできる場面でもフォールドにする（席の札が離れた = 降りた, 2026-09-25）。

        pokerkit はトーナメントの扱いではチェックできるときのフォールドを受け付けないので、その 1 回だけ
        キャッシュゲームの扱い（警告だけ）にする。
        """
        import warnings

        from pokerkit import Mode

        st = self._state
        if st is None or not self._hand_active:
            raise ValueError("No active hand")
        if seat != self.get_current_player():
            raise ValueError(f"Seat {seat} is not the actor")
        mode = st.mode
        st.mode = Mode.CASH_GAME
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                st.fold()
        finally:
            st.mode = mode

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
        if st is None or not self._hand_active or st.actor_index is None:
            raise RuntimeError("No actor (no active hand or hand over)")
        return self._idx_to_seat[st.actor_index]

    def legal_context(self) -> LegalContext:
        """合法手プリオール（ADR-0009 §B / R3 が読む）。

        `end_hand` 後の state は actor_index を持ったままなので、`_hand_active` を見ないと
        「終わったハンドに合法手がある」と答えてしまう（ISSUE-0028）。
        """
        st = self._state
        if st is None or not self._hand_active or st.actor_index is None:
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
            chip=math.gcd(self._sb, self._bb),
        )

    def is_legal_actor(self, seat: int) -> bool:
        st = self._state
        return (
            st is not None
            and self._hand_active
            and st.actor_index is not None
            and self._idx_to_seat.get(st.actor_index) == seat
        )

    @property
    def button_seat(self) -> Optional[int]:
        """現ハンドのボタン席（`new_hand` 前は None, ISSUE-0032）。"""
        return self._button_seat

    def position_map(self) -> dict[int, str]:
        """seat → ポジション名（BTN/SB/BB/UTG…, 仕様 §6.1）。ボタン未確定なら空。"""
        if self._button_seat is None:
            return {}
        return position_map(self._hand_seats, self._button_seat)

    def is_hand_active(self) -> bool:
        """ハンドが進行中か（新ハンド前 / `end_hand` 後は False, ISSUE-0028）。

        actor の有無とは別物: 全員オールインの runout 中は actor が居なくてもハンドは
        進行中で、winner 宣言を受け付ける必要がある。
        """
        return self._state is not None and self._hand_active

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
            eligible = [s for s in self._hand_seats if st.statuses[self._seat_to_idx[s]]]
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
        idx = self._seat_to_idx.get(seat)
        return st.bets[idx] if idx is not None and idx < len(st.bets) else 0

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
        """全席のスタック（ハンドに入っていない席は持ち越しの値）。"""
        stacks = dict(self._stacks)
        st = self._state
        if st is not None and self._hand_active:
            stacks.update({s: st.stacks[self._seat_to_idx[s]] for s in self._hand_seats})
        return stacks

    def get_player_name(self, seat: int) -> str:
        if seat not in self._players:
            raise ValueError(f"Unknown seat: {seat}")
        return self._players[seat].name

    def set_player_name(self, seat: int, name: str) -> None:
        """席のプレイヤー名を変える（席替え, ADR-0059）。スタックとハンドの状態は変えない。"""
        if seat not in self._players:
            raise ValueError(f"Unknown seat: {seat}")
        self._players[seat].name = name

    def get_active_seats(self) -> list[int]:
        st = self._state
        if st is None or not self._hand_active:
            return list(self._hand_seats)
        return [s for s in self._hand_seats if st.statuses[self._seat_to_idx[s]]]

    # ――― 席の参加・休み、ブラインドの変更（次のハンドから, 2026-09-26） ―――

    def seats_in_hand(self) -> list[int]:
        """いまのハンド（まだ無ければ最後のハンド / 全席）に配られた席。"""
        return list(self._hand_seats)

    def sit_out(self, seat: int) -> bool:
        """席を休みにする（次のハンドから配られない。スタックは持ち越す）。変わったら True。"""
        if seat not in self._players:
            raise ValueError(f"Unknown seat: {seat}")
        if seat in self._sitting_out:
            return False
        self._sitting_out.add(seat)
        return True

    def sit_in(self, seat: int) -> bool:
        """休みの席を戻す（次のハンドから配られる。スタック 0 なら買い足すまで配られない）。変わったら True。"""
        if seat not in self._players:
            raise ValueError(f"Unknown seat: {seat}")
        if seat not in self._sitting_out:
            return False
        self._sitting_out.discard(seat)
        return True

    def set_blinds(self, sb: int, bb: int) -> None:
        """ブラインドを変える（トーナメントのレベル上昇）。ハンドの途中なら次のハンドから。"""
        if sb <= 0 or bb <= 0 or sb > bb:
            raise ValueError(f"Invalid blinds: sb={sb} bb={bb}")
        if self._hand_active:
            self._pending_blinds = (sb, bb)
        else:
            self._sb, self._bb = sb, bb
            self._pending_blinds = None

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
