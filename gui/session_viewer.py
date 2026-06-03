"""gui/session_viewer.py

WS2-α: Session / Seating Viewer（desktop, **read-only**）。

S2 core（`core/session_repository.py`）に蓄積された session / hand-based seating の状態を
**人間が確認するための読み取り専用 GUI**。hand logger の dashboard (`gui/dashboard.py`) や
player registry (`gui/player_registry.py`) とは **別画面** として実装する。

read-only の原則:
  - 作成 / 編集 / 削除 / seat 割り当ての操作を一切持たない（refresh のみ許容）。
  - `SessionRepository` / `PlayerRepository` の **read API のみ** を呼ぶ。
  - 業務ルール（validation・seating 導出）は core が source of truth。本画面は表示に徹し、
    name 解決と表示整形だけを行う（CLAUDE.md § Session / Seating Viewer）。

レイアウト:
  ┌────────────────────────────────────────────────────────────┐
  │  Session / Seating Viewer（読み取り専用）          [再読込]   │
  ├──────────────────┬─────────────────────────────────────────┤
  │  session 一覧     │  選択中セッション概要                     │
  │  (左, 選択可)     ├─────────────────────────────────────────┤
  │                  │  現在の seating (current_seating)         │
  │                  ├─────────────────────────────────────────┤
  │                  │  hand ごとの seat assignments             │
  ├──────────────────┴─────────────────────────────────────────┤
  │  ステータス / メッセージ                                      │
  └────────────────────────────────────────────────────────────┘

player_id → display_name は `PlayerRepository` で解決し、解決できない場合は `(unknown)` を表示する
（player は削除できない S1 仕様だが、別ストア由来 / 将来の不整合に備えて安全側に倒す）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterable, Optional

from core.player_repository import PlayerNotFoundError, PlayerRepository
from core.session_repository import SessionNotFoundError, SessionRepository

if TYPE_CHECKING:
    from core.session import SeatAssignment, Session

logger = logging.getLogger(__name__)

_UNKNOWN_LABEL = "(unknown)"
_STATUS_OK_COLOR = "#4CAF50"
_STATUS_ERROR_COLOR = "#F44336"


# ――― 表示用 read model（widget 非依存・テスト可能） ―――

@dataclass
class _SessionListItem:
    """session 一覧 1 行の表示用要約。"""

    session_id: str
    started_at: str
    status: str
    label: Optional[str]
    hand_count: int
    assignment_count: int


@dataclass
class _SeatRow:
    """seat 1 行の表示用（player_id を display_name に解決済み）。"""

    seat_no: int
    player_id: str
    display_name: str
    status: Optional[str]


@dataclass
class _SessionDetail:
    """選択中 session の詳細 read model。"""

    session: "Session"
    current_seating: list[_SeatRow]
    hands: list[tuple[int, list[_SeatRow]]]  # (hand_id, seat rows) を hand_id 昇順で


class SessionViewerWindow:
    """session / seating を眺める read-only ビューア（独立画面）。

    使い方:
        player_repo = PlayerRepository()
        session_repo = SessionRepository(player_repo=player_repo)
        win = SessionViewerWindow(session_repo=session_repo, player_repo=player_repo)
        win.run()  # mainloop 開始（ブロッキング）

    既存アプリ内から開く場合は master を渡すと Toplevel として開く。
    name 解決を一貫させるため、`session_repo` と同じ `PlayerRepository` インスタンスを渡すこと。
    """

    def __init__(
        self,
        session_repo: SessionRepository,
        player_repo: PlayerRepository,
        master: Optional[object] = None,
    ) -> None:
        import customtkinter as ctk

        self._session_repo = session_repo
        self._player_repo = player_repo
        self._selected_id: Optional[str] = None
        self._session_rows: list[object] = []

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self._ctk = ctk
        self._owns_root = master is None
        if master is None:
            self._root = ctk.CTk()
        else:
            self._root = ctk.CTkToplevel(master)
        self._root.title("Session / Seating Viewer（読み取り専用）")
        self._root.geometry("900x620")
        self._root.resizable(True, True)

        self._build_ui()
        self._refresh_session_list()
        self._render_detail()

    # ――― UI 構築 ―――

    def _build_ui(self) -> None:
        ctk = self._ctk
        root = self._root
        root.grid_rowconfigure(1, weight=1)
        root.grid_columnconfigure(1, weight=1)

        # ヘッダー（read-only である旨を明示 + 再読込ボタン）
        header = ctk.CTkFrame(root, corner_radius=0)
        header.grid(row=0, column=0, columnspan=2, sticky="ew", padx=0, pady=0)
        header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            header,
            text="Session / Seating Viewer（読み取り専用 / inspection）",
            font=("", 16, "bold"),
            anchor="w",
        ).grid(row=0, column=0, sticky="w", padx=12, pady=8)
        ctk.CTkButton(header, text="再読込", width=90, command=self._cmd_refresh).grid(
            row=0, column=1, padx=12, pady=8
        )

        # 左ペイン: session 一覧
        self._list_frame = ctk.CTkScrollableFrame(root, width=280, label_text="セッション一覧")
        self._list_frame.grid(row=1, column=0, sticky="nsew", padx=(12, 4), pady=4)
        self._list_frame.grid_columnconfigure(0, weight=1)

        # 右ペイン: 詳細
        detail = ctk.CTkFrame(root, fg_color="transparent")
        detail.grid(row=1, column=1, sticky="nsew", padx=(4, 12), pady=4)
        detail.grid_columnconfigure(0, weight=1)
        detail.grid_rowconfigure(5, weight=1)

        ctk.CTkLabel(detail, text="選択中セッション概要", font=("", 12, "bold"),
                     anchor="w").grid(row=0, column=0, sticky="ew", padx=4, pady=(2, 0))
        self._summary_box = ctk.CTkTextbox(detail, height=130, wrap="none",
                                            font=("Courier", 12), state="disabled")
        self._summary_box.grid(row=1, column=0, sticky="ew", padx=4, pady=(0, 6))

        ctk.CTkLabel(detail, text="現在の seating（最新 hand から導出）",
                     font=("", 12, "bold"), anchor="w").grid(
            row=2, column=0, sticky="ew", padx=4, pady=(2, 0))
        self._seating_box = ctk.CTkTextbox(detail, height=120, wrap="none",
                                            font=("Courier", 12), state="disabled")
        self._seating_box.grid(row=3, column=0, sticky="ew", padx=4, pady=(0, 6))

        ctk.CTkLabel(detail, text="hand ごとの seat assignments",
                     font=("", 12, "bold"), anchor="w").grid(
            row=4, column=0, sticky="ew", padx=4, pady=(2, 0))
        self._hands_box = ctk.CTkTextbox(detail, wrap="none",
                                         font=("Courier", 12), state="disabled")
        self._hands_box.grid(row=5, column=0, sticky="nsew", padx=4, pady=(0, 4))

        # ステータス
        self._status_label = ctk.CTkLabel(root, text="", anchor="w")
        self._status_label.grid(row=2, column=0, columnspan=2, sticky="ew",
                                padx=12, pady=(0, 12))

    # ――― 表示用 read model（widget 非依存） ―――

    def _resolve_display_name(self, player_id: str) -> str:
        """player_id を display_name に解決する。未登録なら ``(unknown)``。"""
        try:
            return self._player_repo.get(player_id).display_name
        except PlayerNotFoundError:
            return _UNKNOWN_LABEL

    def _seat_rows(self, assignments: Iterable["SeatAssignment"]) -> list[_SeatRow]:
        return [
            _SeatRow(
                seat_no=sa.seat_no,
                player_id=sa.player_id,
                display_name=self._resolve_display_name(sa.player_id),
                status=sa.status,
            )
            for sa in assignments
        ]

    def _session_list_items(self) -> list[_SessionListItem]:
        """session 一覧の表示用要約を作成順で返す。"""
        items: list[_SessionListItem] = []
        for s in self._session_repo.list_sessions():
            hand_ids = self._session_repo.list_hand_ids(s.session_id)
            assignment_count = sum(
                len(self._session_repo.list_seat_assignments(s.session_id, h))
                for h in hand_ids
            )
            items.append(
                _SessionListItem(
                    session_id=s.session_id,
                    started_at=s.started_at,
                    status=s.status,
                    label=s.label,
                    hand_count=len(hand_ids),
                    assignment_count=assignment_count,
                )
            )
        return items

    def _build_detail(self, session_id: str) -> _SessionDetail:
        """選択中 session の詳細 read model を組み立てる。

        unknown session は `SessionNotFoundError`（呼び出し側で握る）。
        """
        session = self._session_repo.get_session(session_id)
        current = self._seat_rows(self._session_repo.current_seating(session_id))
        hands: list[tuple[int, list[_SeatRow]]] = []
        for hand_id in self._session_repo.list_hand_ids(session_id):
            rows = self._seat_rows(
                self._session_repo.list_seat_assignments(session_id, hand_id)
            )
            hands.append((hand_id, rows))
        return _SessionDetail(session=session, current_seating=current, hands=hands)

    # ――― 表示整形（純粋関数, テスト可能） ―――

    @staticmethod
    def _format_summary_text(detail: _SessionDetail) -> str:
        s = detail.session
        if s.blinds:
            blinds = f"SB {s.blinds.get('sb', '?')} / BB {s.blinds.get('bb', '?')}"
        else:
            blinds = "—"
        return "\n".join(
            [
                f"session_id : {s.session_id}",
                f"label      : {s.label or '—'}",
                f"status     : {s.status}",
                f"started_at : {s.started_at}",
                f"ended_at   : {s.ended_at or '—'}",
                f"blinds     : {blinds}",
                f"current seating : {'あり' if detail.current_seating else 'なし'}"
                f"（{len(detail.current_seating)} 席）",
                f"hands recorded  : {len(detail.hands)}",
            ]
        )

    @staticmethod
    def _format_seat_rows(rows: list[_SeatRow], indent: str = "") -> list[str]:
        out = [
            f"{indent}{'seat':>4}  {'player_id':<34} {'display_name':<18} status",
        ]
        for r in rows:
            out.append(
                f"{indent}{r.seat_no:>4}  {r.player_id:<34} "
                f"{r.display_name:<18} {r.status or 'active'}"
            )
        return out

    @classmethod
    def _format_seating_text(cls, rows: list[_SeatRow]) -> str:
        if not rows:
            return "（seating はまだありません）"
        return "\n".join(cls._format_seat_rows(rows))

    @classmethod
    def _format_hands_text(cls, hands: list[tuple[int, list[_SeatRow]]]) -> str:
        if not hands:
            return "（記録された hand はまだありません）"
        out: list[str] = []
        for hand_id, rows in hands:
            out.append(f"── hand #{hand_id} ──")
            if not rows:
                out.append("    （seat assignment なし）")
            else:
                out.extend(cls._format_seat_rows(rows, indent="  "))
            out.append("")
        return "\n".join(out).rstrip("\n")

    # ――― widget 更新 ―――

    def _set_status(self, message: str, error: bool = False) -> None:
        color = _STATUS_ERROR_COLOR if error else _STATUS_OK_COLOR
        self._status_label.configure(text=message, text_color=color)

    def _set_textbox(self, box: object, text: str) -> None:
        box.configure(state="normal")
        box.delete("1.0", "end")
        box.insert("end", text)
        box.configure(state="disabled")

    def _refresh_session_list(self) -> None:
        ctk = self._ctk
        for widget in self._session_rows:
            try:
                widget.destroy()
            except Exception:
                pass
        self._session_rows.clear()

        items = self._session_list_items()
        if not items:
            empty = ctk.CTkLabel(
                self._list_frame, text="（セッションがありません）",
                text_color="#AAAAAA", anchor="w",
            )
            empty.grid(row=0, column=0, sticky="ew", padx=4, pady=4)
            self._session_rows.append(empty)
            return

        for row_idx, item in enumerate(items):
            row = ctk.CTkFrame(self._list_frame, fg_color="transparent")
            row.grid(row=row_idx, column=0, sticky="ew", pady=1)
            row.grid_columnconfigure(0, weight=1)
            text = (
                f"{item.label or '(no label)'}  [{item.status}]\n"
                f"{item.session_id[:12]}…  {item.started_at}\n"
                f"hands: {item.hand_count} / assigns: {item.assignment_count}"
            )
            ctk.CTkLabel(row, text=text, anchor="w", justify="left").grid(
                row=0, column=0, sticky="w", padx=4
            )
            ctk.CTkButton(
                row, text="表示", width=56,
                command=lambda sid=item.session_id: self._select_session(sid),
            ).grid(row=0, column=1, padx=4)
            self._session_rows.append(row)

    def _select_session(self, session_id: str) -> None:
        self._selected_id = session_id
        self._render_detail()

    def _render_detail(self) -> None:
        """選択中 session の詳細を 3 つの textbox に反映する。未選択は empty state。"""
        if self._selected_id is None:
            self._set_textbox(self._summary_box, "← 左の一覧から session を選択してください。")
            self._set_textbox(self._seating_box, "")
            self._set_textbox(self._hands_box, "")
            return
        try:
            detail = self._build_detail(self._selected_id)
        except SessionNotFoundError:
            self._selected_id = None
            self._set_textbox(self._summary_box, "選択した session が見つかりません。")
            self._set_textbox(self._seating_box, "")
            self._set_textbox(self._hands_box, "")
            self._set_status("選択した session が見つかりません。", error=True)
            return
        self._set_textbox(self._summary_box, self._format_summary_text(detail))
        self._set_textbox(self._seating_box, self._format_seating_text(detail.current_seating))
        self._set_textbox(self._hands_box, self._format_hands_text(detail.hands))

    # ――― コマンド（read-only: refresh のみ） ―――

    def _cmd_refresh(self) -> None:
        """ディスクから repository を再読込し、一覧と詳細を更新する。"""
        self._session_repo.reload()
        self._player_repo.reload()
        self._refresh_session_list()
        if self._selected_id is not None:
            try:
                self._session_repo.get_session(self._selected_id)
            except SessionNotFoundError:
                self._selected_id = None
        self._render_detail()
        n = len(self._session_repo.list_sessions())
        self._set_status(f"再読込しました（{n} セッション）。")

    # ――― 起動 ―――

    def run(self) -> None:
        self._root.mainloop()
