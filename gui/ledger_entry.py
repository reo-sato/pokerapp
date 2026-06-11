"""gui/ledger_entry.py

Phase S3a (M4, ADR-0014): スタッフ用 ledger 入力画面。

hand logger の dashboard とは **完全に別画面**（WS2 原則）。`LedgerRepository` /
`SessionRepository` / `PlayerRepository` のみに依存し、validation は core が source of truth
（本画面は error code を表示するだけ）。

レイアウト:
  ┌────────────────────────────────────────────┐
  │  会計入力 (ledger)                            │
  ├────────────────────────────────────────────┤
  │  セッション: [OptionMenu(open のみ)]  [更新]  │
  ├────────────────────────────────────────────┤
  │  player [▼] 種別 [▼] 金額 [   ] メモ [    ]   │
  │  (種別=注文: 品名 [  ] 単価 [  ] 数量 [  ])   │
  │  [追加]                                       │
  ├────────────────────────────────────────────┤
  │  中間集計 (player ごと buy-in/注文/調整/合計)  │
  │  エントリ履歴                                  │
  ├────────────────────────────────────────────┤
  │  ステータス / validation メッセージ            │
  └────────────────────────────────────────────┘
"""
from __future__ import annotations

import logging
from typing import Optional

from core.ledger_repository import LedgerError, LedgerRepository
from core.player_repository import PlayerRepository
from core.session_repository import SessionNotFoundError, SessionRepository

logger = logging.getLogger(__name__)

_STATUS_OK_COLOR = "#4CAF50"
_STATUS_ERROR_COLOR = "#F44336"

_KIND_LABELS = {
    "buy_in": "バイイン",
    "rebuy": "リバイ",
    "add_on": "アドオン",
    "order": "注文",
    "adjustment": "調整",
}
_LABEL_KINDS = {v: k for k, v in _KIND_LABELS.items()}


class LedgerEntryWindow:
    """open session への ledger entry 追加と中間集計表示を行う独立画面。

    使い方:
        win = LedgerEntryWindow(ledger_repo, session_repo, player_repo)
        win.run()  # mainloop 開始（ブロッキング）
    """

    def __init__(
        self,
        ledger_repo: LedgerRepository,
        session_repo: SessionRepository,
        player_repo: PlayerRepository,
        master: Optional[object] = None,
        order_repo: Optional[object] = None,
        menu: Optional[object] = None,
    ) -> None:
        """
        Args:
            order_repo: OrderRequestRepository (M5, ADR-0015)。渡すと「注文リクエスト」欄が
                        現れ、pending を確定（ledger 記帳）/ 却下できる。in-process viewer API
                        スレッドと共有されるため thread-safe（repository 側 lock）。
            menu: MenuMaster。確定時の単価 prefill に使う（スタッフ上書き可）。
        """
        import customtkinter as ctk

        self._ledger = ledger_repo
        self._sessions = session_repo
        self._players = player_repo
        self._orders = order_repo
        self._menu = menu
        self._request_rows: dict[str, object] = {}  # request_id → row frame
        # 表示ラベル → ID の対応（OptionMenu はラベルで選ぶ）
        self._session_by_label: dict[str, str] = {}
        self._player_by_label: dict[str, str] = {}

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self._ctk = ctk
        if master is None:
            self._root = ctk.CTk()
        else:
            self._root = ctk.CTkToplevel(master)
        self._root.title("会計入力 (ledger)")
        self._root.geometry("720x640")
        self._root.resizable(True, True)

        self._build_ui()
        self._refresh_sessions()
        if self._orders is not None:
            self._poll_requests()

    # ――― UI 構築 ―――

    def _build_ui(self) -> None:
        ctk = self._ctk
        root = self._root
        root.grid_rowconfigure(3, weight=1)
        root.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(root, text="会計入力 (ledger)", font=("", 16, "bold")).grid(
            row=0, column=0, sticky="w", padx=12, pady=(12, 4))

        # セッション選択
        sess_frame = ctk.CTkFrame(root)
        sess_frame.grid(row=1, column=0, sticky="ew", padx=12, pady=4)
        sess_frame.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(sess_frame, text="セッション:").grid(row=0, column=0, padx=(8, 4), pady=8)
        self._session_var = ctk.StringVar(value="")
        self._session_menu = ctk.CTkOptionMenu(
            sess_frame, variable=self._session_var, values=[""],
            command=lambda _v: self._refresh_board())
        self._session_menu.grid(row=0, column=1, sticky="ew", padx=4, pady=8)
        ctk.CTkButton(sess_frame, text="更新", width=70, command=self._refresh_sessions).grid(
            row=0, column=2, padx=(4, 8), pady=8)

        # 入力 row
        entry_frame = ctk.CTkFrame(root)
        entry_frame.grid(row=2, column=0, sticky="ew", padx=12, pady=4)
        for col in (1, 3, 5, 7):
            entry_frame.grid_columnconfigure(col, weight=1)

        ctk.CTkLabel(entry_frame, text="player:").grid(row=0, column=0, padx=(8, 2), pady=6)
        self._player_var = ctk.StringVar(value="")
        self._player_menu = ctk.CTkOptionMenu(entry_frame, variable=self._player_var, values=[""])
        self._player_menu.grid(row=0, column=1, sticky="ew", padx=2, pady=6)

        ctk.CTkLabel(entry_frame, text="種別:").grid(row=0, column=2, padx=(8, 2), pady=6)
        self._kind_var = ctk.StringVar(value=_KIND_LABELS["buy_in"])
        ctk.CTkOptionMenu(
            entry_frame, variable=self._kind_var, values=list(_KIND_LABELS.values()),
            command=lambda _v: self._toggle_order_row(),
        ).grid(row=0, column=3, sticky="ew", padx=2, pady=6)

        ctk.CTkLabel(entry_frame, text="金額:").grid(row=0, column=4, padx=(8, 2), pady=6)
        self._amount_entry = ctk.CTkEntry(entry_frame, width=90, placeholder_text="現金額")
        self._amount_entry.grid(row=0, column=5, sticky="ew", padx=2, pady=6)

        ctk.CTkLabel(entry_frame, text="メモ:").grid(row=0, column=6, padx=(8, 2), pady=6)
        self._note_entry = ctk.CTkEntry(entry_frame, placeholder_text="任意")
        self._note_entry.grid(row=0, column=7, sticky="ew", padx=2, pady=6)

        # 注文明細 row（種別=注文のときのみ表示。金額は単価×数量で自動計算）
        self._order_frame = ctk.CTkFrame(entry_frame, fg_color="transparent")
        self._order_frame.grid(row=1, column=0, columnspan=8, sticky="ew", padx=4)
        self._order_frame.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(self._order_frame, text="品名:").grid(row=0, column=0, padx=(4, 2), pady=4)
        self._item_entry = ctk.CTkEntry(self._order_frame, placeholder_text="ジントニック 等")
        self._item_entry.grid(row=0, column=1, sticky="ew", padx=2, pady=4)
        ctk.CTkLabel(self._order_frame, text="単価:").grid(row=0, column=2, padx=(8, 2), pady=4)
        self._unit_entry = ctk.CTkEntry(self._order_frame, width=80)
        self._unit_entry.grid(row=0, column=3, padx=2, pady=4)
        ctk.CTkLabel(self._order_frame, text="数量:").grid(row=0, column=4, padx=(8, 2), pady=4)
        self._qty_entry = ctk.CTkEntry(self._order_frame, width=60)
        self._qty_entry.grid(row=0, column=5, padx=2, pady=4)
        self._order_frame.grid_remove()  # 既定 kind=buy_in なので隠す

        ctk.CTkButton(entry_frame, text="追加", width=100, command=self._cmd_add).grid(
            row=2, column=7, sticky="e", padx=4, pady=(2, 8))

        # 注文リクエスト欄 (M5)。order_repo がある場合のみ
        if self._orders is not None:
            self._requests_frame = ctk.CTkScrollableFrame(
                root, label_text="注文リクエスト（pending — スマホから受信）", height=120)
            self._requests_frame.grid(row=3, column=0, sticky="ew", padx=12, pady=4)
            self._requests_frame.grid_columnconfigure(0, weight=1)
            root.grid_rowconfigure(3, weight=0)
            root.grid_rowconfigure(4, weight=1)
            board_row, status_row = 4, 5
        else:
            board_row, status_row = 3, 4

        # 集計 + 履歴
        self._board = ctk.CTkTextbox(root, state="disabled", wrap="none", font=("Courier", 12))
        self._board.grid(row=board_row, column=0, sticky="nsew", padx=12, pady=4)

        self._status_label = ctk.CTkLabel(root, text="", anchor="w")
        self._status_label.grid(row=status_row, column=0, sticky="ew", padx=12, pady=(0, 12))

    # ――― 表示更新 ―――

    def _set_status(self, message: str, error: bool = False) -> None:
        color = _STATUS_ERROR_COLOR if error else _STATUS_OK_COLOR
        self._status_label.configure(text=message, text_color=color)

    def _toggle_order_row(self) -> None:
        if _LABEL_KINDS.get(self._kind_var.get()) == "order":
            self._order_frame.grid()
        else:
            self._order_frame.grid_remove()

    def _refresh_sessions(self) -> None:
        """open session と player 一覧を取り直して OptionMenu を更新する。"""
        self._session_by_label = {}
        for s in self._sessions.list_sessions():
            if s.status != "open":
                continue
            label = f"{s.label or '(ラベルなし)'}  [{s.started_at}  {s.session_id[:8]}…]"
            self._session_by_label[label] = s.session_id
        labels = list(self._session_by_label) or ["(open なセッションがありません)"]
        self._session_menu.configure(values=labels)
        if self._session_var.get() not in self._session_by_label:
            self._session_var.set(labels[0])

        self._player_by_label = {
            p.display_name: p.player_id for p in self._players.list_players()
        }
        plabels = list(self._player_by_label) or ["(player 未登録)"]
        self._player_menu.configure(values=plabels)
        if self._player_var.get() not in self._player_by_label:
            self._player_var.set(plabels[0])
        self._refresh_board()

    def _refresh_board(self) -> None:
        """選択 session の player 別中間集計とエントリ履歴を表示する。"""
        session_id = self._session_by_label.get(self._session_var.get())
        box = self._board
        box.configure(state="normal")
        box.delete("1.0", "end")
        if session_id is not None:
            try:
                entries = self._ledger.list_entries(session_id)
            except SessionNotFoundError:
                entries = []
            box.insert("end", "── 中間集計（確定値ではありません） ──\n")
            id_to_name = {pid: name for name, pid in self._player_by_label.items()}
            for pid in sorted({e.player_id for e in entries}, key=lambda p: id_to_name.get(p, p)):
                s = self._ledger.session_player_summary(session_id, pid)
                box.insert(
                    "end",
                    f"{id_to_name.get(pid, pid):<12} buy-in {s['buy_in_total']:>8,}  "
                    f"注文 {s['order_total']:>7,}  調整 {s['adjustment_total']:>7,}  "
                    f"合計 {s['total_due']:>8,}\n",
                )
            box.insert("end", f"\n── エントリ履歴（{len(entries)} 件） ──\n")
            for e in entries:
                detail = ""
                if e.order is not None:
                    detail = f" {e.order.item_name}×{e.order.quantity}"
                note = f"  ({e.note})" if e.note else ""
                box.insert(
                    "end",
                    f"{e.occurred_at}  {id_to_name.get(e.player_id, e.player_id):<12} "
                    f"{_KIND_LABELS.get(e.kind, e.kind):<6} {e.cash_amount:>8,}{detail}{note}\n",
                )
        box.configure(state="disabled")

    # ――― 注文リクエスト (M5) ―――

    def _poll_requests(self) -> None:
        """2 秒ごとに選択 session の pending リクエストを取り直して欄を更新する。

        repository は thread-safe（in-process API スレッドが create する）。
        """
        try:
            self._rebuild_request_rows()
        except Exception:
            logger.exception("注文リクエスト欄の更新に失敗")
        self._root.after(2000, self._poll_requests)

    def _pending_requests(self) -> list:
        session_id = self._session_by_label.get(self._session_var.get())
        if session_id is None:
            return []
        try:
            return self._orders.list_requests(session_id, status="pending")
        except SessionNotFoundError:
            return []

    def _rebuild_request_rows(self) -> None:
        ctk = self._ctk
        pending = self._pending_requests()
        ids = [r.request_id for r in pending]
        if ids == list(self._request_rows):
            return  # 変化なし（入力中の単価 entry を壊さない）
        for row in self._request_rows.values():
            try:
                row.destroy()
            except Exception:
                pass
        self._request_rows.clear()

        id_to_name = {pid: name for name, pid in self._player_by_label.items()}
        for row_idx, req in enumerate(pending):
            row = ctk.CTkFrame(self._requests_frame, fg_color="transparent")
            row.grid(row=row_idx, column=0, sticky="ew", pady=1)
            row.grid_columnconfigure(0, weight=1)
            note = f"（{req.note}）" if req.note else ""
            label = (f"{req.requested_at}  {id_to_name.get(req.player_id, req.player_id)}  "
                     f"{req.item_name} ×{req.quantity} {note}")
            ctk.CTkLabel(row, text=label, anchor="w").grid(row=0, column=0, sticky="w", padx=4)
            unit_entry = ctk.CTkEntry(row, width=70, placeholder_text="単価")
            prefill = self._menu.unit_amount(req.item_name) if self._menu is not None else None
            if prefill is not None:
                unit_entry.insert(0, str(prefill))
            unit_entry.grid(row=0, column=1, padx=2)
            ctk.CTkButton(
                row, text="確定", width=60,
                command=lambda rid=req.request_id, e=unit_entry: self._cmd_confirm_request(rid, e),
            ).grid(row=0, column=2, padx=2)
            ctk.CTkButton(
                row, text="却下", width=60, fg_color="#774444",
                command=lambda rid=req.request_id: self._cmd_reject_request(rid),
            ).grid(row=0, column=3, padx=(2, 4))
            self._request_rows[req.request_id] = row

    def _cmd_confirm_request(self, request_id: str, unit_entry: object) -> None:
        """pending を確定し ledger に記帳する（単価はスタッフが最終決定, ADR-0015 §4）。"""
        from core.order_request_repository import OrderRequestError

        try:
            unit_amount = int(unit_entry.get().strip())
        except ValueError:
            self._set_status("単価は整数で入力してください。", error=True)
            return
        try:
            req = self._orders.confirm_request(request_id, unit_amount, self._ledger)
        except (OrderRequestError, LedgerError, SessionNotFoundError) as e:
            self._set_status(str(e), error=True)
            return
        self._rebuild_request_rows()
        self._refresh_board()
        self._set_status(f"注文を確定しました: {req.item_name} ×{req.quantity} = "
                         f"{unit_amount * req.quantity:,}")

    def _cmd_reject_request(self, request_id: str) -> None:
        from core.order_request_repository import OrderRequestError

        try:
            self._orders.reject_request(request_id)
        except OrderRequestError as e:
            self._set_status(str(e), error=True)
            return
        self._rebuild_request_rows()
        self._set_status("注文を却下しました。")

    # ――― コマンド ―――

    def _cmd_add(self) -> None:
        session_id = self._session_by_label.get(self._session_var.get())
        player_id = self._player_by_label.get(self._player_var.get())
        if session_id is None:
            self._set_status("open なセッションを選択してください。", error=True)
            return
        if player_id is None:
            self._set_status("player を選択してください（--players で登録）。", error=True)
            return
        kind = _LABEL_KINDS[self._kind_var.get()]
        note = self._note_entry.get().strip() or None

        item_name = unit_amount = quantity = None
        try:
            if kind == "order":
                item_name = self._item_entry.get().strip()
                unit_amount = int(self._unit_entry.get().strip())
                quantity = int(self._qty_entry.get().strip())
                cash_amount = unit_amount * quantity  # cash-only: 金額は明細から自動計算
            else:
                cash_amount = int(self._amount_entry.get().strip())
        except ValueError:
            self._set_status("金額・単価・数量は整数で入力してください。", error=True)
            return

        try:
            entry = self._ledger.add_entry(
                session_id, player_id, kind, cash_amount, note=note,
                item_name=item_name, unit_amount=unit_amount, quantity=quantity,
            )
        except (LedgerError, SessionNotFoundError) as e:
            self._set_status(str(e), error=True)
            return

        self._refresh_board()
        self._set_status(
            f"追加しました: {self._player_var.get()} {self._kind_var.get()} {entry.cash_amount:,}"
        )

    # ――― 起動 ―――

    def run(self) -> None:
        self._root.mainloop()
