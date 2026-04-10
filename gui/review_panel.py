from __future__ import annotations

import logging
from typing import Callable

import customtkinter as ctk

from core.hand_log import ActionRecord

logger = logging.getLogger(__name__)


class ReviewPanel(ctk.CTkFrame):
    """needs_review フラグ付きアクションの手動確認・修正パネル（spec.md FR-42）。

    IntegrationThread が needs_review: true のアクションを生成したとき
    GUIDashboard から add_review_item() を呼び出して表示する。
    オペレーターが内容を確認・修正して「確定」ボタンを押すと on_resolved が呼ばれる。

    on_resolved コールバック:
        (old_record: ActionRecord, new_record: ActionRecord) → None
        json_writer で旧レコードを新レコードで上書き保存するため両方渡す。
    """

    # on_resolved: (旧レコード, 修正後レコード) → json_writer で上書き保存
    _on_resolved: Callable[[ActionRecord, ActionRecord], None]

    def __init__(
        self,
        master: ctk.CTk,
        on_resolved: Callable[[ActionRecord, ActionRecord], None],
    ) -> None: ...

    def add_review_item(self, record: ActionRecord) -> None:
        """新たな needs_review レコードをパネルに追加する。"""
        ...

    def _build_item_row(self, record: ActionRecord) -> ctk.CTkFrame:
        """1 レコード分の確認行ウィジェットを生成して返す。"""
        ...

    def _resolve(self, old_record: ActionRecord, new_record: ActionRecord) -> None:
        """「確定」ボタン押下時に呼ばれる。on_resolved を呼び出してパネルから行を削除する。"""
        ...

    def clear(self) -> None:
        """すべての review 行を削除する（ハンド強制終了時等に使用）。"""
        ...
