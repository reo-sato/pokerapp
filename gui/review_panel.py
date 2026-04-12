"""gui/review_panel.py

FR-42: needs_review フラグ付きアクションの手動確認・修正パネル。

ReviewPanel は ctk.CTkFrame のサブクラスとして実装する。
customtkinter を遅延インポートすることで、tkinter が存在しない CI/ヘッドレス
環境でも `from gui.review_panel import ReviewPanel` が成功する。
"""
from __future__ import annotations

import logging
from dataclasses import replace as _dc_replace
from typing import Callable

from core.hand_log import ActionRecord

logger = logging.getLogger(__name__)

_ACTIONS = ["bet", "raise", "call", "fold", "check", "allin"]


# ── 遅延ロード（top-level ctk import を避ける） ──────────────────────────────

def __getattr__(name: str):
    """ReviewPanel を最初にアクセスされたときだけ定義する。"""
    if name == "ReviewPanel":
        _load_class()
        val = globals().get(name)
        if val is not None:
            return val
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _load_class() -> None:
    """ReviewPanel クラスを定義してモジュール globals に注入する。"""
    try:
        import customtkinter as _ctk
    except ImportError:
        # tkinter が存在しない環境向けスタブ
        class ReviewPanel:  # type: ignore[no-redef]
            """Stub ReviewPanel (tkinter unavailable)."""
            def __init__(
                self,
                master=None,
                on_resolved: Callable[[ActionRecord, ActionRecord], None] | None = None,
                **kwargs,
            ) -> None:
                pass

            def add_review_item(self, record: ActionRecord) -> None:
                pass

            def clear(self) -> None:
                pass

        globals()["ReviewPanel"] = ReviewPanel
        return

    class ReviewPanel(_ctk.CTkFrame):
        """needs_review フラグ付きアクションの手動確認・修正パネル（FR-42）。

        IntegrationThread が needs_review=True のアクションを生成したとき、
        GUIDashboard から add_review_item() を呼び出して行を追加する。
        オペレーターが内容を確認・修正して「確定」ボタンを押すと
        on_resolved(old_record, new_record) が呼ばれる。

        on_resolved コールバック:
            (old_record: ActionRecord, new_record: ActionRecord) → None
            json_writer で旧レコードを新レコードで上書き保存するため両方渡す。

        使い方（DashboardWindow に埋め込む例）:
            panel = ReviewPanel(master=right_frame, on_resolved=handler)
            panel.pack(fill="both", expand=True)
        """

        def __init__(
            self,
            master: _ctk.CTk,
            on_resolved: Callable[[ActionRecord, ActionRecord], None],
            **kwargs,
        ) -> None:
            super().__init__(master, **kwargs)
            self._on_resolved = on_resolved
            # 各行の情報: {"record": ActionRecord, "frame": CTkFrame}
            self._rows: list[dict] = []

            self._build_ui()

        # ── UI 構築 ──────────────────────────────────────────────────────────

        def _build_ui(self) -> None:
            self.grid_rowconfigure(1, weight=1)
            self.grid_columnconfigure(0, weight=1)

            # ヘッダー行
            hdr = _ctk.CTkFrame(self, height=32, corner_radius=0,
                                 fg_color="#5D2D00")
            hdr.grid(row=0, column=0, sticky="ew")
            hdr.grid_columnconfigure(0, weight=1)

            self._lbl_title = _ctk.CTkLabel(
                hdr,
                text="⚠ 要確認アクション (0 件)",
                font=("", 11, "bold"),
                text_color="#FFD700",
                anchor="w",
            )
            self._lbl_title.grid(row=0, column=0, padx=10, pady=6, sticky="w")

            # スクロール可能な行エリア
            self._scroll = _ctk.CTkScrollableFrame(
                self, fg_color="transparent", label_text=""
            )
            self._scroll.grid(row=1, column=0, sticky="nsew", padx=2, pady=2)
            self._scroll.grid_columnconfigure(0, weight=1)

        # ── 公開 API ─────────────────────────────────────────────────────────

        def add_review_item(self, record: ActionRecord) -> None:
            """新たな needs_review レコードをパネルに追加する。"""
            frame = self._build_item_row(record)
            row_idx = len(self._rows)
            frame.grid(row=row_idx, column=0, sticky="ew", padx=2, pady=3)
            self._rows.append({"record": record, "frame": frame})
            self._update_title()
            logger.debug(
                "ReviewPanel: added item hand=%d seat=%d action=%s",
                record.hand_id, record.seat, record.action,
            )

        def clear(self) -> None:
            """すべての review 行を削除する（ハンド強制終了時等に使用）。"""
            for row in self._rows:
                row["frame"].destroy()
            self._rows.clear()
            self._update_title()

        # ── 行の生成 ─────────────────────────────────────────────────────────

        def _build_item_row(self, record: ActionRecord) -> _ctk.CTkFrame:
            """1 レコード分の確認行ウィジェットを生成して返す。"""
            frame = _ctk.CTkFrame(
                self._scroll,
                corner_radius=6,
                fg_color="#2C1A00",
                border_color="#FF9800",
                border_width=1,
            )
            frame.grid_columnconfigure(1, weight=1)

            # ─ 情報ラベル ─
            info_text = (
                f"H#{record.hand_id}  {record.street}"
                f"  席{record.seat} {record.player_name}"
                f"  conf={record.confidence:.2f}"
            )
            _ctk.CTkLabel(
                frame,
                text=info_text,
                font=("", 10),
                text_color="#FFCC88",
                anchor="w",
            ).grid(row=0, column=0, columnspan=5, padx=8, pady=(6, 2), sticky="w")

            # ─ アクション選択 ─
            _ctk.CTkLabel(frame, text="アクション:", font=("", 10)).grid(
                row=1, column=0, padx=(8, 2), pady=(2, 6), sticky="w"
            )
            action_val = record.action if record.action in _ACTIONS else _ACTIONS[0]
            action_var = _ctk.StringVar(value=action_val)
            _ctk.CTkOptionMenu(
                frame,
                variable=action_var,
                values=_ACTIONS,
                width=90,
                font=("", 11),
            ).grid(row=1, column=1, padx=4, pady=(2, 6), sticky="w")

            # ─ 金額 ─
            _ctk.CTkLabel(frame, text="金額:", font=("", 10)).grid(
                row=1, column=2, padx=(8, 2), pady=(2, 6), sticky="w"
            )
            amount_entry = _ctk.CTkEntry(frame, width=80, font=("Courier", 11))
            amount_entry.insert(0, str(record.amount))
            amount_entry.grid(row=1, column=3, padx=4, pady=(2, 6))

            # ─ アクター席 ─
            _ctk.CTkLabel(frame, text="席:", font=("", 10)).grid(
                row=1, column=4, padx=(8, 2), pady=(2, 6), sticky="w"
            )
            seat_entry = _ctk.CTkEntry(frame, width=50, font=("Courier", 11))
            seat_entry.insert(0, str(record.seat))
            seat_entry.grid(row=1, column=5, padx=4, pady=(2, 6))

            # ─ 確定ボタン ─
            def _on_confirm(
                _rec=record,
                _av=action_var,
                _ae=amount_entry,
                _se=seat_entry,
            ) -> None:
                try:
                    new_action = _av.get()
                    new_amount = int(_ae.get().strip() or "0")
                    new_seat   = int(_se.get().strip())
                except ValueError:
                    logger.warning(
                        "ReviewPanel: invalid input for record hand=%d seat=%d",
                        _rec.hand_id, _rec.seat,
                    )
                    return
                new_record = _dc_replace(
                    _rec,
                    action=new_action,
                    amount=new_amount,
                    seat=new_seat,
                    needs_review=False,
                )
                self._resolve(_rec, new_record)

            _ctk.CTkButton(
                frame,
                text="確定",
                width=60,
                height=26,
                fg_color="#27AE60",
                hover_color="#1E8449",
                command=_on_confirm,
            ).grid(row=1, column=6, padx=(8, 8), pady=(2, 6))

            return frame

        # ── 内部操作 ─────────────────────────────────────────────────────────

        def _resolve(
            self,
            old_record: ActionRecord,
            new_record: ActionRecord,
        ) -> None:
            """「確定」ボタン押下時に呼ばれる。on_resolved を呼び出してパネルから行を削除する。"""
            try:
                self._on_resolved(old_record, new_record)
            except Exception:
                logger.exception(
                    "ReviewPanel: on_resolved raised for hand=%d seat=%d",
                    old_record.hand_id, old_record.seat,
                )

            # 対応行を削除
            for i, row in enumerate(self._rows):
                if row["record"] is old_record:
                    row["frame"].destroy()
                    self._rows.pop(i)
                    break

            # 残り行の grid 番号を詰め直す
            for idx, row in enumerate(self._rows):
                row["frame"].grid(row=idx)

            self._update_title()

        def _update_title(self) -> None:
            n = len(self._rows)
            self._lbl_title.configure(
                text=f"⚠ 要確認アクション ({n} 件)",
                text_color="#FFD700" if n > 0 else "#888888",
            )

    globals()["ReviewPanel"] = ReviewPanel
