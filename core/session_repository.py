"""core/session_repository.py

Phase S2: session レイヤと hand-based seating の永続化 + 業務ルール。

`docs/contracts/session-seating.md`（ADR-0006）/ `repository-interfaces.md` /
`error-shapes.md` / `validation-rules.md` の契約に対する core 実装。**業務ルールは
この repository が source of truth**（front-end は結果と error code を表示するだけ）。

永続化は player registry / JsonWriter と同じスタイルの単一 JSON ファイル +
アトミックリネーム。session の hand-based seat assignment を session 配下に入れ子で
持つ（将来 ledger entry を同じ session 配下に additive 拡張しやすい配置）:

    {
      "sessions": [
        {
          "session_id": "...", "started_at": "...", "status": "open",
          "label": "...", "ended_at": "...", "blinds": {...},
          "hands": {
            "12": {"started_at": "...", "seats": [{seat_no, player_id, status?}, ...]}
          }
        }
      ]
    }

ISSUE-0005 の決着（S2 core）: session_id は session レイヤが UUID4 hex で採番し
（hand logger の timestamp session_id とは独立）、seat_assignment は hand logger の
hand JSON ではなく本 repository 専用ストア（既定 `sessions.json`）に持つ。詳細は
`docs/adr/0007-s2-session-layer-persistence-and-id-issuance.md`。
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path

from core.player_repository import PlayerNotFoundError, PlayerRepository
from core.session import HandRef, SeatAssignment, Session

logger = logging.getLogger(__name__)

_DEFAULT_SESSION_DB = Path(__file__).parent.parent / "sessions.json"

_MIN_SEAT_NO = 1
_MAX_SEAT_NO = 9


class SessionError(Exception):
    """session / seating 操作の基底例外。"""


class SessionNotFoundError(SessionError):
    """指定された session_id / hand が存在しない（error code: not_found）。"""


class SessionAlreadyClosedError(SessionError):
    """既に closed の session を再度 close しようとした（error code: already_closed）。"""


class SessionClosedError(SessionError):
    """closed の session に seat を割り当てようとした（error code: session_closed）。"""


class SeatTakenError(SessionError):
    """同一 hand 内で seat が既に埋まっている（error code: seat_taken）。"""


class PlayerAlreadySeatedError(SessionError):
    """同一 hand 内で player が既に別の席に着いている（error code: player_already_seated）。"""


class UnknownPlayerError(SessionError):
    """指定 player_id が registry に実在しない（error code: unknown_player）。"""


class InvalidSeatError(SessionError):
    """seat_no が範囲外（1..9 外）（error code: invalid_seat）。"""


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class SessionRepository:
    """session と hand-based seat assignment の永続ストア。

    unknown_player の判定に player registry を参照する。``player_repo`` を渡さない場合は
    既定の `players.json` を読む ``PlayerRepository`` を構築する。
    """

    def __init__(
        self,
        path: str | Path | None = None,
        player_repo: PlayerRepository | None = None,
    ) -> None:
        self._path = Path(path) if path is not None else _DEFAULT_SESSION_DB
        self._player_repo = player_repo if player_repo is not None else PlayerRepository()
        self._sessions: dict[str, Session] = {}
        # session_id -> {hand_id(int) -> {"started_at": str, "seats": list[SeatAssignment]}}
        self._hands: dict[str, dict[int, dict]] = {}
        self._load()

    # ――― 永続化 ―――

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            with self._path.open(encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Could not load session DB (%s), starting empty.", e)
            return
        for raw in data.get("sessions", []):
            try:
                session = Session.from_dict(raw)
            except (KeyError, TypeError):
                logger.warning("Skipping malformed session record: %r", raw)
                continue
            self._sessions[session.session_id] = session
            hands: dict[int, dict] = {}
            for hand_key, hand_raw in raw.get("hands", {}).items():
                try:
                    hand_id = int(hand_key)
                except (TypeError, ValueError):
                    logger.warning("Skipping malformed hand key: %r", hand_key)
                    continue
                seats = [
                    SeatAssignment(
                        session_id=session.session_id,
                        hand_id=hand_id,
                        seat_no=s["seat_no"],
                        player_id=s["player_id"],
                        status=s.get("status"),
                    )
                    for s in hand_raw.get("seats", [])
                ]
                hands[hand_id] = {
                    "started_at": hand_raw.get("started_at", ""),
                    "seats": seats,
                }
            self._hands[session.session_id] = hands

    def _flush(self) -> None:
        """アトミックリネームで書き込む。失敗してもクラッシュしない。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_suffix(".tmp")
        sessions_out = []
        for session in self._sessions.values():
            entry = session.to_dict()
            hands_out: dict[str, dict] = {}
            for hand_id, hand in self._hands.get(session.session_id, {}).items():
                hands_out[str(hand_id)] = {
                    "started_at": hand["started_at"],
                    "seats": [sa.to_embedded() for sa in hand["seats"]],
                }
            entry["hands"] = hands_out
            sessions_out.append(entry)
        try:
            with tmp_path.open("w", encoding="utf-8") as f:
                json.dump({"sessions": sessions_out}, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self._path)
        except OSError:
            logger.exception("Failed to write session DB: %s", self._path)
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)

    def reload(self) -> None:
        """ディスクから session / seating を再読込する（read-only viewer 用）。

        別プロセス（hand logger 等）が `sessions.json` を更新した場合に最新状態を取り込む。
        業務ルールには影響しない、純粋な再読込のみ（`_player_repo` は別途 reload する）。
        """
        self._sessions.clear()
        self._hands.clear()
        self._load()

    # ――― session CRUD ―――

    def create_session(self, label: str | None = None, blinds: dict | None = None) -> Session:
        session_id = uuid.uuid4().hex
        session = Session(
            session_id=session_id,
            started_at=_now_iso(),
            status="open",
            label=label,
            blinds=blinds,
        )
        self._sessions[session_id] = session
        self._hands[session_id] = {}
        self._flush()
        logger.info("Created session %s (%s)", session_id, label or "")
        return session

    def list_sessions(self) -> list[Session]:
        """全 session を作成順で返す。"""
        return list(self._sessions.values())

    def get_session(self, session_id: str) -> Session:
        session = self._sessions.get(session_id)
        if session is None:
            raise SessionNotFoundError(f"session_id={session_id} は存在しません。")
        return session

    def close_session(self, session_id: str, ended_at: str | None = None) -> Session:
        session = self.get_session(session_id)
        if session.status == "closed":
            raise SessionAlreadyClosedError(f"session_id={session_id} は既に closed です。")
        session.status = "closed"
        session.ended_at = ended_at if ended_at is not None else _now_iso()
        self._flush()
        logger.info("Closed session %s", session_id)
        return session

    # ――― hand-based seating ―――

    def assign_seat(
        self, session_id: str, hand_id: int, seat_no: int, player_id: str
    ) -> SeatAssignment:
        """あるハンドのある席に player を割り当てる。

        reject: unknown session / closed session / invalid seat / unknown player /
        seat taken / player already seated（いずれも `error-shapes.md` の code に対応）。
        """
        session = self.get_session(session_id)
        if session.status == "closed":
            raise SessionClosedError(f"session_id={session_id} は closed です。")
        if not isinstance(seat_no, int) or not (_MIN_SEAT_NO <= seat_no <= _MAX_SEAT_NO):
            raise InvalidSeatError(f"seat_no={seat_no!r} は範囲外です（{_MIN_SEAT_NO}..{_MAX_SEAT_NO}）。")
        if not isinstance(hand_id, int) or hand_id < 0:
            raise InvalidSeatError(f"hand_id={hand_id!r} は 0 以上の整数である必要があります。")
        try:
            self._player_repo.get(player_id)
        except PlayerNotFoundError as e:
            raise UnknownPlayerError(f"player_id={player_id} は registry に存在しません。") from e

        hand = self._hands.setdefault(session_id, {}).setdefault(
            hand_id, {"started_at": _now_iso(), "seats": []}
        )
        for existing in hand["seats"]:
            if existing.seat_no == seat_no:
                raise SeatTakenError(
                    f"seat_no={seat_no} は hand_id={hand_id} で既に埋まっています。"
                )
            if existing.player_id == player_id:
                raise PlayerAlreadySeatedError(
                    f"player_id={player_id} は hand_id={hand_id} で既に着席しています。"
                )

        assignment = SeatAssignment(
            session_id=session_id, hand_id=hand_id, seat_no=seat_no, player_id=player_id
        )
        hand["seats"].append(assignment)
        self._flush()
        logger.info(
            "Assigned seat %d to player %s (session=%s hand=%d)",
            seat_no, player_id, session_id, hand_id,
        )
        return assignment

    def list_hand_ids(self, session_id: str) -> list[int]:
        """ある session に記録済みの hand_id を昇順で返す（hand が無ければ空 list）。

        read-only viewer が「hand ごとの seat assignment」を列挙するための enumerator。
        unknown session は `not_found`（兄弟 read API と同じ）。
        """
        self.get_session(session_id)
        return sorted(self._hands.get(session_id, {}).keys())

    def list_seat_assignments(self, session_id: str, hand_id: int) -> list[SeatAssignment]:
        """あるハンドの seat assignment を seat_no 昇順で返す（空ハンドは空 list）。"""
        self.get_session(session_id)
        hand = self._hands.get(session_id, {}).get(hand_id)
        if hand is None:
            return []
        return sorted(hand["seats"], key=lambda sa: sa.seat_no)

    def resolve_seat_map_for_hand(self, session_id: str, hand_id: int) -> dict[int, str]:
        """あるハンドの ``seat_no -> player_id`` マップを返す。"""
        return {
            sa.seat_no: sa.player_id
            for sa in self.list_seat_assignments(session_id, hand_id)
        }

    def resolve_hand_ref(self, session_id: str, hand_id: int) -> HandRef:
        """あるハンドの cross-app 参照 ``HandRef``（snapshot 込み）を返す。

        記録の無い hand は not_found（既存 code を再利用）。
        """
        self.get_session(session_id)
        hand = self._hands.get(session_id, {}).get(hand_id)
        if hand is None:
            raise SessionNotFoundError(
                f"hand_id={hand_id} は session_id={session_id} に存在しません。"
            )
        seats = sorted(hand["seats"], key=lambda sa: sa.seat_no)
        return HandRef(
            session_id=session_id,
            hand_id=hand_id,
            started_at=hand["started_at"],
            seat_assignments=[sa.to_embedded() for sa in seats],
        )

    def current_seating(self, session_id: str) -> list[SeatAssignment]:
        """最新 hand から現在の seating を導出する（hand が無ければ空 list）。"""
        self.get_session(session_id)
        hands = self._hands.get(session_id, {})
        if not hands:
            return []
        latest = max(hands)
        return sorted(hands[latest]["seats"], key=lambda sa: sa.seat_no)
