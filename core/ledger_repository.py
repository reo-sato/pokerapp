"""core/ledger_repository.py

Phase S3: session ledger / point ledger の永続化 + 業務ルール。

`docs/contracts/ledger-points.md`（ADR-0013）/ `repository-interfaces.md` /
`error-shapes.md` / `validation-rules.md` の契約に対する core 実装。**業務ルールは
この repository が source of truth**（front-end は結果と error code を表示するだけ）。

ISSUE-0001 の決着（ADR-0013）:
- **point 残高の source of truth は point_ledger_entry の fold**（追記列の総和）。
  cached 残高カラムは持たない（必要なら将来 derived cache を additive に追加）。
- point 充当付き entry を追加すると、対応する spend 系 ``PointLedgerEntry`` を
  **core が同時生成** する（`related_ledger_entry_id` で back-link）。front-end が
  spend entry を直接書くことはない。
- grant の冪等性は任意の ``idempotency_key`` の一意性で担保する。

業務ルール（CLAUDE.md § Business rules）:
1. entry fee は cash only（point 不可）。
2. buy_in / rebuy / add_on / order は cash + point 併用可。
3. point 不足分は cash で補完（`plan_payment` が core 側で分割を計算する）。

永続化は player registry / session レイヤと同じ単一 JSON ファイル + アトミックリネーム:

    {
      "ledger_entries": [{...LedgerEntry...}],
      "point_ledger_entries": [{...PointLedgerEntry...}]
    }
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path

from core.ledger import (
    LEDGER_KINDS,
    POINT_GRANT_REASONS,
    SPEND_REASON_BY_KIND,
    LedgerEntry,
    PointLedgerEntry,
)
from core.player_repository import PlayerNotFoundError, PlayerRepository
from core.session_repository import (
    SessionClosedError,
    SessionRepository,
    UnknownPlayerError,
)

logger = logging.getLogger(__name__)

_DEFAULT_LEDGER_DB = Path(__file__).parent.parent / "ledger.json"


class LedgerError(Exception):
    """ledger / point 操作の基底例外。"""


class InvalidKindError(LedgerError):
    """kind が定義外（error code: invalid_kind）。"""


class InvalidReasonError(LedgerError):
    """grant reason が grant 系定義外（error code: invalid_reason）。"""


class InvalidAmountError(LedgerError):
    """金額が不正（負 point / 合計 0 / point 不可 kind への point 等）（error code: invalid_amount）。"""


class InvalidOrderDetailError(LedgerError):
    """order 明細が不正、または order 以外に明細を付けた（error code: invalid_order_detail）。"""


class EntryFeeRequiresCashError(LedgerError):
    """entry fee に point を充当しようとした（error code: entry_fee_requires_cash）。"""


class InsufficientPointsError(LedgerError):
    """point 残高不足（spend / 負残高化する adjustment）（error code: insufficient_points）。"""


class DuplicateGrantError(LedgerError):
    """idempotency_key が既存 grant と重複（error code: duplicate_grant）。"""


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class LedgerRepository:
    """ledger entry / point ledger entry の永続ストア。

    session の実在・open 判定に ``session_repo``、player の実在判定に ``player_repo``
    を参照する。省略時は既定の `sessions.json` / `players.json` を読む実装を構築する。
    """

    def __init__(
        self,
        path: str | Path | None = None,
        session_repo: SessionRepository | None = None,
        player_repo: PlayerRepository | None = None,
    ) -> None:
        self._path = Path(path) if path is not None else _DEFAULT_LEDGER_DB
        self._player_repo = player_repo if player_repo is not None else PlayerRepository()
        self._session_repo = (
            session_repo
            if session_repo is not None
            else SessionRepository(player_repo=self._player_repo)
        )
        self._entries: list[LedgerEntry] = []
        self._point_entries: list[PointLedgerEntry] = []
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
        for raw in data.get("ledger_entries", []):
            try:
                self._entries.append(LedgerEntry.from_dict(raw))
            except (KeyError, TypeError):
                logger.warning("Skipping malformed ledger entry: %r", raw)
        for raw in data.get("point_ledger_entries", []):
            try:
                self._point_entries.append(PointLedgerEntry.from_dict(raw))
            except (KeyError, TypeError):
                logger.warning("Skipping malformed point ledger entry: %r", raw)

    def _flush(self) -> None:
        """アトミックリネームで書き込む。失敗してもクラッシュしない。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_suffix(".tmp")
        data = {
            "ledger_entries": [e.to_dict() for e in self._entries],
            "point_ledger_entries": [e.to_dict() for e in self._point_entries],
        }
        try:
            with tmp_path.open("w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self._path)
        except OSError:
            logger.exception("Failed to write ledger DB: %s", self._path)
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)

    # ――― validation helpers ―――

    def _require_player(self, player_id: str) -> None:
        try:
            self._player_repo.get(player_id)
        except PlayerNotFoundError as e:
            raise UnknownPlayerError(
                f"player_id={player_id} は registry に存在しません。"
            ) from e

    def _require_open_session(self, session_id: str) -> None:
        # unknown session は SessionNotFoundError（code: not_found）のまま伝播させる
        session = self._session_repo.get_session(session_id)
        if session.status == "closed":
            raise SessionClosedError(
                f"session_id={session_id} は closed です（entry 追加不可）。"
            )

    @staticmethod
    def _validate_order_detail(order: dict, cash_amount: int, point_amount: int) -> None:
        if not isinstance(order, dict):
            raise InvalidOrderDetailError("order 明細は object である必要があります。")
        item_name = order.get("item_name")
        unit_amount = order.get("unit_amount")
        quantity = order.get("quantity")
        if not isinstance(item_name, str) or not item_name.strip():
            raise InvalidOrderDetailError("order.item_name が空です。")
        if not isinstance(unit_amount, int) or isinstance(unit_amount, bool) or unit_amount < 0:
            raise InvalidOrderDetailError(f"order.unit_amount={unit_amount!r} が不正です。")
        if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 1:
            raise InvalidOrderDetailError(f"order.quantity={quantity!r} が不正です。")
        if unit_amount * quantity != cash_amount + point_amount:
            raise InvalidOrderDetailError(
                f"order 合計 {unit_amount * quantity} と支払額 {cash_amount + point_amount} が一致しません。"
            )

    @staticmethod
    def _require_int(value: int, label: str) -> None:
        if not isinstance(value, int) or isinstance(value, bool):
            raise InvalidAmountError(f"{label}={value!r} は整数である必要があります。")

    # ――― ledger entries ―――

    def add_entry(
        self,
        session_id: str,
        player_id: str,
        kind: str,
        cash_amount: int = 0,
        point_amount: int = 0,
        note: str | None = None,
        order: dict | None = None,
    ) -> LedgerEntry:
        """金銭イベントを 1 件記録する。

        point_amount > 0 の場合、残高を検証した上で対応する spend 系
        ``PointLedgerEntry`` を **同時生成** する（atomic: 検証完了まで一切 mutate しない）。

        reject: unknown session（`not_found`）/ closed session（`session_closed`）/
        unknown player（`unknown_player`）/ kind 定義外（`invalid_kind`）/
        金額不正（`invalid_amount`）/ order 明細不正（`invalid_order_detail`）/
        entry fee への point（`entry_fee_requires_cash`）/ 残高不足（`insufficient_points`）。
        """
        self._require_open_session(session_id)
        self._require_player(player_id)
        if kind not in LEDGER_KINDS:
            raise InvalidKindError(f"kind={kind!r} は定義外です（{LEDGER_KINDS}）。")
        self._require_int(cash_amount, "cash_amount")
        self._require_int(point_amount, "point_amount")
        if point_amount < 0:
            raise InvalidAmountError(f"point_amount={point_amount} は負にできません。")
        if kind == "entry_fee" and point_amount > 0:
            raise EntryFeeRequiresCashError("entry fee は cash only です（point 充当不可）。")
        if point_amount > 0 and kind not in SPEND_REASON_BY_KIND:
            raise InvalidAmountError(
                f"kind={kind} は point 充当できません（point 調整は adjust_points を使用）。"
            )
        if kind == "adjustment":
            if cash_amount == 0:
                raise InvalidAmountError("adjustment は cash_amount が 0 以外である必要があります。")
        else:
            if cash_amount < 0:
                raise InvalidAmountError(
                    f"kind={kind} の cash_amount={cash_amount} は負にできません。"
                )
            if cash_amount + point_amount <= 0:
                raise InvalidAmountError("cash_amount + point_amount は正である必要があります。")
        if kind == "order":
            if order is None:
                raise InvalidOrderDetailError("kind=order には order 明細が必須です。")
            self._validate_order_detail(order, cash_amount, point_amount)
        elif order is not None:
            raise InvalidOrderDetailError(f"kind={kind} に order 明細は付けられません。")
        if point_amount > 0:
            balance = self.point_balance(player_id)
            if point_amount > balance:
                raise InsufficientPointsError(
                    f"point 残高不足です（残高 {balance} < 要求 {point_amount}）。"
                    "不足分は cash で補完してください（plan_payment 参照）。"
                )

        occurred_at = _now_iso()
        entry = LedgerEntry(
            entry_id=uuid.uuid4().hex,
            session_id=session_id,
            player_id=player_id,
            kind=kind,
            occurred_at=occurred_at,
            cash_amount=cash_amount,
            point_amount=point_amount,
            note=note,
            order=dict(order) if order is not None else None,
        )
        self._entries.append(entry)
        if point_amount > 0:
            self._point_entries.append(
                PointLedgerEntry(
                    entry_id=uuid.uuid4().hex,
                    player_id=player_id,
                    delta_points=-point_amount,
                    reason=SPEND_REASON_BY_KIND[kind],
                    occurred_at=occurred_at,
                    related_ledger_entry_id=entry.entry_id,
                )
            )
        self._flush()
        logger.info(
            "Added ledger entry %s (kind=%s cash=%d point=%d player=%s session=%s)",
            entry.entry_id, kind, cash_amount, point_amount, player_id, session_id,
        )
        return entry

    def list_entries(
        self, session_id: str | None = None, player_id: str | None = None
    ) -> list[LedgerEntry]:
        """ledger entry を記録順で返す（session_id / player_id で絞り込み可）。"""
        return [
            e
            for e in self._entries
            if (session_id is None or e.session_id == session_id)
            and (player_id is None or e.player_id == player_id)
        ]

    def session_totals(self, session_id: str) -> dict[str, dict[str, int]]:
        """session 中間集計: player_id → buy-in 合計 / 注文合計（cash+point 込み）。

        **途中スナップショットであり確定値ではない**（確定は S4 settlement）。
        adjustment / entry_fee はどちらの合計にも含めない。
        """
        self._session_repo.get_session(session_id)  # unknown → not_found
        totals: dict[str, dict[str, int]] = {}
        for e in self._entries:
            if e.session_id != session_id:
                continue
            t = totals.setdefault(e.player_id, {"buy_in_total": 0, "order_total": 0})
            if e.kind in ("buy_in", "rebuy", "add_on"):
                t["buy_in_total"] += e.cash_amount + e.point_amount
            elif e.kind == "order":
                t["order_total"] += e.cash_amount + e.point_amount
        return totals

    # ――― point ledger ―――

    def point_balance(self, player_id: str) -> int:
        """player の point 残高（= point_ledger_entry の fold, ADR-0013）。"""
        self._require_player(player_id)
        return sum(
            e.delta_points for e in self._point_entries if e.player_id == player_id
        )

    def grant_points(
        self,
        player_id: str,
        points: int,
        reason: str,
        idempotency_key: str | None = None,
        note: str | None = None,
    ) -> PointLedgerEntry:
        """point を付与する（manual_grant / result_credit / campaign_grant）。

        ``idempotency_key`` を渡すと、同一キーの既存 entry がある場合
        `duplicate_grant` で reject する（重複 grant 防止, ADR-0013）。
        """
        self._require_player(player_id)
        if reason not in POINT_GRANT_REASONS:
            raise InvalidReasonError(
                f"reason={reason!r} は grant 系定義外です（{POINT_GRANT_REASONS}）。"
            )
        self._require_int(points, "points")
        if points <= 0:
            raise InvalidAmountError(f"points={points} は正である必要があります。")
        if idempotency_key is not None and any(
            e.idempotency_key == idempotency_key for e in self._point_entries
        ):
            raise DuplicateGrantError(
                f"idempotency_key={idempotency_key!r} の grant は既に記録済みです。"
            )

        entry = PointLedgerEntry(
            entry_id=uuid.uuid4().hex,
            player_id=player_id,
            delta_points=points,
            reason=reason,
            occurred_at=_now_iso(),
            idempotency_key=idempotency_key,
            note=note,
        )
        self._point_entries.append(entry)
        self._flush()
        logger.info("Granted %d points to player %s (%s)", points, player_id, reason)
        return entry

    def adjust_points(
        self, player_id: str, delta_points: int, note: str | None = None
    ) -> PointLedgerEntry:
        """point 残高を補正する（reason=adjustment, 両方向可）。

        結果残高が負になる補正は `insufficient_points` で reject する。
        """
        self._require_player(player_id)
        self._require_int(delta_points, "delta_points")
        if delta_points == 0:
            raise InvalidAmountError("delta_points=0 の adjustment は記録できません。")
        balance = self.point_balance(player_id)
        if balance + delta_points < 0:
            raise InsufficientPointsError(
                f"残高 {balance} に対して {delta_points} は負残高になります。"
            )

        entry = PointLedgerEntry(
            entry_id=uuid.uuid4().hex,
            player_id=player_id,
            delta_points=delta_points,
            reason="adjustment",
            occurred_at=_now_iso(),
            note=note,
        )
        self._point_entries.append(entry)
        self._flush()
        logger.info("Adjusted points of player %s by %d", player_id, delta_points)
        return entry

    def list_point_entries(self, player_id: str | None = None) -> list[PointLedgerEntry]:
        """point ledger entry を記録順で返す（player_id で絞り込み可）。"""
        return [
            e for e in self._point_entries if player_id is None or e.player_id == player_id
        ]

    # ――― payment planning（業務ルール 3: point 不足分は cash で補完） ―――

    def plan_payment(
        self, player_id: str, total_amount: int, use_points: bool = True
    ) -> tuple[int, int]:
        """支払額 ``total_amount`` の (cash_amount, point_amount) 分割を計算する。

        point 優先で充当し、**残高不足分は cash に倒す**（core が source of truth。
        front-end はこの結果を `add_entry` に渡すだけで分割ロジックを再実装しない）。
        """
        self._require_player(player_id)
        self._require_int(total_amount, "total_amount")
        if total_amount < 0:
            raise InvalidAmountError(f"total_amount={total_amount} は負にできません。")
        if not use_points:
            return total_amount, 0
        point_amount = min(self.point_balance(player_id), total_amount)
        return total_amount - point_amount, point_amount
