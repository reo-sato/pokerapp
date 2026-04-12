"""gui/card_master_view.py

FR-13 / FR-46: RFID タグ ID ↔ ポーカーカードコード の対応表編集 UI。

CardMasterView は ctk.CTkToplevel のサブクラスとして実装する。
customtkinter を遅延インポートすることで、tkinter が存在しない CI/ヘッドレス
環境でも `from gui.card_master_view import CardMasterView` が成功する。
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

logger = logging.getLogger(__name__)


# ── 遅延ロード（top-level ctk import を避ける） ──────────────────────────────

def __getattr__(name: str):
    """CardMasterView を最初にアクセスされたときだけ定義する。"""
    if name == "CardMasterView":
        _load_class()
        val = globals().get(name)
        if val is not None:
            return val
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _load_class() -> None:
    """CardMasterView クラスを定義してモジュール globals に注入する。"""
    try:
        import customtkinter as _ctk
    except ImportError:
        # tkinter が存在しない環境向けスタブ
        class CardMasterView:  # type: ignore[no-redef]
            """Stub CardMasterView (tkinter unavailable)."""
            def __init__(
                self,
                master=None,
                card_master=None,
                on_saved: Optional[Callable[[], None]] = None,
            ) -> None:
                pass

        globals()["CardMasterView"] = CardMasterView
        return

    from rfid.card_master import CardMaster, normalize_tag_id, VALID_CARDS

    class CardMasterView(_ctk.CTkToplevel):
        """カードマスタ（rfid_cards.json）の登録・編集 UI（FR-13, FR-46）。

        GUIDashboard の「カードマスタ編集」ボタンから起動するトップレベルウィンドウ。
        tag_id ↔ card_code のペアを一覧表示し、追加・削除・保存が可能。

        使い方:
            view = CardMasterView(
                master=app,
                card_master=cm,
                on_saved=refresh_callback,
            )
        """

        def __init__(
            self,
            master: _ctk.CTk,
            card_master: CardMaster,
            on_saved: Callable[[], None],
        ) -> None:
            super().__init__(master)
            self._cm = card_master
            self._on_saved = on_saved
            # 各行のウィジェット情報を保持するリスト
            # 要素: {"tag_var": StringVar, "card_var": StringVar,
            #         "sel_var": BooleanVar, "frame": CTkFrame}
            self._rows: list[dict] = []

            self.title("カードマスタ編集")
            self.geometry("640x500")
            self.resizable(True, True)
            self.grab_set()  # モーダル動作

            self._build_ui()
            self._load_entries()

        # ── UI 構築 ──────────────────────────────────────────────────────────

        def _build_ui(self) -> None:
            """ウィジェットを配置する。"""
            self.grid_rowconfigure(1, weight=1)
            self.grid_columnconfigure(0, weight=1)

            # ヘッダー
            hdr = _ctk.CTkFrame(self, height=44, corner_radius=0)
            hdr.grid(row=0, column=0, sticky="ew")
            hdr.grid_columnconfigure(0, weight=1)
            _ctk.CTkLabel(
                hdr,
                text="RFID タグ ↔ カードコード マスタ編集",
                font=("", 14, "bold"),
            ).grid(row=0, column=0, padx=16, pady=10, sticky="w")

            # 列ヘッダー
            col_hdr = _ctk.CTkFrame(self, height=28, corner_radius=0,
                                     fg_color="#2A2A2A")
            col_hdr.grid(row=1, column=0, sticky="ew")
            col_hdr.grid_columnconfigure(1, weight=1)
            _ctk.CTkLabel(col_hdr, text="", width=30).grid(
                row=0, column=0, padx=(8, 0))  # チェックボックス列
            _ctk.CTkLabel(col_hdr, text="タグ ID (例: 04:AB:CD:EF:12:34)",
                          font=("", 11, "bold"), anchor="w").grid(
                row=0, column=1, padx=8, sticky="w")
            _ctk.CTkLabel(col_hdr, text="カード (例: Ah)",
                          font=("", 11, "bold"), width=100, anchor="w").grid(
                row=0, column=2, padx=8, sticky="w")

            # スクロール可能なエントリ一覧
            self._scroll_frame = _ctk.CTkScrollableFrame(self)
            self._scroll_frame.grid(row=2, column=0, sticky="nsew",
                                    padx=8, pady=4)
            self._scroll_frame.grid_columnconfigure(1, weight=1)
            self.grid_rowconfigure(2, weight=1)

            # ボタンバー
            btn_bar = _ctk.CTkFrame(self, height=52, corner_radius=0)
            btn_bar.grid(row=3, column=0, sticky="ew", padx=0, pady=0)

            _ctk.CTkButton(
                btn_bar, text="＋ 追加", width=100,
                command=self._add_entry,
            ).pack(side="left", padx=10, pady=10)

            _ctk.CTkButton(
                btn_bar, text="選択削除", width=100, fg_color="#C0392B",
                hover_color="#96281B",
                command=self._remove_selected,
            ).pack(side="left", padx=4, pady=10)

            _ctk.CTkButton(
                btn_bar, text="保存", width=120, fg_color="#27AE60",
                hover_color="#1E8449",
                command=self._save,
            ).pack(side="right", padx=10, pady=10)

            # ステータスラベル
            self._lbl_status = _ctk.CTkLabel(
                btn_bar, text="", text_color="#888888", font=("", 11),
            )
            self._lbl_status.pack(side="right", padx=8)

        # ── データ読み込み ────────────────────────────────────────────────────

        def _load_entries(self) -> None:
            """CardMaster から現在のマッピングを読み込み一覧を更新する。"""
            # 既存行を全削除
            for row in self._rows:
                row["frame"].destroy()
            self._rows.clear()

            entries = self._cm.all_entries()
            for tag_id, card in sorted(entries.items()):
                self._append_row(tag_id=tag_id, card=card)

            self._lbl_status.configure(text=f"{len(entries)} 件登録済み")

        # ── 行操作 ───────────────────────────────────────────────────────────

        def _append_row(self, tag_id: str = "", card: str = "") -> None:
            """スクロールフレームに1行追加する。内部用。"""
            row_idx = len(self._rows)
            frame = _ctk.CTkFrame(self._scroll_frame, fg_color="transparent")
            frame.grid(row=row_idx, column=0, columnspan=3,
                       sticky="ew", padx=0, pady=1)
            frame.grid_columnconfigure(1, weight=1)

            sel_var = _ctk.BooleanVar(value=False)
            chk = _ctk.CTkCheckBox(frame, text="", variable=sel_var, width=28)
            chk.grid(row=0, column=0, padx=(4, 0))

            tag_var = _ctk.StringVar(value=tag_id)
            tag_entry = _ctk.CTkEntry(
                frame, textvariable=tag_var,
                placeholder_text="04:AB:CD:EF:12:34",
                font=("Courier", 11),
            )
            tag_entry.grid(row=0, column=1, sticky="ew", padx=6)

            card_var = _ctk.StringVar(value=card)
            card_entry = _ctk.CTkEntry(
                frame, textvariable=card_var,
                placeholder_text="Ah",
                width=80,
                font=("Courier", 11),
            )
            card_entry.grid(row=0, column=2, padx=6)

            self._rows.append({
                "frame":    frame,
                "tag_var":  tag_var,
                "card_var": card_var,
                "sel_var":  sel_var,
            })

        def _add_entry(self) -> None:
            """空行を追加して新規エントリを入力できるようにする。"""
            self._append_row()
            # 末尾へスクロール
            self._scroll_frame._parent_canvas.yview_moveto(1.0)

        def _remove_selected(self) -> None:
            """選択中（チェック済み）の行を一覧から削除する。"""
            remaining: list[dict] = []
            for row in self._rows:
                if row["sel_var"].get():
                    row["frame"].destroy()
                else:
                    remaining.append(row)
            self._rows = remaining
            # grid 行番号を詰め直す
            for i, row in enumerate(self._rows):
                row["frame"].grid(row=i)

        # ── 保存 ─────────────────────────────────────────────────────────────

        def _save(self) -> None:
            """編集内容を rfid_cards.json に保存し、_on_saved を呼び出す。"""
            new_mapping: dict[str, str] = {}
            errors: list[str] = []

            for i, row in enumerate(self._rows, start=1):
                tag_raw  = row["tag_var"].get().strip()
                card_raw = row["card_var"].get().strip()
                if not tag_raw and not card_raw:
                    continue  # 空行はスキップ
                if not tag_raw:
                    errors.append(f"行{i}: タグ ID が空です")
                    continue
                if card_raw not in VALID_CARDS:
                    errors.append(f"行{i}: 無効なカード '{card_raw}'")
                    continue
                try:
                    norm = normalize_tag_id(tag_raw)
                except Exception as e:
                    errors.append(f"行{i}: タグ ID エラー ({e})")
                    continue
                new_mapping[norm] = card_raw

            if errors:
                self._lbl_status.configure(
                    text="⚠ " + " / ".join(errors[:2]),
                    text_color="#E74C3C",
                )
                logger.warning("CardMasterView save errors: %s", errors)
                return

            # CardMaster の内部マッピングを置き換えて一括保存
            self._cm._mapping.clear()
            self._cm._mapping.update(new_mapping)
            self._cm.save()

            logger.info(
                "CardMasterView saved %d entries to %s",
                len(new_mapping), self._cm._path,
            )
            self._lbl_status.configure(
                text=f"保存完了 ({len(new_mapping)} 件)",
                text_color="#27AE60",
            )
            self._on_saved()

    globals()["CardMasterView"] = CardMasterView
