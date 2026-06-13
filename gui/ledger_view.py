"""gui/ledger_view.py

Phase S3.2: Ledger Viewer / Editor Screen（ISSUE-0017）。

hand logger の dashboard (`gui/dashboard.py`) とは **完全に別画面**。本画面は
`GameStateManager` / `JsonWriter` 等の hand logger 依存を一切持たず、`LedgerRepository`
（+ session/player repository）のみに依存する（CLAUDE.md § Ledger / Points / Settlement、ADR-0016）。

できること（S3.2 scope, ISSUE-0017）:
  - session を選び、その roster（S2 seating 先読み）から player を選んで ledger entry を追加する
    （buy_in / rebuy / add_on / order / entry_fee / adjustment、cash + point 併用）。
  - player の point 残高（fold）と、session の **中間集計（buy-in 合計 / order 合計 / net 見込み）**
    を表示する。中間集計は **暫定（speculative）** であることを明示する。
  - entry の訂正は **reversal**（append-only）で行う（直接編集・削除はしない）。
  - point を付与する（manual_grant）。
  - validation は core / repository の error を表示するだけ（再実装しない）。

settlement の確定（commit）/ CSV export は本画面の scope 外（S3.3, ISSUE-0018）。

レイアウト:
  ┌────────────────────────────────────────────┐
  │  Ledger / 台帳                               │
  ├────────────────────────────────────────────┤
  │  セッション [▼]            状態: open         │
  │  プレイヤー [▼]            残高: NNN pt        │
  │  種別[▼] cash[__] point[__] メモ[____] [追加] │
  │  付与ポイント[__] [付与]                       │
  ├────────────────────────────────────────────┤
  │  エントリ一覧 (取消ボタン付き)                 │
  ├────────────────────────────────────────────┤
  │  中間集計（暫定 / speculative）               │
  ├────────────────────────────────────────────┤
  │  ステータス / メッセージ                       │
  └────────────────────────────────────────────┘

スレッド構成なし（hand logger と異なり録音スレッド等を持たない CRUD/閲覧画面）。
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from core.ledger_repository import (
    AlreadySettledError,
    DuplicateGrantError,
    EntryFeeRequiresCashError,
    InsufficientPointsError,
    InvalidAmountError,
    LedgerNotFoundError,
    LedgerRepository,
    SessionNotClosedError,
    UnknownPlayerError,
)
from core.session_repository import SessionNotFoundError

if TYPE_CHECKING:
    from core.menu import MenuMaster
    from core.order_request_repository import OrderRequestRepository
    from core.player_repository import PlayerRepository
    from core.session_repository import SessionRepository

logger = logging.getLogger(__name__)

_STATUS_OK_COLOR = "#4CAF50"
_STATUS_ERROR_COLOR = "#F44336"
_SPECULATIVE_COLOR = "#FFB74D"

# core が enforce する業務 error（表示用に捕捉する。分岐は型 = code, 文言は str(e)）。
_LEDGER_ERRORS = (
    EntryFeeRequiresCashError,
    InsufficientPointsError,
    InvalidAmountError,
    UnknownPlayerError,
    LedgerNotFoundError,
    DuplicateGrantError,
    SessionNotClosedError,
    AlreadySettledError,
)

_KINDS = ["buy_in", "rebuy", "add_on", "order", "entry_fee", "adjustment"]


class LedgerViewWindow:
    """ledger entry の追加・取消・中間集計表示を行う独立画面。

    使い方:
        win = LedgerViewWindow(ledger_repo=ledger, session_repo=sessions, player_repo=players)
        win.run()  # mainloop 開始（ブロッキング）

    既存アプリ内から開く場合は master を渡すと Toplevel として開く。
    """

    def __init__(
        self,
        ledger_repo: LedgerRepository,
        session_repo: "SessionRepository",
        player_repo: "PlayerRepository",
        master: Optional[object] = None,
        order_repo: "OrderRequestRepository | None" = None,
        menu: "MenuMaster | None" = None,
    ) -> None:
        import customtkinter as ctk

        self._ledger = ledger_repo
        self._session_repo = session_repo
        self._player_repo = player_repo
        # M5 (ADR-0018): 注文リクエスト確定/却下パネル。order_repo/menu が無ければ非表示。
        self._order_repo = order_repo
        self._menu = menu
        self._order_rows: list[object] = []

        self._session_id: Optional[str] = None
        self._selected_player_id: Optional[str] = None
        self._session_by_label: dict[str, str] = {}
        self._player_by_label: dict[str, str] = {}
        self._entry_rows: dict[str, object] = {}
        self._summary_rows: list[object] = []

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self._ctk = ctk
        self._owns_root = master is None
        self._root = ctk.CTk() if master is None else ctk.CTkToplevel(master)
        self._root.title("Ledger / 台帳")
        self._root.geometry("620x720")
        self._root.resizable(True, True)

        self._build_ui()
        self._reload_choices()

    # ――― UI 構築 ―――

    def _build_ui(self) -> None:
        ctk = self._ctk
        root = self._root
        root.grid_rowconfigure(2, weight=1)
        root.grid_rowconfigure(3, weight=1)
        root.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(root, text="Ledger / 台帳", font=("", 16, "bold")).grid(
            row=0, column=0, sticky="w", padx=12, pady=(12, 4)
        )

        form = ctk.CTkFrame(root)
        form.grid(row=1, column=0, sticky="ew", padx=12, pady=4)
        form.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(form, text="セッション").grid(row=0, column=0, padx=8, pady=6, sticky="w")
        self._session_menu = ctk.CTkOptionMenu(
            form, values=[""], command=self._on_session_selected
        )
        self._session_menu.grid(row=0, column=1, padx=8, pady=6, sticky="ew")
        self._session_status_label = ctk.CTkLabel(form, text="状態: —", anchor="e")
        self._session_status_label.grid(row=0, column=2, padx=8, pady=6, sticky="e")

        ctk.CTkLabel(form, text="プレイヤー").grid(row=1, column=0, padx=8, pady=6, sticky="w")
        self._player_menu = ctk.CTkOptionMenu(
            form, values=[""], command=self._on_player_selected
        )
        self._player_menu.grid(row=1, column=1, padx=8, pady=6, sticky="ew")
        self._balance_label = ctk.CTkLabel(form, text="残高: — pt", anchor="e")
        self._balance_label.grid(row=1, column=2, padx=8, pady=6, sticky="e")

        entry_row = ctk.CTkFrame(form, fg_color="transparent")
        entry_row.grid(row=2, column=0, columnspan=3, sticky="ew", padx=4, pady=4)
        self._kind_menu = ctk.CTkOptionMenu(entry_row, values=_KINDS)
        self._kind_menu.grid(row=0, column=0, padx=4)
        self._cash_entry = ctk.CTkEntry(entry_row, width=90, placeholder_text="cash(円)")
        self._cash_entry.grid(row=0, column=1, padx=4)
        self._point_entry = ctk.CTkEntry(entry_row, width=90, placeholder_text="point")
        self._point_entry.grid(row=0, column=2, padx=4)
        self._note_entry = ctk.CTkEntry(entry_row, width=130, placeholder_text="メモ")
        self._note_entry.grid(row=0, column=3, padx=4)
        ctk.CTkButton(entry_row, text="エントリ追加", width=100, command=self._cmd_add_entry).grid(
            row=0, column=4, padx=4
        )

        grant_row = ctk.CTkFrame(form, fg_color="transparent")
        grant_row.grid(row=3, column=0, columnspan=3, sticky="w", padx=4, pady=(0, 4))
        ctk.CTkLabel(grant_row, text="ポイント付与").grid(row=0, column=0, padx=4)
        self._grant_entry = ctk.CTkEntry(grant_row, width=90, placeholder_text="point")
        self._grant_entry.grid(row=0, column=1, padx=4)
        ctk.CTkButton(grant_row, text="付与", width=70, command=self._cmd_grant_points).grid(
            row=0, column=2, padx=4
        )

        self._list_frame = ctk.CTkScrollableFrame(root, label_text="エントリ一覧")
        self._list_frame.grid(row=2, column=0, sticky="nsew", padx=12, pady=4)
        self._list_frame.grid_columnconfigure(0, weight=1)

        self._summary_frame = ctk.CTkScrollableFrame(
            root, label_text="中間集計（暫定 / speculative — 確定は session 締めで行う）"
        )
        self._summary_frame.grid(row=3, column=0, sticky="nsew", padx=12, pady=4)
        self._summary_frame.grid_columnconfigure(0, weight=1)

        # M5: 注文リクエスト（pending）確定/却下。order_repo が注入されたときのみ表示。
        if self._order_repo is not None:
            root.grid_rowconfigure(4, weight=1)
            self._order_frame = ctk.CTkScrollableFrame(
                root, label_text="注文リクエスト（pending — 確定で order entry を作成）"
            )
            self._order_frame.grid(row=4, column=0, sticky="nsew", padx=12, pady=4)
            self._order_frame.grid_columnconfigure(0, weight=1)
            status_row = 5
        else:
            self._order_frame = None
            status_row = 4

        self._status_label = ctk.CTkLabel(root, text="", anchor="w")
        self._status_label.grid(row=status_row, column=0, sticky="ew", padx=12, pady=(0, 12))

    # ――― 選択肢ロード ―――

    def _reload_choices(self) -> None:
        sessions = self._session_repo.list_sessions()
        self._session_by_label = {self._session_label(s): s.session_id for s in sessions}
        labels = list(self._session_by_label) or [""]
        self._session_menu.configure(values=labels)
        self._reload_player_choices()

    def _reload_player_choices(self) -> None:
        roster = self._roster_for_session(self._session_id)
        self._player_by_label = {self._player_name(pid): pid for pid in roster}
        labels = list(self._player_by_label) or [""]
        self._player_menu.configure(values=labels)

    # ――― ヘルパ ―――

    @staticmethod
    def _session_label(session) -> str:
        tag = session.label or session.session_id[:8]
        return f"{tag}（{session.status}）"

    def _player_name(self, player_id: str) -> str:
        try:
            return self._player_repo.get(player_id).display_name
        except Exception:
            return player_id[:8]

    def _roster_for_session(self, session_id: Optional[str]) -> list[str]:
        """session の参加 player を返す（S2 seating 先読み）。

        seating があればその player_id 群、無ければ registry 全 player に fallback する。
        """
        if session_id is not None:
            try:
                seated = [sa.player_id for sa in self._session_repo.current_seating(session_id)]
            except SessionNotFoundError:
                seated = []
            if seated:
                # 重複除去（同一 player が複数 hand に出てもよい）かつ順序維持
                seen: dict[str, None] = {}
                for pid in seated:
                    seen.setdefault(pid, None)
                return list(seen)
        return [p.player_id for p in self._player_repo.list_players()]

    @staticmethod
    def _parse_amount(text: Optional[str]) -> Optional[int]:
        """金額テキストを int に変換する。空文字は 0、変換不可は None。"""
        text = (text or "").strip()
        if text == "":
            return 0
        try:
            return int(text)
        except ValueError:
            return None

    def _set_status(self, message: str, error: bool = False) -> None:
        color = _STATUS_ERROR_COLOR if error else _STATUS_OK_COLOR
        self._status_label.configure(text=message, text_color=color)

    # ――― 選択 ―――

    def _on_session_selected(self, label: str) -> None:
        self._select_session(self._session_by_label.get(label))

    def _on_player_selected(self, label: str) -> None:
        self._select_player(self._player_by_label.get(label))

    def _select_session(self, session_id: Optional[str]) -> None:
        self._session_id = session_id
        status = "—"
        if session_id is not None:
            try:
                status = self._session_repo.get_session(session_id).status
            except SessionNotFoundError:
                status = "—"
        self._session_status_label.configure(text=f"状態: {status}")
        self._selected_player_id = None
        self._balance_label.configure(text="残高: — pt")
        self._reload_player_choices()
        self._refresh()
        if session_id is not None:
            self._set_status("セッションを選択しました。")

    def _select_player(self, player_id: Optional[str]) -> None:
        self._selected_player_id = player_id
        if player_id is None:
            self._balance_label.configure(text="残高: — pt")
            return
        try:
            balance = self._ledger.point_balance(player_id)
        except UnknownPlayerError:
            balance = 0
        self._balance_label.configure(text=f"残高: {balance} pt")

    # ――― コマンド ―――

    def _cmd_add_entry(self) -> None:
        if not self._session_id:
            self._set_status("セッションを選択してください。", error=True)
            return
        if not self._selected_player_id:
            self._set_status("プレイヤーを選択してください。", error=True)
            return
        cash = self._parse_amount(self._cash_entry.get())
        point = self._parse_amount(self._point_entry.get())
        if cash is None or point is None:
            self._set_status("cash / point は整数で入力してください。", error=True)
            return
        kind = self._kind_menu.get()
        note = (self._note_entry.get() or "").strip() or None
        try:
            entry = self._ledger.add_entry(
                self._session_id,
                self._selected_player_id,
                kind,
                cash_amount=cash,
                point_amount=point,
                note=note,
            )
        except _LEDGER_ERRORS as e:
            self._set_status(str(e), error=True)
            return
        except ValueError as e:
            self._set_status(str(e), error=True)
            return
        self._select_player(self._selected_player_id)  # 残高表示を更新
        self._refresh()
        self._set_status(f"記録しました: {entry.kind} cash={entry.cash_amount} point={entry.point_amount}")

    def _cmd_reverse_entry(self, entry_id: str) -> None:
        try:
            self._ledger.reverse_entry(entry_id)
        except _LEDGER_ERRORS as e:
            self._set_status(str(e), error=True)
            return
        if self._selected_player_id:
            self._select_player(self._selected_player_id)
        self._refresh()
        self._set_status("取り消しました（reversal を追加）。")

    def _cmd_grant_points(self) -> None:
        if not self._selected_player_id:
            self._set_status("プレイヤーを選択してください。", error=True)
            return
        points = self._parse_amount(self._grant_entry.get())
        if points is None:
            self._set_status("付与ポイントは整数で入力してください。", error=True)
            return
        try:
            self._ledger.grant_points(
                self._selected_player_id, points, session_id=self._session_id or None
            )
        except _LEDGER_ERRORS as e:
            self._set_status(str(e), error=True)
            return
        except ValueError as e:
            self._set_status(str(e), error=True)
            return
        self._select_player(self._selected_player_id)
        self._set_status(f"ポイントを付与しました: +{points} pt")

    # ――― 表示更新 ―――

    def _refresh(self) -> None:
        self._refresh_entry_list()
        self._refresh_summary()
        self._refresh_order_requests()

    def _refresh_entry_list(self) -> None:
        ctk = self._ctk
        for widget in list(self._entry_rows.values()):
            try:
                widget.destroy()
            except Exception:
                pass
        self._entry_rows.clear()
        if not self._session_id:
            return
        for idx, entry in enumerate(self._ledger.list_entries(session_id=self._session_id)):
            row = ctk.CTkFrame(self._list_frame, fg_color="transparent")
            row.grid(row=idx, column=0, sticky="ew", pady=1)
            row.grid_columnconfigure(0, weight=1)
            text = (
                f"{entry.occurred_at[11:19]}  {self._player_name(entry.player_id)}  "
                f"{entry.kind}  cash={entry.cash_amount} point={entry.point_amount}"
            )
            if entry.reverses_entry_id is not None:
                text = "↩ " + text
            ctk.CTkLabel(row, text=text, anchor="w").grid(row=0, column=0, sticky="w", padx=4)
            # reversal 自身は再 reverse できないのでボタンを出さない
            if entry.reverses_entry_id is None:
                ctk.CTkButton(
                    row, text="取消", width=56,
                    command=lambda eid=entry.entry_id: self._cmd_reverse_entry(eid),
                ).grid(row=0, column=1, padx=4)
            self._entry_rows[entry.entry_id] = row

    def _refresh_summary(self) -> None:
        ctk = self._ctk
        for widget in self._summary_rows:
            try:
                widget.destroy()
            except Exception:
                pass
        self._summary_rows.clear()
        if not self._session_id:
            return
        try:
            rows = self._ledger.compute_settlement(self._session_id)
        except LedgerNotFoundError:
            return
        net_total = 0
        for idx, s in enumerate(rows):
            net_total += s.net_due_to_store
            line = (
                f"{self._player_name(s.player_id)}: buy-in {s.cash_in_total} / order {s.order_total} / "
                f"fee {s.entry_fee} / spent {s.point_spent_total}pt → net {s.net_due_to_store} 円（暫定）"
            )
            lbl = ctk.CTkLabel(self._summary_frame, text=line, anchor="w", text_color=_SPECULATIVE_COLOR)
            lbl.grid(row=idx, column=0, sticky="w", padx=4, pady=1)
            self._summary_rows.append(lbl)
        total = ctk.CTkLabel(
            self._summary_frame,
            text=f"合計 net（暫定）: {net_total} 円",
            anchor="w",
            font=("", 12, "bold"),
        )
        total.grid(row=len(rows), column=0, sticky="w", padx=4, pady=(6, 1))
        self._summary_rows.append(total)

    # ――― 注文リクエスト（M5, ADR-0018）―――

    def _refresh_order_requests(self) -> None:
        if self._order_frame is None or self._order_repo is None:
            return
        ctk = self._ctk
        for widget in self._order_rows:
            try:
                widget.destroy()
            except Exception:
                pass
        self._order_rows.clear()
        if not self._session_id:
            return
        try:
            pending = self._order_repo.list_requests(self._session_id, status="pending")
        except SessionNotFoundError:
            return
        for idx, req in enumerate(pending):
            row = ctk.CTkFrame(self._order_frame, fg_color="transparent")
            row.grid(row=idx, column=0, sticky="ew", pady=1)
            row.grid_columnconfigure(0, weight=1)
            prefill = self._menu.unit_amount(req.item_name) if self._menu else None
            label = (
                f"{self._player_name(req.player_id)}  {req.item_name} x{req.quantity}"
                + (f"  @{prefill}円" if prefill is not None else "")
                + (f"  «{req.note}»" if req.note else "")
            )
            ctk.CTkLabel(row, text=label, anchor="w").grid(row=0, column=0, sticky="w", padx=4)
            unit_entry = ctk.CTkEntry(row, width=70, placeholder_text="単価")
            if prefill is not None:
                unit_entry.insert(0, str(prefill))
            unit_entry.grid(row=0, column=1, padx=4)
            ctk.CTkButton(
                row, text="確定", width=56,
                command=lambda rid=req.request_id, e=unit_entry: self._cmd_confirm_order(rid, e),
            ).grid(row=0, column=2, padx=2)
            ctk.CTkButton(
                row, text="却下", width=56,
                command=lambda rid=req.request_id: self._cmd_reject_order(rid),
            ).grid(row=0, column=3, padx=2)
            self._order_rows.append(row)

    def _cmd_confirm_order(self, request_id: str, unit_entry) -> None:
        if self._order_repo is None:
            return
        unit = self._parse_amount(unit_entry.get())
        if unit is None or unit < 0:
            self._set_status("単価は 0 以上の整数で入力してください。", error=True)
            return
        try:
            self._order_repo.confirm_request(request_id, unit, self._ledger)
        except Exception as e:  # noqa: BLE001 — order / ledger の業務 error を表示
            self._set_status(str(e), error=True)
            return
        self._refresh()
        if self._selected_player_id:
            self._select_player(self._selected_player_id)
        self._set_status("注文を確定しました（order entry を作成）。")

    def _cmd_reject_order(self, request_id: str) -> None:
        if self._order_repo is None:
            return
        try:
            self._order_repo.reject_request(request_id)
        except Exception as e:  # noqa: BLE001
            self._set_status(str(e), error=True)
            return
        self._refresh_order_requests()
        self._set_status("注文を却下しました。")

    # ――― 起動 ―――

    def run(self) -> None:
        self._root.mainloop()
