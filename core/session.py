"""core/session.py

Phase S2: session レイヤと hand-based seating の最小ドメインモデル。

3 つの contract model（`docs/contracts/session-seating.md` / ADR-0006）に対応する:

- ``Session``        … 1 卓 1 回の運営単位（player membership は持たない）。
- ``SeatAssignment`` … hand-based の seat→player スナップショット 1 行（standalone 永続形）。
- ``HandRef``        … ledger app が hand を後から参照する不変軽量参照（denormalized snapshot 込み）。

業務ルール（重複・状態遷移・registry 実在性）は ``core/session_repository.py`` が
source of truth として enforce する。本モジュールは純粋なデータ構造に徹する。

各 ``to_dict`` は対応 schema の ``additionalProperties: false`` に合わせ、absent な
optional フィールドはキーごと省略する（open 中は ``ended_at`` 不在 等）。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Session:
    """1 卓 1 回の運営単位。

    属性:
        session_id: 永続・安定な session 識別子（session レイヤが採番する UUID hex）。
        started_at: 開始時刻（ISO 8601）。
        status:     ``"open"`` | ``"closed"``。
        label:      任意の表示名（識別には使わない）。
        ended_at:   終了時刻。``open`` の間は None。``closed`` では必須（core が enforce）。
        blinds:     任意の参考値 ``{"sb": int, "bb": int}``。
    """

    session_id: str
    started_at: str
    status: str
    label: str | None = None
    ended_at: str | None = None
    blinds: dict | None = None

    def to_dict(self) -> dict:
        d: dict = {
            "session_id": self.session_id,
            "started_at": self.started_at,
            "status": self.status,
        }
        if self.label is not None:
            d["label"] = self.label
        if self.ended_at is not None:
            d["ended_at"] = self.ended_at
        if self.blinds is not None:
            d["blinds"] = dict(self.blinds)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Session":
        return cls(
            session_id=d["session_id"],
            started_at=d["started_at"],
            status=d["status"],
            label=d.get("label"),
            ended_at=d.get("ended_at"),
            blinds=d.get("blinds"),
        )


@dataclass
class SeatAssignment:
    """あるハンド ``(session_id, hand_id)`` のある席 ``seat_no`` への 1 player 割り当て。

    空席は行を作らない（partial assignment）。``status`` 不在は ``active`` 扱い。
    """

    session_id: str
    hand_id: int
    seat_no: int
    player_id: str
    status: str | None = None

    def to_dict(self) -> dict:
        d: dict = {
            "session_id": self.session_id,
            "hand_id": self.hand_id,
            "seat_no": self.seat_no,
            "player_id": self.player_id,
        }
        if self.status is not None:
            d["status"] = self.status
        return d

    def to_embedded(self) -> dict:
        """``hand_ref.seat_assignments`` 用の denormalized 形（session_id/hand_id を除く）。"""
        d: dict = {"seat_no": self.seat_no, "player_id": self.player_id}
        if self.status is not None:
            d["status"] = self.status
        return d


@dataclass
class HandRef:
    """ledger app が hand logger の 1 ハンドを参照する不変軽量参照。

    ``(session_id, hand_id)`` が複合一意キー（ADR-0006）。``seat_assignments`` は
    hand 開始時点の seat→player スナップショット（denormalized, 空配列可）。
    """

    session_id: str
    hand_id: int
    started_at: str
    seat_assignments: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "hand_id": self.hand_id,
            "started_at": self.started_at,
            "seat_assignments": [dict(sa) for sa in self.seat_assignments],
        }
