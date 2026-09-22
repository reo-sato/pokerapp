"""core/table_state.py

**RFID だけから導く卓の状態**（カード / 有効席 / ストリート）。アクション推定には依存しない。

狙い（実プレイ環境での検証, ADR-0056 D4/D5）:

- **カード読み取り** — どの席に何が載っているか、ボードに何が並んでいるか。
- **有効席** — 仕様 FR-35 の `active_seats`。ただし ADR-0056 D4 のとおり
  「カードが載っている = ゲームに残っている」ではない（プレイヤーは札を持ち上げる）ので、
  **1 つの真偽値に潰さず**「配られた / いま載っている / 何秒離れている / fold らしい」を分けて出す。
- **ストリート遷移** — ボード枚数 3/4/5。pokerkit backend ではストリートはベッティングで進むため
  `advance_street` は no-op（契約上そうなっている）。ここで出すのは **RFID から見た**ストリートで、
  engine のストリートとは別物として両方を表示する（食い違いこそ見たい情報）。

本モジュールは**純粋**（I/O もスレッドも持たない）。スナップショットの生成だけを行い、
書き出しは `integration/engine.py`、表示は `tools/table_monitor.py` が担う。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# ボード枚数 → RFID から見たストリート。
_BOARD_STREET = {0: "preflop", 1: "preflop", 2: "preflop", 3: "flop", 4: "turn", 5: "river"}

# 席の札がこれ以上離れていたら「fold らしい」と**表示**する（判定ではない, ADR-0056 D4）。
# プレイヤーが札を持ち上げて見る時間より十分長く取る。実測して調整する前提の暫定値。
DEFAULT_FOLD_HINT_SEC = 20.0


def derive_street(board_count: int) -> str:
    """ボード枚数から RFID 由来のストリートを返す（5 枚超は river 扱い）。"""
    if board_count < 0:
        return "preflop"
    return _BOARD_STREET.get(board_count, "river")


@dataclass
class SeatState:
    """1 席の観測状態。`present` と `likely_folded` を混同しないこと。"""

    seat: int
    dealt_in: bool               # このハンドで札を受け取ったか（一度でも検出されたか）
    present: bool                # いま札がリーダー上にあるか
    cards: list[str] = field(default_factory=list)   # 読めたカード（最大 2 枚）
    away_sec: Optional[float] = None                 # 離れている秒数（載っていれば None）
    likely_folded: bool = False                      # away_sec > しきい値 の **表示上の推測**
    position: str = ""           # BTN/SB/BB/UTG…（ボタンから導出。持たない backend では空）

    def to_dict(self) -> dict:
        return {
            "seat": self.seat,
            "dealt_in": self.dealt_in,
            "present": self.present,
            "cards": list(self.cards),
            "away_sec": round(self.away_sec, 1) if self.away_sec is not None else None,
            "likely_folded": self.likely_folded,
            "position": self.position,
        }


@dataclass
class TableState:
    """ある時点の卓の観測状態（RFID 由来のみ）。"""

    session_id: str
    hand_id: int
    updated_at: str                                  # ISO 8601（この snapshot を作った時刻）
    seats: list[SeatState] = field(default_factory=list)
    board: list[str] = field(default_factory=list)   # 位置順
    board_timeline: list[dict] = field(default_factory=list)  # [{index, card, dealt_at}]
    rfid_street: str = "preflop"                     # ボード枚数から導いたストリート
    engine_street: str = ""                          # engine のストリート（比較用）
    # ボタン席（engine 由来, ISSUE-0032）。`engine_street` と同じく **比較・確認のための表示**で、
    # ハンドごとに回っていることを卓の脇から目視できるようにする。持たない backend では None。
    button_seat: Optional[int] = None
    dealt_in_seats: list[int] = field(default_factory=list)
    present_seats: list[int] = field(default_factory=list)
    likely_folded_seats: list[int] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "hand_id": self.hand_id,
            "updated_at": self.updated_at,
            "seats": [s.to_dict() for s in self.seats],
            "board": list(self.board),
            "board_timeline": list(self.board_timeline),
            "rfid_street": self.rfid_street,
            "engine_street": self.engine_street,
            "button_seat": self.button_seat,
            "dealt_in_seats": list(self.dealt_in_seats),
            "present_seats": list(self.present_seats),
            "likely_folded_seats": list(self.likely_folded_seats),
        }


def build_table_state(
    *,
    session_id: str,
    hand_id: int,
    now: float,
    updated_at: str,
    seats: list[int],
    hole_cards: dict[int, list[str]],
    presence: dict[int, dict],
    board: list[str],
    board_timeline: list[dict],
    engine_street: str = "",
    button_seat: Optional[int] = None,
    position_map: Optional[dict[int, str]] = None,
    fold_hint_sec: float = DEFAULT_FOLD_HINT_SEC,
) -> TableState:
    """観測から `TableState` を組み立てる（純粋関数）。

    Args:
        seats:        卓の全席番号（着席している席）。
        hole_cards:   seat → 読めたカード（engine が蓄積したもの）。
        presence:     seat → `{"present": bool, "absent_since": float | None}`
                      （`RFIDThread.presence_snapshot()` の形）。RFID 無しなら空 dict。
        board:        位置順のボードカード。
        engine_street: engine 側のストリート（比較表示用。空可）。
        button_seat / position_map: engine 側のボタンとポジション名（表示用。無ければ None/空）。
        fold_hint_sec: 「fold らしい」と表示するまでの不在秒数。

    `dealt_in` は **カードが読めたか**（`hole_cards`）または **一度でも検出されたか**
    （`presence` に現れたか）で判定する。カードマスター未登録の札でも検出はされるため、
    「配られたのにカード名が出ない」= 未登録のサインとして両方を出す。
    """
    positions = position_map or {}
    seat_states: list[SeatState] = []
    for seat in sorted(seats):
        p = presence.get(seat) or {}
        cards = list(hole_cards.get(seat, []))
        present = bool(p.get("present"))
        absent_since = p.get("absent_since")
        seen = seat in presence and (present or absent_since is not None)
        away = (now - absent_since) if (not present and absent_since is not None) else None
        seat_states.append(SeatState(
            seat=seat,
            dealt_in=bool(cards) or seen,
            present=present,
            cards=cards,
            away_sec=away,
            likely_folded=bool(away is not None and away > fold_hint_sec),
            position=positions.get(seat, ""),
        ))

    return TableState(
        session_id=session_id,
        hand_id=hand_id,
        updated_at=updated_at,
        seats=seat_states,
        board=list(board),
        board_timeline=list(board_timeline),
        rfid_street=derive_street(len(board)),
        engine_street=engine_street,
        button_seat=button_seat,
        dealt_in_seats=[s.seat for s in seat_states if s.dealt_in],
        present_seats=[s.seat for s in seat_states if s.present],
        likely_folded_seats=[s.seat for s in seat_states if s.likely_folded],
    )
