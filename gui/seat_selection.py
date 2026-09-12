"""gui/seat_selection.py

Phase E3 (ISSUE-0006): seat → player_id を選ぶモーダルダイアログ。

hand logger の dashboard から「座席設定/変更」で開く。S2 session レイヤ
(`SessionRepository.assign_seat`) へ write-through する seat_player_map を組み立てる。
`config.session_layer.enabled=true` のときだけ dashboard から呼ばれる(既定 off では未使用)。

UX 方針(ISSUE-0006 決定):
  - セッション開始時に一度設定し、以後は必要時のみ本ダイアログで編集する
    (毎ハンドは出さず、最後に設定した seating を carry-forward する)。
  - 未登録プレイヤーはダイアログ内でその場作成できる(PlayerRepository.create_player)。
  - 空席(=未割当/離席)を許可し、その席は assign_seat しない。

customtkinter は __init__ 内で遅延 import する(module import は GUI 非依存)。
seat_player_map 構築・重複検証・初期選択解決は module-level の純関数に切り出し、
customtkinter 無しで unit test 可能にする(CI は GUI 非導入・skip 0)。
"""
from __future__ import annotations

import logging
from typing import Optional

from core.player_repository import (
    DuplicateDisplayNameError,
    EmptyDisplayNameError,
)

logger = logging.getLogger(__name__)

_EMPTY_SEAT_LABEL = "（空席）"
_STATUS_OK_COLOR = "#4CAF50"
_STATUS_ERROR_COLOR = "#F44336"


# ――― 純ロジック(customtkinter 非依存・テスト対象) ―――

def resolve_initial_selections(
    seats: list[int], initial_map: Optional[dict[int, str]]
) -> dict[int, Optional[str]]:
    """各 seat の初期選択(player_id or 未割当=None)を返す。

    `initial_map`(前回確定した seating)を carry-forward 表示するための解決。
    seats に無い席は無視し、initial_map に無い席は None。
    """
    initial = dict(initial_map or {})
    return {seat: initial.get(seat) for seat in seats}


def find_duplicate_player(selections: dict[int, Optional[str]]) -> Optional[str]:
    """同一 player_id が 2 席以上に割り当てられていれば、その player_id を返す(無ければ None)。

    空席(None)は無視する。`assign_seat` の player_already_seated と同じ意図の
    client 側 validation(UX 即時フィードバック用)。
    """
    seen: set[str] = set()
    for player_id in selections.values():
        if not player_id:
            continue
        if player_id in seen:
            return player_id
        seen.add(player_id)
    return None


def build_result_map(selections: dict[int, Optional[str]]) -> dict[int, str]:
    """空席(None/空文字)を除いた seat→player_id を返す(assign 対象のみ)。"""
    return {seat: player_id for seat, player_id in selections.items() if player_id}


# ――― モーダルダイアログ ―――

class SeatSelectionDialog:
    """seat→player_id をオペレータが選ぶモーダル Toplevel。

    使い方:
        dialog = SeatSelectionDialog(master, game_state, player_repo, initial_map)
        result = dialog.get_result()  # OK=dict[int,str] / キャンセル=None(ブロッキング)
    """

    def __init__(
        self,
        master: object,
        game_state: object,
        player_repo: object,
        initial_map: Optional[dict[int, str]] = None,
    ) -> None:
        import customtkinter as ctk

        self._ctk = ctk
        self._gs = game_state
        self._repo = player_repo
        self._seats: list[int] = sorted(game_state.get_stacks().keys())
        self._selections: dict[int, Optional[str]] = resolve_initial_selections(
            self._seats, initial_map
        )
        self._result: Optional[dict[int, str]] = None
        self._seat_vars: dict[int, object] = {}
        self._seat_menus: dict[int, object] = {}
        # display_name -> player_id (list_players は display_name 一意を保証)
        self._name_to_id: dict[str, str] = {}

        self._root = ctk.CTkToplevel(master)
        self._root.title("座席設定")
        self._root.geometry("440x520")
        self._root.resizable(True, True)
        self._build_ui()
        self._refresh_player_options()

    # ――― UI 構築 ―――

    def _build_ui(self) -> None:
        ctk = self._ctk
        root = self._root
        root.grid_rowconfigure(2, weight=1)
        root.grid_columnconfigure(0, weight=1)

        header = ctk.CTkLabel(root, text="座席 → プレイヤー", font=("", 16, "bold"))
        header.grid(row=0, column=0, sticky="w", padx=12, pady=(12, 4))

        # 新規プレイヤー作成 row(その場作成, ISSUE-0006)
        add_frame = ctk.CTkFrame(root)
        add_frame.grid(row=1, column=0, sticky="ew", padx=12, pady=4)
        add_frame.grid_columnconfigure(0, weight=1)
        self._new_name_entry = ctk.CTkEntry(add_frame, placeholder_text="新規プレイヤー名")
        self._new_name_entry.grid(row=0, column=0, sticky="ew", padx=(8, 4), pady=8)
        ctk.CTkButton(add_frame, text="追加", width=80, command=self._cmd_add_player).grid(
            row=0, column=1, padx=(4, 8), pady=8
        )

        # 席 → プレイヤー選択(scrollable)
        self._seat_frame = ctk.CTkScrollableFrame(root, label_text="席ごとに割り当て")
        self._seat_frame.grid(row=2, column=0, sticky="nsew", padx=12, pady=4)
        self._seat_frame.grid_columnconfigure(1, weight=1)
        for row_idx, seat in enumerate(self._seats):
            name = self._gs.get_player_name(seat)
            lbl = ctk.CTkLabel(self._seat_frame, text=f"席{seat} ({name})", anchor="w")
            lbl.grid(row=row_idx, column=0, sticky="w", padx=(4, 8), pady=3)
            var = ctk.StringVar(value=_EMPTY_SEAT_LABEL)
            menu = ctk.CTkOptionMenu(
                self._seat_frame, variable=var, values=[_EMPTY_SEAT_LABEL], width=200
            )
            menu.grid(row=row_idx, column=1, sticky="ew", padx=4, pady=3)
            self._seat_vars[seat] = var
            self._seat_menus[seat] = menu

        # ステータス + OK/キャンセル
        self._status_label = ctk.CTkLabel(root, text="", anchor="w")
        self._status_label.grid(row=3, column=0, sticky="ew", padx=12, pady=(0, 4))

        btns = ctk.CTkFrame(root, fg_color="transparent")
        btns.grid(row=4, column=0, sticky="ew", padx=12, pady=(0, 12))
        btns.grid_columnconfigure((0, 1), weight=1)
        ctk.CTkButton(btns, text="キャンセル", command=self._cmd_cancel).grid(
            row=0, column=0, padx=4, sticky="ew"
        )
        ctk.CTkButton(btns, text="OK", command=self._cmd_ok).grid(
            row=0, column=1, padx=4, sticky="ew"
        )

    def _refresh_player_options(self) -> None:
        """registry の最新一覧で各席の OptionMenu を更新し、現在の選択を反映する。"""
        players = self._repo.list_players()
        self._name_to_id = {p.display_name: p.player_id for p in players}
        id_to_name = {p.player_id: p.display_name for p in players}
        values = [_EMPTY_SEAT_LABEL] + [p.display_name for p in players]
        for seat in self._seats:
            menu = self._seat_menus[seat]
            menu.configure(values=values)
            current_id = self._selections.get(seat)
            label = (
                id_to_name.get(current_id, _EMPTY_SEAT_LABEL)
                if current_id
                else _EMPTY_SEAT_LABEL
            )
            self._seat_vars[seat].set(label)

    # ――― コマンド ―――

    def _set_status(self, message: str, error: bool = False) -> None:
        color = _STATUS_ERROR_COLOR if error else _STATUS_OK_COLOR
        self._status_label.configure(text=message, text_color=color)

    def _read_selections(self) -> dict[int, Optional[str]]:
        result: dict[int, Optional[str]] = {}
        for seat in self._seats:
            label = self._seat_vars[seat].get()
            result[seat] = (
                self._name_to_id.get(label) if label != _EMPTY_SEAT_LABEL else None
            )
        return result

    def _cmd_add_player(self) -> None:
        name = self._new_name_entry.get()
        try:
            player = self._repo.create_player(name)
        except EmptyDisplayNameError:
            self._set_status("プレイヤー名を入力してください。", error=True)
            return
        except DuplicateDisplayNameError as e:
            self._set_status(str(e), error=True)
            return
        # 現在の選択を保持したまま options を更新(新規分を追加)
        self._selections = self._read_selections()
        try:
            self._new_name_entry.delete(0, "end")
        except Exception:
            pass
        self._refresh_player_options()
        self._set_status(f"追加しました: {player.display_name}（席に割り当ててください）")

    def _cmd_ok(self) -> None:
        selections = self._read_selections()
        dup = find_duplicate_player(selections)
        if dup is not None:
            self._set_status("同じプレイヤーを複数の席に割り当てられません。", error=True)
            return
        self._result = build_result_map(selections)
        self._close()

    def _cmd_cancel(self) -> None:
        self._result = None
        self._close()

    def _close(self) -> None:
        try:
            self._root.grab_release()
        except Exception:
            pass
        self._root.destroy()

    # ――― 起動(モーダル) ―――

    def get_result(self) -> Optional[dict[int, str]]:
        """モーダル表示し、OK なら seat→player_id、キャンセルなら None を返す(ブロッキング)。"""
        try:
            self._root.grab_set()
        except Exception:
            pass
        self._root.wait_window()
        return self._result
