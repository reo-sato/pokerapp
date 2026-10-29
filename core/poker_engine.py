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
from typing import Any, Optional, Protocol, runtime_checkable

from core.constants import CHIP_UNIT, STREET_ORDER
from core.engine_types import LegalContext
from core.game_state import GameStateManager, PlayerState, Street
from core.positions import advance_button, next_hand_seating, position_map, seat_order_from_button

logger = logging.getLogger(__name__)

# 持ち点を数えないモード（`stackless`, オーナー 2026-10-24）で各席に持たせる持ち点。誰も足りなくならない大きさ
STACKLESS_STACK = 10 ** 12

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
    def set_bb_ante(self, on: "bool | int") -> None: ...
    def set_button(self, seat: int) -> None: ...

    @property
    def hand_id(self) -> int: ...
    @property
    def street(self) -> str: ...
    @property
    def pot(self) -> int: ...


def _advance_button(playing: list[int], button: Optional[int]) -> int:
    """前のボタンから 1 つ進めたボタン（前のボタンの席が抜けていたら、その次の席, 仕様 FR-05b）。"""
    return advance_button(playing, button)


def create_game_state(
    backend: str, players: list[PlayerState], sb: int, bb: int,
    button_seat: Optional[int] = None, *, stackless: bool = False,
) -> PokerEngine:
    """backend 文字列から game-state 実装を生成する。

    `stackless`: 持ち点を数えないモード（pokerkit のみ。`PokerkitGameState` 参照）。legacy では無視する。

    - "pokerkit": `PokerkitGameState`（preview）
    - それ以外（既定 "legacy"）: 既存 `GameStateManager`

    `button_seat` は **最初のハンドで使うボタンの 1 つ手前**の席（省略時は最大の席番号 =
    ボタン導入前と同じ並び, ISSUE-0032）。legacy はボタンを持たないので無視される。
    """
    if backend == "pokerkit":
        try:
            engine = PokerkitGameState(players, sb, bb, button_seat=button_seat, stackless=stackless)
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

    **持ち点を数えないモード**（`stackless=True`, オーナー 2026-10-24。リングでもトーナメントでも使う）: 持ち点は
    分からないものとして扱い、オールインはオールインとだけ記録する（額は書かない）。内部では各席が
    `STACKLESS_STACK` を持ってハンドを始める（誰も足りなくならない = 言った額のベットを持ち点で切らない・持ち点 0 で
    配られない席が無い）。オールインは内部では最小のレイズ（仮の額）にして、その席を「オールインした席」にする。
    オールインした席には以後手番を回さない（自動でコール / チェックする = 本物のオールインと同じ）。オールインして
    いない人が 1 人だけになり、その人が出す額が無ければ、その人もチェックで進める（ボードを最後まで開く）。
    記録の額・持ち点を書かないのは `HandSummary.to_dict`（`core/stackless.py`）。
    """

    # ルールの状態機械を持つ（手番・ベッティングの終わり・side pot が分かる）。勝者の自動判定は
    # これが True の backend だけで行う（ADR-0062）。
    rules_aware = True

    def __init__(
        self, players: list[PlayerState], sb: int, bb: int,
        button_seat: Optional[int] = None, *, stackless: bool = False,
    ) -> None:
        from pokerkit import Automation  # 遅延 import（backend 選択時のみ pokerkit 必須）

        if not players:
            raise ValueError("players must not be empty")
        self._sb = sb
        self._bb = bb
        self._stackless = bool(stackless)
        # このハンドでオールインした席（持ち点を数えないモード。以後手番を回さない）
        self._allin_seats: set[int] = set()
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
        # ハンドの途中に買い足した額（ハンドの席）。ハンドの終わりに席のスタックを書き直すので、次のハンドの
        # 始めに足す（そのハンドの結果には入れない）
        self._pending_rebuys: dict[int, int] = {}
        # 休みの席（操作で外した。次のハンドから配られない）。スタック 0 の席も配られない。
        self._sitting_out: set[int] = set()
        # いまのハンド（最後のハンド）に配られた席。ボタン・ブラインド・手番はこの席だけで回す。
        self._hand_seats: list[int] = list(self._seats)
        # 次のハンドから使うブラインド（ハンドの途中に変えたとき）
        self._pending_blinds: Optional[tuple[int, int]] = None
        # BB アンティ（BB の人がアンティを出す, CLI の `ante on/off` 2026-10-19・トーナメントのレベル ADR-0071）。
        # False = 無し / True = BB と同額（BB を変えればアンティも変わる）/ 整数 = その額。ハンドの途中に変えたら次の
        # ハンドから（`_pending_bb_ante`）
        self._bb_ante_on: "bool | int" = False
        self._pending_bb_ante: "Optional[bool | int]" = None
        self._posted_ante: int = 0          # いまのハンドで BB の人が払ったアンティ
        # 次のハンドのボタンを手で指定（仕様 FR-05g）。使ったら (通常の進み方の席, 指定した席) を残す。
        self._forced_button: Optional[int] = None
        # デッドスモール（BB の人が抜けたあとのハンドで SB が空き, `next_hand_seating`, 2026-10-29）: 空いた SB の席。
        # そのハンドは BB だけを払い、次のハンドのボタンはこの席（デッドボタン）。
        self._dead_sb: Optional[int] = None
        self._forced_dead_sb: Optional[int] = None
        # 前のハンドのボタン（このハンドの席を組み直すときに、そこから進め直す）と、このハンドのボタンを手で
        # （か配った順で）直したか
        self._prev_button: Optional[int] = button_seat
        self._button_by_hand: bool = False
        self.last_button_override: Optional[tuple[int, int]] = None
        # 次のハンドの持ち点を決める（台本のハンド = 台本どおりの持ち点で始める, 2026-09-30）
        self._forced_stacks: Optional[dict[int, int]] = None
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
        """次のハンドに配られる席（休みでなく、チップがある席。ハンドの途中の買い足しも数える）。"""
        if self._stackless:
            return [s for s in self._seats if s not in self._sitting_out]       # 持ち点を数えない
        return [s for s in self._seats
                if s not in self._sitting_out and self._stacks[s] + self._pending_rebuys.get(s, 0) > 0]

    def new_hand(self) -> int:
        """新しいハンドを始める。配られる席が 2 つ未満なら ValueError（状態は変えない）。"""
        from pokerkit import NoLimitTexasHoldem

        forced, saved = self._forced_stacks, (dict(self._stacks), dict(self._pending_rebuys))
        if forced is not None:
            # 台本の持ち点（前のハンドの結果・買い足しの保留より優先）
            for seat, stack in forced.items():
                self._stacks[seat] = stack
                self._pending_rebuys.pop(seat, None)
            self._forced_stacks = None
        playing = self.playing_seats()
        if len(playing) < 2:
            self._stacks, self._pending_rebuys = saved
            self._forced_stacks = forced
            raise ValueError(
                "配られる席が 2 つ未満です（休み: "
                f"{sorted(self._sitting_out) or 'なし'} / スタック 0: "
                f"{[s for s in self._seats if self._stacks[s] <= 0] or 'なし'}）"
            )
        self._apply_pending_rebuys()
        if self._pending_blinds is not None:
            self._sb, self._bb = self._pending_blinds
            self._pending_blinds = None
        if self._pending_bb_ante is not None:
            self._bb_ante_on = self._pending_bb_ante
            self._pending_bb_ante = None
        self._hand_id += 1
        # ボタンを 1 つ進めてから並びを作る（ボタンの次が SB, 末尾が BTN）。前のボタンの席が
        # 抜けていたら（バースト・休み）、その次の席にボタンを置く（仕様 FR-05b）。SB・BB の人が抜けたときは
        # デッドボタン / ボタンを動かさない（`next_hand_button`, オーナー 2026-10-28）。
        self._prev_button = self._button_seat
        self._prev_dead_sb = self._dead_sb if self._hand_id > 1 else None
        self._prev_hand_seats = list(self._hand_seats) if self._hand_id > 1 else []   # 前のハンドがあるときだけ
        natural, dead_sb = next_hand_seating(self._prev_hand_seats, self._button_seat, playing,
                                             self._dead_sb if self._prev_hand_seats else None)
        forced = self._forced_button
        self._forced_button = None
        forced_dead_sb, self._forced_dead_sb = self._forced_dead_sb, None
        self.last_button_override = None
        self._button_by_hand = False
        if forced is not None and forced != natural and (forced in playing or len(playing) >= 3):
            self._button_seat = forced                  # 手動移動（仕様 FR-05g）
            self.last_button_override = (natural, forced)
            self._button_by_hand = True
            dead_sb = None
        else:
            self._button_seat = natural
        if forced_dead_sb is not None:                  # 真のアクションの入力で決めたデッドスモール
            dead_sb = forced_dead_sb
        self._dead_sb = dead_sb if len(playing) >= 3 and (dead_sb is None or dead_sb not in playing) else None
        self._hand_seats = list(playing)
        self._order = seat_order_from_button(playing, self._button_seat)
        self._seat_to_idx = {s: i for i, s in enumerate(self._order)}
        self._idx_to_seat = {i: s for i, s in enumerate(self._order)}
        if self._stackless:
            for s in self._order:
                self._stacks[s] = STACKLESS_STACK
        self._allin_seats = set()
        stacks = [self._stacks[s] for s in self._order]
        self._hand_start_stacks = list(stacks)
        self._state = NoLimitTexasHoldem.create_state(
            self._automations, False, self._raw_antes(stacks), self._blind_tuple(), self._bb, stacks, len(stacks),
        )
        self._hand_active = True
        self._final_pots = []
        logger.info(
            "New hand (pokerkit) started: hand_id=%d button=seat %d (%s)",
            self._hand_id, self._button_seat,
            " ".join(f"{s}:{n}" for s, n in self.position_map().items()),
        )
        return self._hand_id

    def restart_hand_with_button(self, seat: int) -> None:
        """いまのハンドを、同じ席・同じ持ち点・同じブラインドのままボタンだけ変えて始め直す（アクションは空に戻る）。

        ディーラーがボタンを動かし忘れたハンドの組み直しに使う（呼ぶ側がハンドの入力を流し直す, 2026-09-29）。
        次のハンドのボタンはここから進む。ハンドの席でなければ ValueError（状態は変えない）。
        """
        from pokerkit import NoLimitTexasHoldem

        if not self._hand_active or self._state is None:
            raise RuntimeError("restart_hand_with_button called without an active hand")
        if seat not in self._hand_seats:
            raise ValueError(f"席{seat} はこのハンドに配られていません")
        start = {s: self._hand_start_stacks[self._seat_to_idx[s]] for s in self._hand_seats}
        self._button_seat = seat
        self._button_by_hand = True                     # 手で・配った順で直したボタン（席を組み直しても保つ）
        self._dead_sb = None
        self._order = seat_order_from_button(self._hand_seats, seat)
        self._seat_to_idx = {s: i for i, s in enumerate(self._order)}
        self._idx_to_seat = {i: s for i, s in enumerate(self._order)}
        self._hand_start_stacks = [start[s] for s in self._order]
        self._allin_seats = set()
        self._state = NoLimitTexasHoldem.create_state(
            self._automations, False, self._raw_antes(self._hand_start_stacks), self._blind_tuple(), self._bb,
            list(self._hand_start_stacks), len(self._order),
        )
        self._final_pots = []
        logger.info(
            "Hand %d restarted (pokerkit) with button=seat %d (%s)",
            self._hand_id, seat, " ".join(f"{s}:{n}" for s, n in self.position_map().items()),
        )

    def restart_hand_with_blinds(self, sb: int, bb: int, ante: int) -> None:
        """いまのハンドを、同じ席・ボタン・持ち点のままブラインド（と BB アンテ）だけ変えて始め直す（アクションは空に
        戻る）。時計がレベルを越えたあと最初のハンドで、最初のアクションの前にブラインド上昇の宣言があったとき
        （2026-10-28 オーナー）。呼ぶ側がハンドの入力を流し直す。"""
        from pokerkit import NoLimitTexasHoldem

        if not self._hand_active or self._state is None:
            raise RuntimeError("restart_hand_with_blinds called without an active hand")
        if sb <= 0 or bb <= 0 or sb > bb or ante < 0:
            raise ValueError(f"Invalid blinds: sb={sb} bb={bb} ante={ante}")
        self._sb, self._bb = sb, bb
        self._pending_blinds = None
        self._bb_ante_on = int(ante) or False
        self._pending_bb_ante = None
        self._allin_seats = set()
        self._state = NoLimitTexasHoldem.create_state(
            self._automations, False, self._raw_antes(self._hand_start_stacks), self._blind_tuple(), self._bb,
            list(self._hand_start_stacks), len(self._order),
        )
        self._final_pots = []
        logger.info("Hand %d restarted (pokerkit) with blinds %d/%d ante %d", self._hand_id, sb, bb, ante)

    def restart_hand_with_seats(self, seats: list[int], stacks: dict[int, int]) -> None:
        """いまのハンドを、配られた席を `seats` にして始め直す（同じブラインド。アクションは空に戻る, 2026-10-23）。

        手札が載る前にハンドを始めるので、あとから札が載った席（持ち点 0 と誤って記録した人）・札が 1 枚も
        載らない席（空いた席）を配った席に合わせる（店舗 2026-10-19: ボタンが 1 席先に進んだまま 30 ハンド）。
        ボタンは前のハンドのボタンからこの席で進め直す（手で・配った順で直したボタンがこの席にあれば保つ）。
        `stacks` = 新しく入れる席の始めの持ち点（無ければいまの持ち点）。席が 2 つ未満なら ValueError。
        """
        from pokerkit import NoLimitTexasHoldem

        if not self._hand_active or self._state is None:
            raise RuntimeError("restart_hand_with_seats called without an active hand")
        playing = sorted(s for s in set(seats) if s in self._players)
        if len(playing) < 2:
            raise ValueError("配られる席が 2 つ未満です")
        start = {s: self._hand_start_stacks[self._seat_to_idx[s]] for s in self._hand_seats}
        for s in playing:
            if s not in start:
                start[s] = STACKLESS_STACK if self._stackless else int(stacks.get(s, self._stacks[s]))
                self._stacks[s] = start[s]
            if start[s] <= 0:
                raise ValueError(f"席{s} の持ち点がありません")
        if not (self._button_by_hand and (self._button_seat in playing or len(playing) >= 3)):
            self._button_seat, dead_sb = next_hand_seating(getattr(self, "_prev_hand_seats", []), self._prev_button,
                                                           playing, getattr(self, "_prev_dead_sb", None))
            self._dead_sb = dead_sb if len(playing) >= 3 else None
        self._hand_seats = playing
        self._order = seat_order_from_button(playing, self._button_seat)
        self._seat_to_idx = {s: i for i, s in enumerate(self._order)}
        self._idx_to_seat = {i: s for i, s in enumerate(self._order)}
        self._hand_start_stacks = [start[s] for s in self._order]
        self._allin_seats = set()
        self._state = NoLimitTexasHoldem.create_state(
            self._automations, False, self._raw_antes(self._hand_start_stacks), self._blind_tuple(), self._bb,
            list(self._hand_start_stacks), len(self._order),
        )
        self._final_pots = []
        logger.info(
            "Hand %d restarted (pokerkit) with seats %s, button=seat %d (%s)",
            self._hand_id, playing, self._button_seat,
            " ".join(f"{s}:{n}" for s, n in self.position_map().items()),
        )

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

    def end_hand_refund(self) -> dict[int, int]:
        """勝者が決まらないハンドを、チップを動かさずに終える（各席に出した額を返す = 持ち点はハンドの前のまま）。

        ショーダウンで札が読めず勝者が分からないとき（オーナー 2026-09-29: チップ移動は無し）。席 → 返した額。
        """
        st = self._state
        if st is None or not self._hand_active:
            raise RuntimeError("end_hand_refund called without an active hand")
        refunds = {
            s: self._hand_start_stacks[self._seat_to_idx[s]] - st.stacks[self._seat_to_idx[s]]
            for s in self._hand_seats
        }
        self.end_hand_awards({s: amount for s, amount in refunds.items() if amount > 0})
        logger.info("Hand %d ended (pokerkit) without a winner — chips returned: %s", self._hand_id, refunds)
        return refunds

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

    # 組み直しで戻さない、ハンドの外から変える設定（買い足し・休み・次のハンドのブラインドとボタン）。
    # 組み直しはハンドの入力だけを流し直すので、戻すとスナップショットのあとの操作が消える。
    _KEEP_ON_RESTORE = ("_stacks", "_pending_rebuys", "_sitting_out", "_pending_blinds", "_forced_button",
                        "_forced_stacks", "_pending_bb_ante")

    def restore(self, snapshot: dict) -> None:
        """`snapshot()` の時点に戻す（同じオブジェクトのまま = 参照している側はそのまま使える）。

        ハンドの外から変える設定（`_KEEP_ON_RESTORE`）はいまの値のまま。
        """
        kept = {key: copy.deepcopy(self.__dict__[key]) for key in self._KEEP_ON_RESTORE if key in self.__dict__}
        self.__dict__.clear()
        self.__dict__.update(copy.deepcopy(snapshot))
        self.__dict__.update(kept)

    def seats_to_act(self) -> list[int]:
        """このベッティングラウンドでまだ行動する席（手番の順。先頭が actor）。"""
        st = self._state
        if st is None or not self._hand_active:
            return []
        # オールインした席（持ち点を数えないモード）は自動で進めるので行動する席に数えない
        return [self._idx_to_seat[i] for i in st.actor_indices if self._idx_to_seat[i] not in self._allin_seats]

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
        self._auto_act()

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
        elif a == "allin" and self._stackless:
            # 持ち点を数えない: 額は分からないので、内部では最小のレイズ（仮の額）にして、この席に以後手番を回さない
            if st.can_complete_bet_or_raise_to():
                st.complete_bet_or_raise_to(st.min_completion_betting_or_raising_to_amount)
            elif st.can_check_or_call():
                st.check_or_call()
            else:
                raise ValueError("allin not legal")
            self._allin_seats.add(seat)
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
        self._auto_act()

    @property
    def stackless(self) -> bool:
        """持ち点を数えないモードか。"""
        return self._stackless

    def allin_seats(self) -> set[int]:
        """このハンドでオールインした席（持ち点を数えないモードだけ。ほかは空）。"""
        return set(self._allin_seats)

    def _auto_act(self) -> None:
        """持ち点を数えないモード: 手番を回さない席を自動で進める（本物のオールインで pokerkit が手番を飛ばすのと同じ）。

        - オールインした席: コール / チェック（内部の額を合わせるだけ。記録には出ない）。
        - オールインしていない人が手番の人だけで、出す額が無い: チェック（もう賭けられる相手がいない = ボードを開く）。
        """
        st = self._state
        if not self._stackless or st is None or not self._hand_active:
            return
        for _ in range(64):                      # 1 回の操作で進めるのは高々 4 ストリート × 9 席
            if st.actor_index is None:
                return
            seat = self._idx_to_seat[st.actor_index]
            live = [s for s in self._hand_seats if st.statuses[self._seat_to_idx[s]]]
            can_bet = [s for s in live if s not in self._allin_seats and s != seat]
            if seat in self._allin_seats or (not can_bet and st.checking_or_calling_amount == 0):
                st.check_or_call()
            else:
                return

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

        状態が変わらないあいだは同じ答えを返す（engine の定期の確認が毎回呼ぶ。pokerkit の state は操作のたびに
        `operations` が伸び、ハンドの作り直し・巻き戻しでは別の state になる）。
        """
        st = self._state
        key = (len(st.operations), self._hand_active) if st is not None else None
        cached = getattr(self, "_legal_cache", None)
        if cached is not None and cached[0] is st and cached[1] == key:
            return cached[2]
        ctx = self._legal_context(st)
        self._legal_cache = (st, key, ctx)
        return ctx

    def _legal_context(self, st: Any) -> LegalContext:
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
            chip=math.gcd(self._sb, self._bb, CHIP_UNIT),
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
        return position_map(self._hand_seats, self._button_seat, dead_sb=self.dead_sb_seat is not None)

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
                self._auto_act()
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

    def is_sitting_out(self, seat: int) -> bool:
        """操作で休みにした席か（スタック 0 で配られない席は含まない）。"""
        return seat in self._sitting_out

    def sit_in(self, seat: int) -> bool:
        """休みの席を戻す（次のハンドから配られる。スタック 0 なら買い足すまで配られない）。変わったら True。

        卓に無い席（1〜9）は足す（途中から座った人, オーナー 2026-10-26: 店舗 10/26 は席 7 を足せなかった）。持ち点を
        数えないモードではそのまま配られ、数えるモードでは持ち点 0 = 買い足す（`rebuy`）まで配られない。
        """
        if seat not in self._players:
            self.add_seat(seat)
            return True
        if seat not in self._sitting_out:
            return False
        self._sitting_out.discard(seat)
        return True

    def add_seat(self, seat: int, name: Optional[str] = None) -> None:
        """卓に無い席を足す（次のハンドから。持ち点 0、持ち点を数えないモードでは数えない）。1〜9 以外は ValueError。"""
        if isinstance(seat, bool) or not isinstance(seat, int) or not 1 <= seat <= 9:
            raise ValueError(f"Unknown seat: {seat}")
        if seat in self._players:
            return
        self._players[seat] = PlayerState(seat=seat, name=name or f"Player{seat}", stack=0)
        self._seats = sorted(self._players)
        self._stacks[seat] = 0
        logger.info("席 %d を足しました（次のハンドから）", seat)

    def set_blinds(self, sb: int, bb: int) -> None:
        """ブラインドを変える（トーナメントのレベル上昇）。ハンドの途中なら次のハンドから。"""
        if sb <= 0 or bb <= 0 or sb > bb:
            raise ValueError(f"Invalid blinds: sb={sb} bb={bb}")
        if self._hand_active:
            self._pending_blinds = (sb, bb)
        else:
            self._sb, self._bb = sb, bb
            self._pending_blinds = None

    def set_bb_ante(self, on: "bool | int") -> None:
        """BB アンティを入れる・外す: True = BB と同額 / 整数 = その額（トーナメントのレベル, ADR-0071）/ False・0 = 無し。
        ハンドの途中なら次のハンドから。"""
        if isinstance(on, bool):
            value: "bool | int" = on
        else:
            value = int(on)
            if value < 0:
                raise ValueError(f"Invalid ante: {on}")
            value = value or False
        if self._hand_active:
            self._pending_bb_ante = value
        else:
            self._bb_ante_on = value
            self._pending_bb_ante = None

    @staticmethod
    def _ante_amount(setting: "bool | int", bb: int) -> int:
        if isinstance(setting, bool):
            return bb if setting else 0
        return int(setting)

    @property
    def bb_ante(self) -> int:
        """いまの（最後の）ハンドの BB アンティの額（無ければ 0）。BB の人の持ち点が足りず減らしたときは払った額。"""
        if self._state is not None:
            return self._posted_ante
        return self._ante_amount(self._bb_ante_on, self._bb)

    def bb_ante_next_hand(self) -> bool:
        """次のハンドで BB アンティが入っているか（保留の切り替えを含む）。"""
        setting = self._bb_ante_on if self._pending_bb_ante is None else self._pending_bb_ante
        return bool(setting)

    def _raw_antes(self, stacks: list[int]):
        """pokerkit の `raw_antes`: BB アンティは {1: 額}（インデックス 1 = BB。heads-up も pokerkit が BB に付ける）。
        アンティの返金（`ante_trimming_status`）は切って渡す（切らないと BB だけのアンティが返される）。

        BB の人の持ち点がブラインドとアンティの両方に足りなければ、ブラインドを先に払う（TDA の規則。pokerkit はアンティを
        先に取るので、アンティを持ち点からブラインドを引いた残りまでに減らす, ADR-0071）。`stacks` は pokerkit の並び
        （heads-up は 0 番 = BB、3 人以上は 1 番 = BB）。"""
        ante = self._ante_amount(self._bb_ante_on, self._bb)
        bb_index = 0 if self._dead_sb is not None and len(stacks) >= 3 else 1
        if ante and stacks:
            bb_stack = stacks[0] if len(stacks) == 2 else stacks[bb_index]
            ante = min(ante, max(0, bb_stack - self._bb))
        self._posted_ante = ante
        return {bb_index: ante} if ante else 0

    def _blind_tuple(self) -> tuple[int, int]:
        """pokerkit の `raw_blinds_or_straddles`。デッドスモールのハンドは並びの先頭（ボタンの次）が BB で、BB だけを払う
        （pokerkit は一番大きいブラインドの次の人から始める）。"""
        if self._dead_sb is not None and len(self._order) >= 3:
            return (self._bb, 0)
        return (self._sb, self._bb)

    @property
    def dead_sb_seat(self) -> Optional[int]:
        """このハンドのデッドスモール（空いた SB の席）。無ければ None。"""
        return self._dead_sb if self._hand_seats and len(self._hand_seats) >= 3 else None

    def set_dead_sb(self, seat: Optional[int]) -> None:
        """次のハンドをデッドスモールで始める（真のアクションの入力・記録の再生で、記録のデッドスモールに合わせる）。"""
        self._forced_dead_sb = seat

    def set_button(self, seat: int) -> None:
        """次のハンドのボタンを手で指定する（ディーラーの手違い等の修正, 仕様 FR-05g）。空いた席（1〜9）も指定できる
        = デッドボタン（3 人以上のとき, 2026-10-28）。"""
        if seat not in self._players and (isinstance(seat, bool) or not isinstance(seat, int) or not 1 <= seat <= 9):
            raise ValueError(f"Unknown seat: {seat}")
        self._forced_button = seat

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
            self.add_seat(seat)                 # 途中から座った人の買い込み（`r <席> <額>`, 2026-10-26）
        if amount <= 0:
            raise ValueError(f"Rebuy amount must be positive, got {amount}")
        if self._hand_active and seat in self._hand_seats:
            # ハンドの席のスタックはハンドの終わりに書き直すので、いま足すと消える → 次のハンドの始めに足す
            logger.warning("rebuy mid-hand (pokerkit): 次ハンドから反映 (seat=%d)", seat)
            self._pending_rebuys[seat] = self._pending_rebuys.get(seat, 0) + amount
            return
        self._stacks[seat] += amount

    def stacks_before_next_hand(self) -> dict[int, int]:
        """次のハンドの始めの持ち点（ブラインドの前）: いまの持ち点に、台本で決めた持ち点（`force_next_stacks`）を
        重ねたもの。記録の `stack_start` に使う。"""
        if self._stackless:
            return {s: STACKLESS_STACK for s in self._seats}       # 毎ハンド同じ持ち点で始める
        stacks = self.get_stacks()
        if self._forced_stacks:
            stacks.update(self._forced_stacks)
        return stacks

    def force_next_stacks(self, stacks: dict[int, int]) -> None:
        """次のハンドを、この持ち点で始める（台本のハンド。前のハンドがまだ確定していなくても、その結果より
        優先する）。卓に無い席・負の値は ValueError（何も変えない）。"""
        for seat, stack in stacks.items():
            if seat not in self._players:
                raise ValueError(f"Unknown seat: {seat}")
            if stack < 0:
                raise ValueError(f"Stack must be non-negative, got {stack}")
        self._forced_stacks = {int(s): int(v) for s, v in stacks.items()}

    def set_stacks(self, stacks: dict[int, int]) -> None:
        """次のハンドの持ち点を決める（評価で各ハンドを記録の持ち点から始める = `replay_events(hand_stacks=...)`）。

        ハンドの途中の買い足しの保留は捨てる（記録の持ち点に入っている）。
        """
        for seat, stack in stacks.items():
            if seat in self._players and stack >= 0:
                self._stacks[seat] = int(stack)
                self._pending_rebuys.pop(seat, None)

    def _apply_pending_rebuys(self) -> None:
        for seat, amount in self._pending_rebuys.items():
            self._stacks[seat] += amount
        self._pending_rebuys = {}
