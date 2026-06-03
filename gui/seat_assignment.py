"""gui/seat_assignment.py

Phase 2.3: hand logger の最小 seat selection UX。

hand 開始時に「この hand の seat → player_id 割り当て」を確認・編集する
モーダルダイアログ。`session_layer.enabled == True` のときだけ hand logger
dashboard から開かれる（ADR-0008 Pattern A の seating 入力経路、ISSUE-0006）。

設計（最小スコープ）:
  - seat ごとに 1 行、player 選択コンボボックス（候補は PlayerRepository の display_name）。
  - 「（空席）」選択 = その seat を seating に入れない（= assign しない）。
  - carry-forward: 直前 hand の seating を初期値として表示（普通は無変更で OK）。
  - OK → seat→player_id を組み立てて on_confirm(seating) を呼ぶ。
  - キャンセル → seating を変えずに閉じる（その操作では write-through しない）。

scope 外（ISSUE-0006 に残す）: sitting_out / late entry 等の状態、未登録 player の
その場追加、seat change 履歴 UI、mobile との一貫性。
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable, Iterable

if TYPE_CHECKING:
    from core.player import Player

logger = logging.getLogger(__name__)

# 「player を割り当てない」を表すコンボボックスの選択肢ラベル
EMPTY_LABEL = "（空席）"


def build_seating_map(
    selections: dict[int, str], name_to_id: dict[str, str]
) -> dict[int, str]:
    """seat→display_name の選択を seat→player_id の seating dict に変換する。

    EMPTY_LABEL / 未知の display_name は seating に含めない（= 空席扱い）。
    純粋関数として切り出してテスト可能にする。
    """
    seating: dict[int, str] = {}
    for seat_no, label in selections.items():
        player_id = name_to_id.get(label)
        if player_id is not None:
            seating[seat_no] = player_id
    return seating


class SeatAssignmentDialog:
    """seat → player を選ぶモーダルダイアログ（customtkinter）。

    Args:
        parent:          親ウィンドウ（CTk）。
        ctk:             customtkinter モジュール（DI でテスト時にモック可能）。
        players:         候補 player（``Player`` のリスト。display_name/player_id を持つ）。
        current_seating: carry-forward 用の現在の ``seat_no -> player_id``。
        seats:           表示する seat 番号の列。
        on_confirm:      OK 時に ``seat_no -> player_id`` の seating を受け取るコールバック。
    """

    def __init__(
        self,
        parent: object,
        ctk: object,
        players: Iterable["Player"],
        current_seating: dict[int, str],
        seats: Iterable[int],
        on_confirm: Callable[[dict[int, str]], None],
    ) -> None:
        self._ctk = ctk
        self._players = list(players)
        self._seats = list(seats)
        self._on_confirm = on_confirm

        self._name_to_id = {p.display_name: p.player_id for p in self._players}
        self._id_to_name = {p.player_id: p.display_name for p in self._players}
        self._options = [EMPTY_LABEL] + [p.display_name for p in self._players]
        self._seat_vars: dict[int, object] = {}

        self._win = ctk.CTkToplevel(parent)
        self._win.title("席 → プレイヤー割り当て")
        try:
            self._win.grab_set()  # モーダル化（失敗してもダイアログ自体は機能する）
        except Exception:  # pragma: no cover - 環境依存
            pass
        self._build_ui(current_seating or {})

    def _build_ui(self, current_seating: dict[int, str]) -> None:
        ctk = self._ctk
        win = self._win

        ctk.CTkLabel(
            win, text="この hand の seat → player を選択してください。"
        ).grid(row=0, column=0, columnspan=2, padx=12, pady=(12, 6))

        for idx, seat_no in enumerate(self._seats, start=1):
            ctk.CTkLabel(win, text=f"席 {seat_no}").grid(
                row=idx, column=0, padx=12, pady=4, sticky="w"
            )
            # carry-forward: 現在の seating の player_id → display_name を初期選択に
            cur_pid = current_seating.get(seat_no)
            initial = self._id_to_name.get(cur_pid, EMPTY_LABEL)
            var = ctk.StringVar(value=initial)
            self._seat_vars[seat_no] = var
            ctk.CTkOptionMenu(
                win, variable=var, values=self._options, width=180
            ).grid(row=idx, column=1, padx=12, pady=4, sticky="e")

        btn_row = len(self._seats) + 1
        ctk.CTkButton(win, text="OK", width=90, command=self._cmd_ok).grid(
            row=btn_row, column=0, padx=12, pady=12
        )
        ctk.CTkButton(win, text="キャンセル", width=90, command=self._cmd_cancel).grid(
            row=btn_row, column=1, padx=12, pady=12
        )

    def _collect_selections(self) -> dict[int, str]:
        return {seat: var.get() for seat, var in self._seat_vars.items()}

    def _build_seating(self) -> dict[int, str]:
        return build_seating_map(self._collect_selections(), self._name_to_id)

    def _cmd_ok(self) -> None:
        seating = self._build_seating()
        logger.info("Seat assignment confirmed: %d seat(s)", len(seating))
        self._on_confirm(seating)
        self._close()

    def _cmd_cancel(self) -> None:
        logger.info("Seat assignment cancelled (seating unchanged)")
        self._close()

    def _close(self) -> None:
        try:
            self._win.grab_release()
        except Exception:  # pragma: no cover - 環境依存
            pass
        self._win.destroy()
