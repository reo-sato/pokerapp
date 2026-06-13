"""gui/player_registry.py

Phase S1: Player Registry Screen。

hand logger の dashboard (`gui/dashboard.py`) とは **完全に別画面** として実装する。
本画面は `GameStateManager` / `JsonWriter` 等の hand logger 依存を一切持たず、
`PlayerRepository` のみに依存する（CLAUDE.md § Future Scope / Phase candidates S1 参照）。

レイアウト:
  ┌──────────────────────────────────────┐
  │  プレイヤー登録                         │
  ├──────────────────────────────────────┤
  │  [新規名前入力] [追加]                  │
  ├──────────────────────────────────────┤
  │  player 一覧 (選択可)                   │
  │    display_name        [選択]          │
  │  ...                                    │
  ├──────────────────────────────────────┤
  │  選択中: ___  [リネーム入力] [リネーム]  │
  ├──────────────────────────────────────┤
  │  ステータス / validation メッセージ      │
  └──────────────────────────────────────┘

スレッド構成なし（hand logger と異なり録音スレッド等を持たない単純な CRUD 画面）。
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from core.player_repository import (
    DuplicateDisplayNameError,
    EmptyDisplayNameError,
    PlayerNotFoundError,
    PlayerRepository,
)

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

_STATUS_OK_COLOR = "#4CAF50"
_STATUS_ERROR_COLOR = "#F44336"


class PlayerRegistryWindow:
    """player の新規作成・一覧表示・リネームを行う独立画面。

    使い方:
        repo = PlayerRepository("./players.json")
        win = PlayerRegistryWindow(repository=repo)
        win.run()  # mainloop 開始（ブロッキング）

    既存アプリ内から開く場合は master を渡すと Toplevel として開く。
    """

    def __init__(
        self,
        repository: PlayerRepository,
        master: Optional[object] = None,
    ) -> None:
        import customtkinter as ctk

        self._repo = repository
        self._selected_id: Optional[str] = None
        self._player_rows: dict[str, object] = {}

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self._ctk = ctk
        self._owns_root = master is None
        if master is None:
            self._root = ctk.CTk()
        else:
            self._root = ctk.CTkToplevel(master)
        self._root.title("プレイヤー登録")
        self._root.geometry("460x560")
        self._root.resizable(True, True)

        self._build_ui()
        self._refresh_player_list()

    # ――― UI 構築 ―――

    def _build_ui(self) -> None:
        ctk = self._ctk
        root = self._root
        root.grid_rowconfigure(2, weight=1)
        root.grid_columnconfigure(0, weight=1)

        # ヘッダー
        header = ctk.CTkLabel(root, text="プレイヤー登録", font=("", 16, "bold"))
        header.grid(row=0, column=0, sticky="w", padx=12, pady=(12, 4))

        # 新規作成 row
        add_frame = ctk.CTkFrame(root)
        add_frame.grid(row=1, column=0, sticky="ew", padx=12, pady=4)
        add_frame.grid_columnconfigure(0, weight=1)
        self._name_entry = ctk.CTkEntry(add_frame, placeholder_text="新しい表示名")
        self._name_entry.grid(row=0, column=0, sticky="ew", padx=(8, 4), pady=8)
        ctk.CTkButton(add_frame, text="追加", width=80, command=self._cmd_add).grid(
            row=0, column=1, padx=(4, 8), pady=8
        )

        # player 一覧
        self._list_frame = ctk.CTkScrollableFrame(root, label_text="登録済みプレイヤー")
        self._list_frame.grid(row=2, column=0, sticky="nsew", padx=12, pady=4)
        self._list_frame.grid_columnconfigure(0, weight=1)

        # リネーム row
        rename_frame = ctk.CTkFrame(root)
        rename_frame.grid(row=3, column=0, sticky="ew", padx=12, pady=4)
        rename_frame.grid_columnconfigure(1, weight=1)
        self._selected_label = ctk.CTkLabel(rename_frame, text="選択中: —", anchor="w")
        self._selected_label.grid(row=0, column=0, columnspan=3, sticky="w", padx=8, pady=(8, 0))
        self._rename_entry = ctk.CTkEntry(rename_frame, placeholder_text="新しい表示名")
        self._rename_entry.grid(row=1, column=0, columnspan=2, sticky="ew", padx=(8, 4), pady=8)
        ctk.CTkButton(rename_frame, text="リネーム", width=80, command=self._cmd_rename).grid(
            row=1, column=2, padx=(4, 8), pady=8
        )

        # ステータス / validation メッセージ
        self._status_label = ctk.CTkLabel(root, text="", anchor="w")
        self._status_label.grid(row=4, column=0, sticky="ew", padx=12, pady=(0, 12))

    def _refresh_player_list(self) -> None:
        ctk = self._ctk
        for widget in list(self._player_rows.values()):
            try:
                widget.destroy()
            except Exception:
                pass
        self._player_rows.clear()

        for row_idx, player in enumerate(self._repo.list_players()):
            row = ctk.CTkFrame(self._list_frame, fg_color="transparent")
            row.grid(row=row_idx, column=0, sticky="ew", pady=1)
            row.grid_columnconfigure(0, weight=1)
            name_lbl = ctk.CTkLabel(row, text=player.display_name, anchor="w")
            name_lbl.grid(row=0, column=0, sticky="w", padx=4)
            ctk.CTkButton(
                row, text="選択", width=60,
                command=lambda pid=player.player_id: self._select_player(pid),
            ).grid(row=0, column=1, padx=4)
            self._player_rows[player.player_id] = row

    # ――― コマンド ―――

    def _set_status(self, message: str, error: bool = False) -> None:
        color = _STATUS_ERROR_COLOR if error else _STATUS_OK_COLOR
        self._status_label.configure(text=message, text_color=color)

    def _select_player(self, player_id: str) -> None:
        try:
            player = self._repo.get(player_id)
        except PlayerNotFoundError:
            self._set_status("選択した player が見つかりません。", error=True)
            return
        self._selected_id = player_id
        self._selected_label.configure(text=f"選択中: {player.display_name}")
        self._set_status(f"選択しました: {player.display_name}")

    def _cmd_add(self) -> None:
        name = self._name_entry.get()
        try:
            player = self._repo.create_player(name)
        except EmptyDisplayNameError:
            self._set_status("表示名を入力してください。", error=True)
            return
        except DuplicateDisplayNameError as e:
            self._set_status(str(e), error=True)
            return
        try:
            self._name_entry.delete(0, "end")
        except Exception:
            pass
        self._refresh_player_list()
        self._set_status(f"追加しました: {player.display_name}")

    def _cmd_rename(self) -> None:
        if not self._selected_id:
            self._set_status("リネーム対象を選択してください。", error=True)
            return
        new_name = self._rename_entry.get()
        try:
            player = self._repo.rename_player(self._selected_id, new_name)
        except EmptyDisplayNameError:
            self._set_status("表示名を入力してください。", error=True)
            return
        except DuplicateDisplayNameError as e:
            self._set_status(str(e), error=True)
            return
        except PlayerNotFoundError:
            self._set_status("選択した player が見つかりません。", error=True)
            return
        self._selected_label.configure(text=f"選択中: {player.display_name}")
        self._refresh_player_list()
        self._set_status(f"リネームしました: {player.display_name}")

    # ――― 起動 ―――

    def run(self) -> None:
        self._root.mainloop()
