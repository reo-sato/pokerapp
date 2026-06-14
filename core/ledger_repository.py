"""core/ledger_repository.py

Phase S3.1: ledger / points / settlement の永続化 + 業務ルール。

`docs/contracts/ledger-overview.md`（ADR-0016）/ `ledger-schema.md` / `error-shapes.md` /
`validation-rules.md` の契約に対する core 実装。**業務ルール（invariants）はこの repository が
source of truth**（front-end は結果と error code を表示するだけ）。

永続化は player / session repository と同じスタイルの単一 JSON ファイル + アトミックリネーム。
別ストア（ADR-0016, 既定 `ledger.json`）に 2 台帳 + settlement を持つ:

    {
      "schema_version": "0.1",
      "ledger_entries": [ {LedgerEntry...}, ... ],
      "point_ledger_entries": [ {PointLedgerEntry...}, ... ],
      "settlements": [ {SessionSettlement...}, ... ]
    }

enforce する主な不変条件（`ledger-overview.md` § invariants）:
  - append-only（entry は mutate/delete せず、訂正は reversal で表す）。
  - point 残高 = point_ledger_entry の delta_points の fold（ISSUE-0001）。
  - 残高は負にならない（spend が残高を割り込むと insufficient_points）。
  - ledger↔point 整合（point_amount!=0 の entry には delta=-point_amount の point entry 1 件）。
  - entry fee は cash only。settlement は player→店の derived view、closed session のみ確定。
"""
from __future__ import annotations

import functools
import json
import logging
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path

from core.ledger import LedgerEntry, PointLedgerEntry, SessionSettlement
from core.player_repository import PlayerNotFoundError, PlayerRepository
from core.session_repository import SessionNotFoundError, SessionRepository

logger = logging.getLogger(__name__)

_DEFAULT_LEDGER_DB = Path(__file__).parent.parent / "ledger.json"
_SCHEMA_VERSION = "0.1"

_VALID_KINDS = {"buy_in", "rebuy", "add_on", "order", "entry_fee", "adjustment"}
_SPENDABLE_KINDS = {"buy_in", "rebuy", "add_on", "order"}  # point 充当を許す kind
_CASH_IN_KINDS = {"buy_in", "rebuy", "add_on"}
_GRANT_REASONS = {"manual_grant", "result_credit", "campaign_grant"}
_CREDIT_REASONS = {"result_credit", "campaign_grant"}
_SPEND_REASON = {
    "buy_in": "spend_on_buyin",
    "rebuy": "spend_on_rebuy",
    "add_on": "spend_on_addon",
    "order": "spend_on_order",
}


class LedgerError(Exception):
    """ledger / points / settlement 操作の基底例外。"""


class LedgerNotFoundError(LedgerError):
    """指定された entry / session / settlement が存在しない（error code: not_found）。"""


class UnknownPlayerError(LedgerError):
    """指定 player_id が registry に実在しない（error code: unknown_player）。"""


class InvalidAmountError(LedgerError):
    """金額・符号・明細・reversal 要求が不正（error code: invalid_amount）。"""


class EntryFeeRequiresCashError(LedgerError):
    """entry_fee に point を充当しようとした（error code: entry_fee_requires_cash）。"""


class InsufficientPointsError(LedgerError):
    """spend が point 残高を割り込む（error code: insufficient_points）。"""


class DuplicateGrantError(LedgerError):
    """同一 idempotency_key の grant が既に存在（error code: duplicate_grant）。"""


class SessionNotClosedError(LedgerError):
    """open の session を settlement 確定しようとした（error code: session_not_closed）。"""


class AlreadySettledError(LedgerError):
    """既に確定済の session を再確定しようとした（error code: already_settled）。"""


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _derive_payment_status(net_due_to_store: int, paid_amount: int) -> str:
    """paid_amount と net_due_to_store から payment_status を導出する（ADR-0023, 単一導出点）。

    - net<=0 → "paid"（徴収不要。過払い paid>=net もここに含まれ "paid" に丸める）。
    - paid_amount<=0 → "unpaid"。
    - paid_amount>=net → "paid"（全額・過払い）。
    - それ以外（0<paid<net）→ "partial"。
    """
    if net_due_to_store <= 0:
        return "paid"
    if paid_amount <= 0:
        return "unpaid"
    if paid_amount >= net_due_to_store:
        return "paid"
    return "partial"


def _locked(method):
    """`self._lock`（RLock）で公開メソッドの本体を囲む（thread-safety, S5/ADR-0021）。

    `--ledger` プロセスでは GUI スレッドと viewer API スレッドが同じ `LedgerRepository` を
    mutate しうる（HTTP スタッフ会計 write）。RLock は再入可能なので、ロック済みメソッドが
    他のロック済みメソッドを呼んでもデッドロックしない。
    """

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


class LedgerRepository:
    """ledger entry / point ledger / settlement の永続ストア。

    unknown_player の判定に player registry を、session 実在性・closed 判定に session レイヤを
    参照する。``session_repo`` / ``player_repo`` を渡さない場合は既定ストアを読むものを構築する。
    """

    def __init__(
        self,
        path: str | Path | None = None,
        session_repo: SessionRepository | None = None,
        player_repo: PlayerRepository | None = None,
    ) -> None:
        self._path = Path(path) if path is not None else _DEFAULT_LEDGER_DB
        # player 実在判定は session レイヤと同じ registry を共有する（名前空間ずれ防止）。
        # player_repo 明示 > session_repo の player_repo > 既定 registry の優先順。
        if player_repo is not None:
            self._player_repo = player_repo
        elif session_repo is not None:
            self._player_repo = session_repo.player_repo
        else:
            self._player_repo = PlayerRepository()
        self._session_repo = (
            session_repo
            if session_repo is not None
            else SessionRepository(player_repo=self._player_repo)
        )
        # GUI スレッドと in-process viewer API スレッドが同居する `--ledger` プロセスで
        # 共有状態を保護する（RLock = 再入可能。ADR-0021）。
        self._lock = threading.RLock()
        self._entries: list[LedgerEntry] = []
        self._point_entries: list[PointLedgerEntry] = []
        # (session_id, player_id) -> SessionSettlement
        self._settlements: dict[tuple[str, str], SessionSettlement] = {}
        self._load()

    @property
    def path(self) -> Path:
        """この repository の永続ファイルパス（sync が snapshot/merge 対象を特定する用）。"""
        return self._path

    @property
    def player_repo(self) -> PlayerRepository:
        """canonicalize（player merge, ADR-0030）に使う registry。"""
        return self._player_repo

    def _canonical(self, player_id: str) -> str:
        """merge 済みなら survivor の player_id に解決する（ADR-0030 D2）。"""
        return self._player_repo.resolve_canonical(player_id)

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
        for raw in data.get("settlements", []):
            try:
                s = SessionSettlement.from_dict(raw)
            except (KeyError, TypeError):
                logger.warning("Skipping malformed settlement: %r", raw)
                continue
            self._settlements[(s.session_id, s.player_id)] = s

    def _flush(self) -> None:
        """アトミックリネームで書き込む。失敗してもクラッシュしない。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_suffix(".tmp")
        data = {
            "schema_version": _SCHEMA_VERSION,
            "ledger_entries": [e.to_dict() for e in self._entries],
            "point_ledger_entries": [p.to_dict() for p in self._point_entries],
            "settlements": [s.to_dict() for s in self._settlements.values()],
        }
        try:
            with tmp_path.open("w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self._path)
        except OSError:
            logger.exception("Failed to write ledger DB: %s", self._path)
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)

    @_locked
    def reload(self) -> None:
        """ディスクから ledger / point / settlement を再読込する（sync 後の最新化用, ADR-0022）。

        file-level merge（`core/sync.py`）が `ledger.json` を書き換えた後、live プロセスの
        in-memory 状態を最新化するために呼ぶ。in-memory list/dict をリセットして `_load` し直す
        だけで、業務ルールには影響しない。
        """
        self._entries.clear()
        self._point_entries.clear()
        self._settlements.clear()
        self._load()

    # ――― 参照整合ヘルパ ―――

    def _require_session(self, session_id: str):
        try:
            return self._session_repo.get_session(session_id)
        except SessionNotFoundError as e:
            raise LedgerNotFoundError(f"session_id={session_id} は存在しません。") from e

    def _require_player(self, player_id: str) -> None:
        try:
            self._player_repo.get(player_id)
        except PlayerNotFoundError as e:
            raise UnknownPlayerError(f"player_id={player_id} は registry に存在しません。") from e

    def _find_entry(self, entry_id: str) -> LedgerEntry | None:
        return next((e for e in self._entries if e.entry_id == entry_id), None)

    @staticmethod
    def _validate_order(order: dict) -> None:
        if not isinstance(order, dict):
            raise InvalidAmountError("order は object である必要があります。")
        name, unit, qty = order.get("item_name"), order.get("unit_amount"), order.get("quantity")
        if not isinstance(name, str) or not name.strip():
            raise InvalidAmountError("order.item_name が空です。")
        if not isinstance(unit, int) or isinstance(unit, bool) or unit < 0:
            raise InvalidAmountError("order.unit_amount は 0 以上の整数です。")
        if not isinstance(qty, int) or isinstance(qty, bool) or qty < 1:
            raise InvalidAmountError("order.quantity は 1 以上の整数です。")

    # ――― point ledger ―――

    def _append_point_entry(
        self,
        player_id: str,
        delta_points: int,
        reason: str,
        related_ledger_entry_id: str | None = None,
        session_id: str | None = None,
        idempotency_key: str | None = None,
        occurred_at: str | None = None,
    ) -> PointLedgerEntry:
        entry = PointLedgerEntry(
            entry_id=uuid.uuid4().hex,
            player_id=player_id,
            delta_points=delta_points,
            reason=reason,
            occurred_at=occurred_at or _now_iso(),
            related_ledger_entry_id=related_ledger_entry_id,
            session_id=session_id,
            idempotency_key=idempotency_key,
        )
        self._point_entries.append(entry)
        return entry

    @_locked
    def point_balance(self, player_id: str) -> int:
        """player の point 残高 = point_ledger_entry の delta_points の fold（source of truth）。

        merge 済みなら survivor の equivalence class 全体を合算する（ADR-0030 D2）。
        """
        self._require_player(player_id)
        cls = self._player_repo.equivalence_class(player_id)
        return sum(p.delta_points for p in self._point_entries if p.player_id in cls)

    @_locked
    def grant_points(
        self,
        player_id: str,
        delta_points: int,
        reason: str = "manual_grant",
        session_id: str | None = None,
        idempotency_key: str | None = None,
        occurred_at: str | None = None,
    ) -> PointLedgerEntry:
        """point を付与する（増加経路: manual_grant / result_credit / campaign_grant）。

        idempotency_key を渡すと、同キーの既存 grant があれば DuplicateGrantError（重複防止）。
        """
        self._require_player(player_id)
        if reason not in _GRANT_REASONS:
            raise ValueError(f"grant の reason が不正です: {reason!r}")
        if not isinstance(delta_points, int) or isinstance(delta_points, bool) or delta_points <= 0:
            raise InvalidAmountError("grant は正の整数 point である必要があります。")
        if idempotency_key is not None and any(
            p.idempotency_key == idempotency_key for p in self._point_entries
        ):
            raise DuplicateGrantError(f"idempotency_key={idempotency_key} は既に使用済みです。")
        entry = self._append_point_entry(
            player_id,
            delta_points,
            reason,
            session_id=session_id,
            idempotency_key=idempotency_key,
            occurred_at=occurred_at,
        )
        self._flush()
        logger.info("Granted %d points to %s (%s)", delta_points, player_id, reason)
        return entry

    @_locked
    def list_point_entries(
        self, player_id: str | None = None, session_id: str | None = None
    ) -> list[PointLedgerEntry]:
        """point ledger entry を挿入順で返す（filter は lenient: unknown でも空 list）。"""
        result = self._point_entries
        if player_id is not None:
            result = [p for p in result if p.player_id == player_id]
        if session_id is not None:
            result = [p for p in result if p.session_id == session_id]
        return list(result)

    # ――― ledger entry ―――

    @_locked
    def add_entry(
        self,
        session_id: str,
        player_id: str,
        kind: str,
        cash_amount: int = 0,
        point_amount: int = 0,
        note: str | None = None,
        hand_id: int | None = None,
        order: dict | None = None,
        occurred_at: str | None = None,
    ) -> LedgerEntry:
        """金銭イベント 1 件を記録する（append-only）。

        point_amount>0 なら残高を確認（不足は InsufficientPointsError）し、対応する
        spend_* の point_ledger_entry を同時に起こす（ledger↔point 整合）。
        """
        if kind not in _VALID_KINDS:
            raise ValueError(f"kind が不正です: {kind!r}")
        self._require_session(session_id)
        self._require_player(player_id)
        if (
            not isinstance(cash_amount, int)
            or isinstance(cash_amount, bool)
            or not isinstance(point_amount, int)
            or isinstance(point_amount, bool)
        ):
            raise InvalidAmountError("cash_amount / point_amount は整数である必要があります。")
        if order is not None and kind != "order":
            raise InvalidAmountError("order 明細は kind=order のときのみ指定できます。")
        if kind == "order" and order is not None:
            self._validate_order(order)

        if kind == "entry_fee":
            if point_amount != 0:
                raise EntryFeeRequiresCashError("entry_fee は cash only です（point 不可）。")
            if cash_amount <= 0:
                raise InvalidAmountError("entry_fee は正の cash である必要があります。")
        elif kind == "adjustment":
            if point_amount != 0:
                raise InvalidAmountError(
                    "adjustment の point 調整は grant_points / reverse_entry を使ってください。"
                )
            if cash_amount == 0:
                raise InvalidAmountError("adjustment は非ゼロの cash である必要があります。")
        else:  # buy_in / rebuy / add_on / order
            if cash_amount < 0 or point_amount < 0:
                raise InvalidAmountError("通常 entry の金額は非負である必要があります。")
            if cash_amount + point_amount <= 0:
                raise InvalidAmountError("entry は cash か point のいずれかで価値が動く必要があります。")

        if point_amount > 0:  # spendable kind のみここに到達
            balance = self.point_balance(player_id)
            if balance < point_amount:
                raise InsufficientPointsError(
                    f"point 残高 {balance} が必要点数 {point_amount} に不足しています。"
                )

        entry = LedgerEntry(
            entry_id=uuid.uuid4().hex,
            session_id=session_id,
            player_id=player_id,
            kind=kind,
            occurred_at=occurred_at or _now_iso(),
            cash_amount=cash_amount,
            point_amount=point_amount,
            note=note,
            hand_id=hand_id,
            order=order,
        )
        self._entries.append(entry)
        if point_amount > 0:
            self._append_point_entry(
                player_id,
                -point_amount,
                _SPEND_REASON[kind],
                related_ledger_entry_id=entry.entry_id,
                session_id=session_id,
                occurred_at=entry.occurred_at,
            )
        self._flush()
        logger.info(
            "Ledger entry %s: %s cash=%d point=%d (session=%s player=%s)",
            entry.entry_id, kind, cash_amount, point_amount, session_id, player_id,
        )
        return entry

    @_locked
    def reverse_entry(self, entry_id: str, occurred_at: str | None = None) -> LedgerEntry:
        """既存 entry を相殺する reversal を append する（append-only 訂正）。

        reversal / 既に reverse 済の entry は再 reverse できない（残高不変条件を守るため）。
        point を伴う entry の reversal は point を払い戻す point_ledger_entry を起こす。
        """
        orig = self._find_entry(entry_id)
        if orig is None:
            raise LedgerNotFoundError(f"entry_id={entry_id} は存在しません。")
        if orig.reverses_entry_id is not None:
            raise InvalidAmountError("reversal entry は再度 reverse できません。")
        if any(e.reverses_entry_id == entry_id for e in self._entries):
            raise InvalidAmountError(f"entry_id={entry_id} は既に reverse 済みです。")

        reversal = LedgerEntry(
            entry_id=uuid.uuid4().hex,
            session_id=orig.session_id,
            player_id=orig.player_id,
            kind=orig.kind,
            occurred_at=occurred_at or _now_iso(),
            cash_amount=-orig.cash_amount,
            point_amount=-orig.point_amount,
            note=f"reversal of {orig.entry_id}",
            reverses_entry_id=orig.entry_id,
        )
        self._entries.append(reversal)
        if orig.point_amount != 0:
            # 払い戻し: linked delta = -reversal.point_amount = orig.point_amount
            self._append_point_entry(
                orig.player_id,
                -reversal.point_amount,
                "adjustment",
                related_ledger_entry_id=reversal.entry_id,
                session_id=orig.session_id,
                occurred_at=reversal.occurred_at,
            )
        self._flush()
        logger.info("Reversed entry %s with %s", orig.entry_id, reversal.entry_id)
        return reversal

    @_locked
    def list_entries(
        self, session_id: str | None = None, player_id: str | None = None
    ) -> list[LedgerEntry]:
        """ledger entry を挿入順で返す（filter は lenient: unknown でも空 list）。"""
        result = self._entries
        if session_id is not None:
            result = [e for e in result if e.session_id == session_id]
        if player_id is not None:
            # merge 済みの旧 ID で記帳された entry も survivor 視点で拾う（ADR-0030 D2）。
            cls = self._player_repo.equivalence_class(player_id)
            result = [e for e in result if e.player_id in cls]
        return list(result)

    # ――― settlement ―――

    def _derive_settlement_rows(self, session_id: str) -> list[SessionSettlement]:
        # merge 済み player は survivor の canonical id でグルーピングし、同一人物の複数 ID 分を
        # 1 行に合算する（ADR-0030 D2。historical な entry.player_id は書き換えない）。
        players: set[str] = {
            self._canonical(e.player_id) for e in self._entries if e.session_id == session_id
        }
        players |= {
            self._canonical(p.player_id)
            for p in self._point_entries if p.session_id == session_id
        }

        rows: list[SessionSettlement] = []
        for pid in sorted(players):
            entries_p = [
                e for e in self._entries
                if e.session_id == session_id and self._canonical(e.player_id) == pid
            ]
            points_p = [
                p for p in self._point_entries
                if p.session_id == session_id and self._canonical(p.player_id) == pid
            ]
            cash_in_total = sum(e.cash_amount for e in entries_p if e.kind in _CASH_IN_KINDS)
            order_total = sum(e.cash_amount for e in entries_p if e.kind == "order")
            entry_fee = sum(e.cash_amount for e in entries_p if e.kind == "entry_fee")
            net_due = sum(e.cash_amount for e in entries_p)
            point_spent = sum(
                -p.delta_points for p in points_p if p.reason.startswith("spend_on_")
            )
            point_credited = sum(
                p.delta_points for p in points_p if p.reason in _CREDIT_REASONS
            )
            existing = self._settlements.get((session_id, pid))
            rows.append(
                SessionSettlement(
                    session_id=session_id,
                    player_id=pid,
                    cash_in_total=cash_in_total,
                    point_spent_total=point_spent,
                    order_total=order_total,
                    entry_fee=entry_fee,
                    point_credited_total=point_credited,
                    net_due_to_store=net_due,
                    payment_status=existing.payment_status if existing else "unpaid",
                    settled_at=existing.settled_at if existing else _now_iso(),
                    paid_amount=existing.paid_amount if existing else 0,
                )
            )
        return rows

    @_locked
    def compute_settlement(self, session_id: str) -> list[SessionSettlement]:
        """session の player ごと settlement を導出する（speculative。確定はしない）。

        open / closed どちらでも計算できる（中間集計）。確定済 session は確定行の
        payment_status / settled_at を反映する。
        """
        self._require_session(session_id)
        return self._derive_settlement_rows(session_id)

    @_locked
    def commit_settlement(self, session_id: str) -> list[SessionSettlement]:
        """closed session の settlement を確定（凍結）する。

        open は SessionNotClosedError、確定済は AlreadySettledError。
        """
        session = self._require_session(session_id)
        if session.status != "closed":
            raise SessionNotClosedError(f"session_id={session_id} は closed ではありません。")
        if any(s_id == session_id for (s_id, _pid) in self._settlements):
            raise AlreadySettledError(f"session_id={session_id} は既に確定済みです。")
        now = _now_iso()
        committed: list[SessionSettlement] = []
        for row in self._derive_settlement_rows(session_id):
            row.paid_amount = 0
            # net<=0 の player は徴収不要 → "paid"、他は "unpaid"（ADR-0023）。
            row.payment_status = _derive_payment_status(row.net_due_to_store, 0)
            row.settled_at = now
            self._settlements[(session_id, row.player_id)] = row
            committed.append(row)
        self._flush()
        logger.info("Committed settlement for session %s (%d rows)", session_id, len(committed))
        return committed

    @_locked
    def list_settlements(self, session_id: str) -> list[SessionSettlement]:
        """確定済 settlement 行を返す（未確定なら空 list）。"""
        return [s for (s_id, _pid), s in self._settlements.items() if s_id == session_id]

    @_locked
    def all_settlements(self) -> list[SessionSettlement]:
        """全 session の確定済 settlement 行を返す（CSV export 等の横断集計用）。"""
        return list(self._settlements.values())

    @_locked
    def record_payment(
        self, session_id: str, player_id: str, paid_amount: int
    ) -> SessionSettlement:
        """確定済 settlement に受領額を記録する（partial-paid 対応, ADR-0023）。

        paid_amount は「これまでに受け取った累計額」（差分ではなく絶対値）。payment_status は
        net_due_to_store との関係から導出する（paid / unpaid / partial）。負値は
        InvalidAmountError、未確定は LedgerNotFoundError。
        """
        if not isinstance(paid_amount, int) or isinstance(paid_amount, bool) or paid_amount < 0:
            raise InvalidAmountError("paid_amount は 0 以上の整数である必要があります。")
        settlement = self._settlements.get((session_id, player_id))
        if settlement is None:
            raise LedgerNotFoundError(
                f"settlement(session={session_id}, player={player_id}) は確定されていません。"
            )
        settlement.paid_amount = paid_amount
        settlement.payment_status = _derive_payment_status(
            settlement.net_due_to_store, paid_amount
        )
        self._flush()
        return settlement

    @_locked
    def set_payment_status(
        self, session_id: str, player_id: str, status: str
    ) -> SessionSettlement:
        """確定済 settlement の支払状態を切り替える shortcut（後方互換, ADR-0023）。

        "paid" → 全額受領（record_payment(net_due_to_store)）、"unpaid" → 受領 0
        （record_payment(0)）として記録する。"partial" は金額が必要なため status 経由では
        扱えず ValueError（record_payment を使う）。導出は record_payment に一元化する。
        """
        if status == "paid":
            settlement = self._settlements.get((session_id, player_id))
            if settlement is None:
                raise LedgerNotFoundError(
                    f"settlement(session={session_id}, player={player_id}) は確定されていません。"
                )
            # net<=0（徴収不要）でも paid_amount は非負である必要があるため 0 で record する
            # （_derive_payment_status は net<=0 を "paid" に丸める）。
            return self.record_payment(
                session_id, player_id, max(settlement.net_due_to_store, 0)
            )
        if status == "unpaid":
            return self.record_payment(session_id, player_id, 0)
        raise ValueError(
            f"payment_status が不正です: {status!r}（partial は record_payment を使ってください）"
        )
