from __future__ import annotations

import logging
from typing import Callable

import customtkinter as ctk

from rfid.card_master import CardMaster

logger = logging.getLogger(__name__)


class CardMasterView(ctk.CTkToplevel):
    """カードマスタ（rfid_cards.json）の登録・編集 UI（spec.md FR-13, FR-46）。

    GUIDashboard から「カードマスタ編集」ボタンで起動するトップレベルウィンドウ。
    tag_id ↔ card_code のペアを一覧表示し、追加・削除・保存ができる。

    使い方:
        view = CardMasterView(master=app, card_master=cm, on_saved=refresh_callback)
    """

    _card_master: CardMaster
    _on_saved: Callable[[], None]

    def __init__(
        self,
        master: ctk.CTk,
        card_master: CardMaster,
        on_saved: Callable[[], None],
    ) -> None: ...

    def _build_ui(self) -> None:
        """ウィジェットを配置する。"""
        ...

    def _load_entries(self) -> None:
        """CardMaster から現在のマッピングを読み込み一覧を更新する。"""
        ...

    def _save(self) -> None:
        """編集内容を rfid_cards.json に保存し on_saved を呼び出す。"""
        ...

    def _add_entry(self) -> None:
        """空行を追加して新規エントリを入力できるようにする。"""
        ...

    def _remove_selected(self) -> None:
        """選択中の行を一覧から削除する。"""
        ...
