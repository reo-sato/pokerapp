"""gui/session_viewer.py

WS2-α: Session / Seating Viewer（desktop, read-only, 最小版）。

S2 core（`core/session*.py`）と Phase 2.2 / 2.3 で session / seating が hand logger と
接続されたので、その状態を **人間が壊れない形で覗ける read-only GUI** を提供する。
hand logger（`gui/dashboard.py`）/ player registry（`gui/player_registry.py`）とは
**別ウィンドウ**で、編集機能は一切持たない（inspection 専用）。

依存:
  - ``SessionRepository`` … session / seating の read（source of truth は repository 側）。
  - ``PlayerRepository``  … ``player_id -> display_name`` 解決。

レイアウト（α）:
  ┌──────────────┬──────────────────────────────────────┐
  │ session list │  概要 (session_id / status / 時刻 / blinds) │
  │ (選択可)      ├──────────────────────────────────────┤
  │              │  current seating (seat / player_id / 名前) │
  │              ├──────────────────────────────────────┤
  │              │  hand assignments (hand / seat / 名前)    │
  ├──────────────┴──────────────────────────────────────┤
  │  [Refresh]  ステータス                                  │
  └──────────────────────────────────────────────────────┘

state は「起動時 read → Refresh で再読込」のシンプルモデル。auto-refresh はしない。
business ルール（保存・採番・validation）は repository 側の責務であり、本 viewer は
shape を整えて表示するだけ。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from core.player_repository import PlayerRepository
    from core.session_repository import SessionRepository

logger = logging.getLogger(__name__)

# player_id が registry で解決できなかったときの表示ラベル
UNKNOWN_LABEL = "(unknown)"


# ――― view model（pure, テスト可能） ―――

@dataclass
class SeatRow:
    seat_no: int
    player_id: str
    display_name: str


@dataclass
class HandAssignmentRow:
    hand_id: int
    seat_no: int
    player_id: str
    display_name: str


@dataclass
class SessionDetail:
    session_id: str
    status: str
    started_at: str
    ended_at: Optional[str]
    blinds: Optional[dict]
    current_seating: list[SeatRow] = field(default_factory=list)
    hand_assignments: list[HandAssignmentRow] = field(default_factory=list)


def resolve_display_name(player_id: str, name_map: dict[str, str]) -> str:
    """player_id を display_name に解決する。未解決は UNKNOWN_LABEL。"""
    return name_map.get(player_id, UNKNOWN_LABEL)


def build_name_map(player_repo: "PlayerRepository") -> dict[str, str]:
    """``player_id -> display_name`` の dict を一括構築する。"""
    return {p.player_id: p.display_name for p in player_repo.list_players()}


def build_session_detail(
    session_repo: "SessionRepository",
    name_map: dict[str, str],
    session_id: str,
) -> SessionDetail:
    """1 session の概要 + current seating + hand assignments を view model 化する。

    repository の read API のみを使う（read-only）。
    """
    session = session_repo.get_session(session_id)

    current = [
        SeatRow(
            seat_no=sa.seat_no,
            player_id=sa.player_id,
            display_name=resolve_display_name(sa.player_id, name_map),
        )
        for sa in session_repo.current_seating(session_id)
    ]

    assignments: list[HandAssignmentRow] = []
    for hand_id in session_repo.list_hand_ids(session_id):
        for sa in session_repo.list_seat_assignments(session_id, hand_id):
            assignments.append(
                HandAssignmentRow(
                    hand_id=hand_id,
                    seat_no=sa.seat_no,
                    player_id=sa.player_id,
                    display_name=resolve_display_name(sa.player_id, name_map),
                )
            )

    return SessionDetail(
        session_id=session.session_id,
        status=session.status,
        started_at=session.started_at,
        ended_at=session.ended_at,
        blinds=session.blinds,
        current_seating=current,
        hand_assignments=assignments,
    )


def _short_id(session_id: str, width: int = 8) -> str:
    """session_id を一覧用に短縮表示する（UUID4 hex の先頭など）。"""
    if len(session_id) <= width:
        return session_id
    return session_id[:width] + "…"


def _format_blinds(blinds: Optional[dict]) -> str:
    if not blinds:
        return "—"
    sb = blinds.get("sb", "?")
    bb = blinds.get("bb", "?")
    return f"SB {sb} / BB {bb}"


_STATUS_COLOR = "#9E9E9E"


class SessionViewerWindow:
    """session / seating を読むだけの独立ウィンドウ（read-only）。

    使い方:
        win = SessionViewerWindow(session_repo=srepo, player_repo=prepo)
        win.run()  # mainloop（ブロッキング）

    既存アプリ内から開く場合は master を渡すと Toplevel として開く。
    """

    def __init__(
        self,
        session_repo: "SessionRepository",
        player_repo: "PlayerRepository",
        master: Optional[object] = None,
    ) -> None:
        import customtkinter as ctk

        self._session_repo = session_repo
        self._player_repo = player_repo

        # 内部 state
        self._sessions: list = []
        self._name_map: dict[str, str] = {}
        self._selected_id: Optional[str] = None
        self._current_detail: Optional[SessionDetail] = None
        self._last_status: str = ""
        self._session_rows: dict[str, object] = {}
        self._detail_widgets: list = []

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        self._ctk = ctk

        self._owns_root = master is None
        self._root = ctk.CTk() if master is None else ctk.CTkToplevel(master)
        self._root.title("セッション / シート ビューア (read-only)")
        self._root.geometry("760x560")
        self._root.resizable(True, True)

        self._build_ui()
        self._reload_data()

    # ――― UI 構築 ―――

    def _build_ui(self) -> None:
        ctk = self._ctk
        root = self._root
        root.grid_rowconfigure(0, weight=1)
        root.grid_columnconfigure(0, weight=0)
        root.grid_columnconfigure(1, weight=1)

        # 左: session list
        self._list_frame = ctk.CTkScrollableFrame(
            root, width=220, label_text="セッション一覧"
        )
        self._list_frame.grid(row=0, column=0, sticky="nsew", padx=(8, 4), pady=8)
        self._list_frame.grid_columnconfigure(0, weight=1)

        # 右: details
        self._detail_frame = ctk.CTkScrollableFrame(root, label_text="詳細")
        self._detail_frame.grid(row=0, column=1, sticky="nsew", padx=(4, 8), pady=8)
        self._detail_frame.grid_columnconfigure(0, weight=1)

        # 下: refresh + status
        bottom = ctk.CTkFrame(root, height=44, corner_radius=0)
        bottom.grid(row=1, column=0, columnspan=2, sticky="ew", padx=0, pady=0)
        bottom.grid_columnconfigure(1, weight=1)
        ctk.CTkButton(bottom, text="Refresh", width=100, command=self._cmd_refresh).grid(
            row=0, column=0, padx=8, pady=8
        )
        self._status_label = ctk.CTkLabel(bottom, text="", anchor="w")
        self._status_label.grid(row=0, column=1, sticky="ew", padx=8, pady=8)

    # ――― data 読み込み（read-only） ―――

    def _reload_data(self) -> None:
        """repository から最新を読み直し、selection を保ちつつ UI を再構築する。"""
        self._sessions = self._session_repo.list_sessions()
        self._name_map = build_name_map(self._player_repo)

        ids = [s.session_id for s in self._sessions]
        if self._selected_id not in ids:
            self._selected_id = ids[0] if ids else None

        self._render_session_list()
        self._render_detail()
        self._set_status(f"{len(self._sessions)} 件のセッション（最終読み込み済み）。")

    def _cmd_refresh(self) -> None:
        self._reload_data()

    def _select_session(self, session_id: str) -> None:
        self._selected_id = session_id
        self._render_detail()

    # ――― 描画 ―――

    def _render_session_list(self) -> None:
        ctk = self._ctk
        for widget in list(self._session_rows.values()):
            try:
                widget.destroy()
            except Exception:
                pass
        self._session_rows.clear()

        if not self._sessions:
            lbl = ctk.CTkLabel(
                self._list_frame, text="セッションがありません", anchor="w",
                text_color=_STATUS_COLOR,
            )
            lbl.grid(row=0, column=0, sticky="ew", padx=4, pady=4)
            self._session_rows["__empty__"] = lbl
            return

        for row_idx, session in enumerate(self._sessions):
            text = f"{_short_id(session.session_id)}  [{session.status}]"
            btn = ctk.CTkButton(
                self._list_frame, text=text, anchor="w",
                command=lambda sid=session.session_id: self._select_session(sid),
            )
            btn.grid(row=row_idx, column=0, sticky="ew", padx=4, pady=1)
            self._session_rows[session.session_id] = btn

    def _clear_detail(self) -> None:
        for widget in self._detail_widgets:
            try:
                widget.destroy()
            except Exception:
                pass
        self._detail_widgets.clear()

    def _detail_label(self, text: str, row: int, *, bold: bool = False,
                      muted: bool = False, indent: int = 8, pad_top: int = 0) -> None:
        ctk = self._ctk
        kwargs: dict = {"text": text, "anchor": "w"}
        kwargs["font"] = ("", 14, "bold") if bold else ("Courier", 11)
        if muted:
            kwargs["text_color"] = _STATUS_COLOR
        lbl = ctk.CTkLabel(self._detail_frame, **kwargs)
        lbl.grid(row=row, column=0, sticky="w", padx=indent, pady=(pad_top, 2 if bold else 0))
        self._detail_widgets.append(lbl)

    def _render_detail(self) -> None:
        self._clear_detail()

        if self._selected_id is None:
            self._current_detail = None
            self._detail_label("セッションを選択してください。", row=0, muted=True)
            return

        detail = build_session_detail(
            self._session_repo, self._name_map, self._selected_id
        )
        self._current_detail = detail
        row = 0

        # 概要
        self._detail_label("概要", row=row, bold=True, pad_top=8); row += 1
        for line in [
            f"session_id: {detail.session_id}",
            f"status: {detail.status}",
            f"started_at: {detail.started_at}",
            f"ended_at: {detail.ended_at or '—'}",
            f"blinds: {_format_blinds(detail.blinds)}",
        ]:
            self._detail_label(line, row=row, indent=12); row += 1

        # current seating
        self._detail_label("現在の seating", row=row, bold=True, pad_top=12); row += 1
        if not detail.current_seating:
            self._detail_label("まだ seat assignment がありません", row=row, muted=True, indent=12)
            row += 1
        else:
            for seat in detail.current_seating:
                self._detail_label(
                    f"席{seat.seat_no}: {seat.display_name}  ({seat.player_id})",
                    row=row, indent=12,
                ); row += 1

        # hand assignments
        self._detail_label("hand 別 seat assignments", row=row, bold=True, pad_top=12); row += 1
        if not detail.hand_assignments:
            self._detail_label("まだ seat assignment がありません", row=row, muted=True, indent=12)
            row += 1
        else:
            for a in detail.hand_assignments:
                self._detail_label(
                    f"hand#{a.hand_id} 席{a.seat_no}: {a.display_name}  ({a.player_id})",
                    row=row, indent=12,
                ); row += 1

    def _set_status(self, message: str) -> None:
        self._last_status = message
        self._status_label.configure(text=message, text_color=_STATUS_COLOR)

    # ――― 起動 ―――

    def run(self) -> None:
        self._root.mainloop()
