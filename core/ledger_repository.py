"""core/ledger_repository.py

Phase S3a (M4, ADR-0014): cash-only ledger の永続化 + 業務ルール。

`docs/contracts/ledger.md` / `validation-rules.md` / `error-shapes.md` の契約に対する core 実装。
**業務ルールはこの repository が source of truth**（front-end は結果と error code を表示するだけ）。

永続化は players/sessions と同じ単一 JSON + アトミックリネーム:

    {"entries": [{entry_id, session_id, player_id, kind, occurred_at,
                  cash_amount, point_amount, note?, order?}, ...]}

S3a の制約: point_amount != 0 は reject（points_not_supported, M6/ISSUE-0001 で解放）。
entry は append-only（編集・削除なし。訂正は adjustment を追記）。
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path

from core.ledger import LedgerEntry, OrderDetail
from core.player_repository import PlayerNotFoundError
from core.session_repository import SessionRepository

logger = logging.getLogger(__name__)

_DEFAULT_LEDGER_DB = Path(__file__).parent.parent / "ledger.json"

_VALID_KINDS = ("buy_in", "rebuy", "add_on", "order", "adjustment")
_BUYIN_KINDS = ("buy_in", "rebuy", "add_on")


class LedgerError(Exception):
    """ledger 操作の基底例外。"""


class LedgerSessionClosedError(LedgerError):
    """closed の session に entry を追加しようとした（error code: session_closed）。"""


class LedgerUnknownPlayerError(LedgerError):
    """指定 player_id が registry に実在しない（error code: unknown_player）。"""


class InvalidKindError(LedgerError):
    """kind が 5 種別以外（error code: invalid_kind）。"""


class InvalidAmountError(LedgerError):
    """kind 別の金額規則違反（error code: invalid_amount）。"""


class PointsNotSupportedError(LedgerError):
    """S3a では point_amount != 0 を受け付けない（error code: points_not_supported）。"""


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class LedgerRepository:
    """ledger entry の永続ストア（append-only）。

    session の実在/open 判定に ``session_repo`` を、player の実在判定にその registry を参照する。
    unknown session は session 側の ``SessionNotFoundError``（code: not_found）を透過する。
    """

    def __init__(
        self,
        path: str | Path | None = None,
        session_repo: SessionRepository | None = None,
    ) -> None:
        self._path = Path(path) if path is not None else _DEFAULT_LEDGER_DB
        self._session_repo = session_repo if session_repo is not None else SessionRepository()
        self._entries: list[LedgerEntry] = []
        self._load()

    # ――― 永続化 ―――

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            with self._path.open(encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Could not load ledger DB (%s), starting empty.", e)
            return
        for raw in data.get("entries", []):
            try:
                self._entries.append(LedgerEntry.from_dict(raw))
            except (KeyError, TypeError):
                logger.warning("Skipping malformed ledger record: %r", raw)

    def _flush(self) -> None:
        """アトミックリネームで書き込む。失敗してもクラッシュしない。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_suffix(".tmp")
        try:
            with tmp_path.open("w", encoding="utf-8") as f:
                json.dump(
                    {"entries": [e.to_dict() for e in self._entries]},
                    f, ensure_ascii=False, indent=2,
                )
            os.replace(tmp_path, self._path)
        except OSError:
            logger.exception("Failed to write ledger DB: %s", self._path)
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)

    # ――― validation ―――

    def _validate(
        self,
        session_id: str,
        player_id: str,
        kind: str,
        cash_amount: int,
        point_amount: int,
        order: OrderDetail | None,
    ) -> None:
        session = self._session_repo.get_session(session_id)  # not_found を透過
        if session.status == "closed":
            raise LedgerSessionClosedError(
                f"session_id={session_id} は closed です（訂正は S4 settlement の責務）。"
            )
        try:
            self._session_repo.get_player(player_id)
        except PlayerNotFoundError as e:
            raise LedgerUnknownPlayerError(
                f"player_id={player_id} は registry に存在しません。"
            ) from e
        if kind not in _VALID_KINDS:
            raise InvalidKindError(f"kind={kind!r} は {_VALID_KINDS} のいずれかである必要があります。")
        if point_amount != 0:
            raise PointsNotSupportedError(
                "point 払いは未対応です（S3a cash-only, ADR-0014。M6 で解放予定）。"
            )
        if not isinstance(cash_amount, int) or isinstance(cash_amount, bool):
            raise InvalidAmountError(f"cash_amount={cash_amount!r} は整数である必要があります。")

        if kind in _BUYIN_KINDS:
            if order is not None:
                raise InvalidAmountError(f"kind={kind} に order 明細は付けられません。")
            if cash_amount <= 0:
                raise InvalidAmountError(f"kind={kind} の cash_amount は正の整数が必要です。")
        elif kind == "order":
            if order is None:
                raise InvalidAmountError("kind=order には item_name / unit_amount / quantity が必要です。")
            if not order.item_name.strip():
                raise InvalidAmountError("order の item_name が空です。")
            if order.unit_amount < 0 or order.quantity < 1:
                raise InvalidAmountError(
                    f"order の unit_amount は 0 以上 / quantity は 1 以上が必要です "
                    f"(unit_amount={order.unit_amount}, quantity={order.quantity})。"
                )
            if cash_amount != order.unit_amount * order.quantity:
                raise InvalidAmountError(
                    f"cash_amount={cash_amount} が unit_amount×quantity="
                    f"{order.unit_amount * order.quantity} と一致しません。"
                )
        else:  # adjustment
            if order is not None:
                raise InvalidAmountError("kind=adjustment に order 明細は付けられません。")
            if cash_amount == 0:
                raise InvalidAmountError("adjustment の cash_amount は 0 以外が必要です。")

    # ――― 操作 ―――

    def add_entry(
        self,
        session_id: str,
        player_id: str,
        kind: str,
        cash_amount: int,
        point_amount: int = 0,
        note: str | None = None,
        item_name: str | None = None,
        unit_amount: int | None = None,
        quantity: int | None = None,
    ) -> LedgerEntry:
        """entry を 1 件追記する（kind=order は item_name/unit_amount/quantity 必須）。"""
        order: OrderDetail | None = None
        if item_name is not None or unit_amount is not None or quantity is not None:
            if item_name is None or unit_amount is None or quantity is None:
                raise InvalidAmountError(
                    "order 明細は item_name / unit_amount / quantity を揃えて指定してください。"
                )
            order = OrderDetail(item_name=item_name, unit_amount=unit_amount, quantity=quantity)

        self._validate(session_id, player_id, kind, cash_amount, point_amount, order)

        entry = LedgerEntry(
            entry_id=uuid.uuid4().hex,
            session_id=session_id,
            player_id=player_id,
            kind=kind,
            occurred_at=_now_iso(),
            cash_amount=cash_amount,
            point_amount=point_amount,
            note=note,
            order=order,
        )
        self._entries.append(entry)
        self._flush()
        logger.info(
            "Ledger entry added: %s %s cash=%d (session=%s player=%s)",
            entry.entry_id, kind, cash_amount, session_id, player_id,
        )
        return entry

    def list_entries(self, session_id: str, player_id: str | None = None) -> list[LedgerEntry]:
        """session の entry を追記順で返す（player_id 指定で絞り込み）。"""
        self._session_repo.get_session(session_id)  # not_found を透過
        return [
            e for e in self._entries
            if e.session_id == session_id
            and (player_id is None or e.player_id == player_id)
        ]

    def session_player_summary(self, session_id: str, player_id: str) -> dict:
        """player の session 中間集計（確定値ではない。確定は S4 settlement）。"""
        try:
            self._session_repo.get_player(player_id)
        except PlayerNotFoundError as e:
            raise LedgerUnknownPlayerError(
                f"player_id={player_id} は registry に存在しません。"
            ) from e
        entries = self.list_entries(session_id, player_id)
        buy_in_total = sum(e.cash_amount for e in entries if e.kind in _BUYIN_KINDS)
        order_total = sum(e.cash_amount for e in entries if e.kind == "order")
        adjustment_total = sum(e.cash_amount for e in entries if e.kind == "adjustment")
        return {
            "buy_in_total": buy_in_total,
            "order_total": order_total,
            "adjustment_total": adjustment_total,
            "total_due": buy_in_total + order_total + adjustment_total,
        }
