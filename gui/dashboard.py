"""gui/dashboard.py

Phase 4: customtkinter ベースの GUI ダッシュボード。

レイアウト:
  ┌──────────────────────────────────────────────────────┐
  │  ヘッダー: セッション情報 / ハンド番号 / ストリート / ポット   │
  ├─────────────────┬────────────────────────────────────┤
  │  プレイヤー一覧   │  アクションログ                       │
  │  (左パネル)      │  (中央パネル, スクロール可)             │
  ├─────────────────┴────────────────────────────────────┤
  │  コントロール: 新ハンド / ウィナー確定 / リバイ             │
  └──────────────────────────────────────────────────────┘

スレッド安全設計:
  - IntegrationThread → on_action(record) → _update_queue.put(record)
  - mainloop 内で after(100, _poll_updates) を繰り返し呼び出して UI を更新
"""
from __future__ import annotations

import queue
import threading
import time
from datetime import datetime
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:
    from core.game_state import GameStateManager
    from core.hand_log import ActionRecord
    from output.json_writer import JsonWriter

# confidence スコアに応じた色
_CONF_COLOR_HIGH   = "#4CAF50"  # 緑  (>= 0.75)
_CONF_COLOR_MEDIUM = "#FF9800"  # 橙  (>= 0.5)
_CONF_COLOR_LOW    = "#F44336"  # 赤  (< 0.5)


def _conf_color(conf: float) -> str:
    if conf >= 0.75:
        return _CONF_COLOR_HIGH
    if conf >= 0.5:
        return _CONF_COLOR_MEDIUM
    return _CONF_COLOR_LOW


class GUIDashboard:
    """customtkinter GUI ダッシュボード。

    使い方:
        dash = GUIDashboard(
            game_state=gs,
            json_writer=writer,
            audio_queue=audio_q,
            camera_queue=camera_q,   # 省略可
            stop_event=stop_event,
        )
        dash.run()  # mainloop 開始（ブロッキング）
    """

    def __init__(
        self,
        game_state: "GameStateManager",
        json_writer: "JsonWriter",
        audio_queue: queue.Queue,
        camera_queue: Optional[queue.Queue] = None,
        stop_event: Optional[threading.Event] = None,
        rfid_receiver: Optional[object] = None,
    ) -> None:
        import customtkinter as ctk

        self._gs = game_state
        self._writer = json_writer
        self._audio_queue = audio_queue
        self._camera_queue = camera_queue
        self._stop_event = stop_event or threading.Event()
        self._rfid_receiver = rfid_receiver  # RFIDHTTPReceiver (status プロパティ用)
        self._update_queue: queue.Queue["ActionRecord"] = queue.Queue()
        self._rfid_card_queue: queue.Queue = queue.Queue()
        # seat → hole cards 表示用 (スレッド安全のため queue 経由で更新)
        self._hole_cards_display: dict[int, list[str]] = {}
        self._board_cards_display: list[str] = []

        # customtkinter の設定
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self._root = ctk.CTk()
        self._root.title("ポーカーハンドロガー")
        self._root.geometry("900x620")
        self._root.resizable(True, True)
        self._root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._ctk = ctk
        self._build_ui()

    # ――― UI 構築 ―――

    def _build_ui(self) -> None:
        ctk = self._ctk
        root = self._root
        root.grid_rowconfigure(1, weight=1)
        root.grid_columnconfigure(0, weight=1)

        # ヘッダー
        self._header = ctk.CTkFrame(root, height=50, corner_radius=0)
        self._header.grid(row=0, column=0, sticky="ew", padx=0, pady=0)
        self._header.grid_columnconfigure((0, 1, 2, 3), weight=1)

        self._lbl_session = ctk.CTkLabel(self._header, text="セッション: —", anchor="w")
        self._lbl_session.grid(row=0, column=0, padx=12, pady=8, sticky="w")

        self._lbl_hand = ctk.CTkLabel(self._header, text="ハンド: —", anchor="center")
        self._lbl_hand.grid(row=0, column=1, padx=12, pady=8)

        self._lbl_street = ctk.CTkLabel(self._header, text="ストリート: —", anchor="center")
        self._lbl_street.grid(row=0, column=2, padx=12, pady=8)

        self._lbl_pot = ctk.CTkLabel(self._header, text="ポット: 0", anchor="e")
        self._lbl_pot.grid(row=0, column=3, padx=12, pady=8, sticky="e")

        # RFID ステータスラベル (HTTP受信機が設定されている場合のみ有効)
        self._lbl_rfid = ctk.CTkLabel(self._header, text="RFID: —", anchor="e",
                                       font=("", 10))
        self._lbl_rfid.grid(row=1, column=3, padx=12, pady=0, sticky="e")

        # ボードカード表示ラベル
        self._lbl_board = ctk.CTkLabel(self._header, text="ボード: —", anchor="w",
                                        font=("Courier", 11))
        self._lbl_board.grid(row=1, column=0, columnspan=3, padx=12, pady=0, sticky="w")

        # メインエリア
        main_frame = ctk.CTkFrame(root, corner_radius=0, fg_color="transparent")
        main_frame.grid(row=1, column=0, sticky="nsew", padx=8, pady=4)
        main_frame.grid_rowconfigure(0, weight=1)
        main_frame.grid_columnconfigure(0, weight=0)
        main_frame.grid_columnconfigure(1, weight=1)

        # 左パネル: プレイヤー一覧
        self._left = ctk.CTkScrollableFrame(main_frame, width=220, label_text="プレイヤー")
        self._left.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        self._player_rows: dict[int, dict] = {}  # seat → {frame, labels}
        self._build_player_table()

        # 中央パネル: アクションログ
        self._log_box = ctk.CTkTextbox(main_frame, state="disabled", wrap="none",
                                        font=("Courier", 12))
        self._log_box.grid(row=0, column=1, sticky="nsew")
        # タグ設定（色用）
        self._log_box.tag_config("high",   foreground=_CONF_COLOR_HIGH)
        self._log_box.tag_config("medium", foreground=_CONF_COLOR_MEDIUM)
        self._log_box.tag_config("low",    foreground=_CONF_COLOR_LOW)
        self._log_box.tag_config("review", foreground="#FF5252", font=("Courier", 12, "bold"))

        # 下部コントロール
        ctrl = ctk.CTkFrame(root, height=80, corner_radius=0)
        ctrl.grid(row=2, column=0, sticky="ew", padx=0, pady=0)
        self._build_controls(ctrl)

        # セッション名を表示
        self._lbl_session.configure(text=f"セッション: {self._writer._session_id}")

    def _build_player_table(self) -> None:
        ctk = self._ctk
        frame = self._left
        # ヘッダー行
        for col, text in enumerate(["席", "名前", "スタック", "状態", "ホールカード"]):
            lbl = ctk.CTkLabel(frame, text=text, font=("", 11, "bold"))
            lbl.grid(row=0, column=col, padx=4, pady=2, sticky="w")

        stacks = self._gs.get_stacks()
        active = set(self._gs.get_active_seats())
        for row_idx, seat in enumerate(sorted(stacks.keys()), start=1):
            name = self._gs.get_player_name(seat)
            stack = stacks[seat]
            status = "○" if seat in active else "×"
            color = "#CCCCCC" if seat in active else "#888888"

            seat_lbl   = ctk.CTkLabel(frame, text=str(seat), text_color=color)
            name_lbl   = ctk.CTkLabel(frame, text=name,      text_color=color, anchor="w")
            stack_lbl  = ctk.CTkLabel(frame, text=f"{stack:,}", text_color=color, anchor="e")
            status_lbl = ctk.CTkLabel(frame, text=status,    text_color=color)
            hole_lbl   = ctk.CTkLabel(frame, text="—",        text_color="#AAAAAA",
                                       font=("Courier", 10))

            seat_lbl.grid(row=row_idx,  column=0, padx=4, pady=1, sticky="w")
            name_lbl.grid(row=row_idx,  column=1, padx=4, pady=1, sticky="w")
            stack_lbl.grid(row=row_idx, column=2, padx=4, pady=1, sticky="e")
            status_lbl.grid(row=row_idx,column=3, padx=4, pady=1)
            hole_lbl.grid(row=row_idx,  column=4, padx=4, pady=1, sticky="w")

            self._player_rows[seat] = {
                "stack_lbl":  stack_lbl,
                "status_lbl": status_lbl,
                "name_lbl":   name_lbl,
                "hole_lbl":   hole_lbl,
            }

    def _build_controls(self, ctrl: object) -> None:
        ctk = self._ctk
        ctrl.grid_columnconfigure((0, 1, 2, 3, 4, 5, 6), weight=1)

        # 新ハンドボタン
        ctk.CTkButton(ctrl, text="新ハンド", width=100,
                      command=self._cmd_new_hand).grid(row=0, column=0, padx=8, pady=12)

        # ウィナー確定
        ctk.CTkLabel(ctrl, text="ウィナー:").grid(row=0, column=1, padx=(12, 2))
        seats = [str(s) for s in sorted(self._gs.get_stacks().keys())]
        self._winner_var = ctk.StringVar(value=seats[0] if seats else "1")
        ctk.CTkOptionMenu(ctrl, variable=self._winner_var, values=seats,
                          width=70).grid(row=0, column=2, padx=2)
        ctk.CTkButton(ctrl, text="確定", width=70,
                      command=self._cmd_winner).grid(row=0, column=3, padx=(2, 12))

        # リバイ
        ctk.CTkLabel(ctrl, text="リバイ 席:").grid(row=0, column=4, padx=(12, 2))
        self._rebuy_seat_var = ctk.StringVar(value=seats[0] if seats else "1")
        ctk.CTkOptionMenu(ctrl, variable=self._rebuy_seat_var, values=seats,
                          width=70).grid(row=0, column=5, padx=2)
        self._rebuy_amount_entry = ctk.CTkEntry(ctrl, width=90, placeholder_text="金額")
        self._rebuy_amount_entry.grid(row=0, column=6, padx=2)
        ctk.CTkButton(ctrl, text="適用", width=70,
                      command=self._cmd_rebuy).grid(row=0, column=7, padx=(2, 12))

    # ――― コントロールコマンド ―――

    def _cmd_new_hand(self) -> None:
        from core.events import AudioEvent
        # ホールカード / ボードカード表示をリセット
        self._hole_cards_display.clear()
        self._board_cards_display.clear()
        for row in self._player_rows.values():
            if "hole_lbl" in row:
                row["hole_lbl"].configure(text="—")
        self._lbl_board.configure(text="ボード: —")
        self._audio_queue.put(AudioEvent(
            action="new_hand", amount=0, timestamp=time.time(), raw_text="",
        ))

    def _cmd_winner(self) -> None:
        from core.events import AudioEvent
        try:
            seat = int(self._winner_var.get())
        except ValueError:
            return
        self._audio_queue.put(AudioEvent(
            action="winner", amount=0, timestamp=time.time(),
            raw_text=f"シート{seat} ウィナー",
        ))

    def _cmd_rebuy(self) -> None:
        try:
            seat = int(self._rebuy_seat_var.get())
            amount = int(self._rebuy_amount_entry.get().strip())
        except ValueError:
            self._append_log("⚠ リバイ入力が不正です。", tag="review")
            return
        try:
            self._gs.rebuy(seat, amount)
            self._refresh_player_row(seat)
            self._append_log(f"リバイ: 席{seat} +{amount:,}", tag="medium")
        except Exception as e:
            self._append_log(f"⚠ リバイ失敗: {e}", tag="review")

    # ――― UI 更新（メインスレッド側） ―――

    def _poll_updates(self) -> None:
        """100ms ごとに _update_queue / _rfid_card_queue を消費して UI を更新する。"""
        try:
            while True:
                record = self._update_queue.get_nowait()
                self._apply_record(record)
        except queue.Empty:
            pass
        # RFID カードイベントを処理
        try:
            while True:
                rfid_ev = self._rfid_card_queue.get_nowait()
                self._apply_rfid_card(rfid_ev)
        except queue.Empty:
            pass
        # ゲーム状態のヘッダーを常に最新化
        self._refresh_header()
        if not self._stop_event.is_set():
            self._root.after(100, self._poll_updates)

    def _apply_record(self, record: "ActionRecord") -> None:
        """ActionRecord を UI に反映する。"""
        self._refresh_player_row(record.seat)
        self._refresh_header()

        # ログ行を組み立て
        conf = record.confidence
        tag = "high" if conf >= 0.75 else ("medium" if conf >= 0.5 else "low")
        src_flags = []
        if record.source.get("rfid"):
            src_flags.append("RFID")
        if record.source.get("audio"):
            src_flags.append("音声")
        if record.source.get("camera"):
            src_flags.append("カメラ")
        src_str = "+".join(src_flags) if src_flags else "-"

        line = (
            f"[{record.street:<8}] 席{record.seat} {record.player_name:<8} "
            f"{record.action:<6} {record.amount:>6,}  "
            f"pot={record.pot_after:>7,}  conf={conf:.2f} ({src_str})"
        )
        if record.needs_review:
            line += "  ⚠要確認"
            self._append_log(line, tag="review")
        else:
            self._append_log(line, tag=tag)

    def _apply_rfid_card(self, rfid_ev: object) -> None:
        """RFIDEvent を UI に反映する (ホールカード / ボードカード更新)。"""
        if rfid_ev.role == "seat" and rfid_ev.seat is not None and rfid_ev.card:
            cards = self._hole_cards_display.setdefault(rfid_ev.seat, [])
            if rfid_ev.card not in cards and len(cards) < 2:
                cards.append(rfid_ev.card)
            if rfid_ev.seat in self._player_rows:
                hole_text = " ".join(cards) if cards else "—"
                self._player_rows[rfid_ev.seat]["hole_lbl"].configure(text=hole_text)
        elif rfid_ev.role == "board" and rfid_ev.card:
            if rfid_ev.card not in self._board_cards_display:
                self._board_cards_display.append(rfid_ev.card)
            board_text = "ボード: " + " ".join(self._board_cards_display)
            self._lbl_board.configure(text=board_text)

    def _refresh_header(self) -> None:
        gs = self._gs
        self._lbl_hand.configure(text=f"ハンド: #{gs.hand_id}")
        self._lbl_street.configure(text=f"ストリート: {gs.street}")
        self._lbl_pot.configure(text=f"ポット: {gs.pot:,}")
        # RFID HTTP 受信機のステータスを表示
        if self._rfid_receiver is not None:
            try:
                st = self._rfid_receiver.status
                count = st.get("events_received", 0)
                port = st.get("bind_port", "")
                self._lbl_rfid.configure(text=f"RFID:{port} ({count}件)")
            except Exception:
                pass

    def _refresh_player_row(self, seat: int) -> None:
        if seat not in self._player_rows:
            return
        row = self._player_rows[seat]
        stack = self._gs.get_stack(seat)
        active = seat in self._gs.get_active_seats()
        color = "#CCCCCC" if active else "#888888"
        row["stack_lbl"].configure(text=f"{stack:,}", text_color=color)
        row["status_lbl"].configure(text="○" if active else "×", text_color=color)
        row["name_lbl"].configure(text_color=color)

    def _append_log(self, text: str, tag: str = "") -> None:
        box = self._log_box
        box.configure(state="normal")
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"{ts}  {text}\n"
        box.insert("end", line, tag)
        box.configure(state="disabled")
        box.see("end")

    # ――― ライフサイクル ―――

    def start_threads(
        self,
        audio_thread: threading.Thread,
        integration_thread: threading.Thread,
        camera_thread: Optional[threading.Thread] = None,
        rfid_thread: Optional[threading.Thread] = None,
    ) -> None:
        """外部で生成したスレッドを受け取って起動する。"""
        self._audio_thread = audio_thread
        self._integration_thread = integration_thread
        self._camera_thread = camera_thread
        self._rfid_thread = rfid_thread

        if camera_thread is not None:
            camera_thread.start()
        if rfid_thread is not None:
            rfid_thread.start()
        audio_thread.start()
        integration_thread.start()

    def on_action(self, record: "ActionRecord") -> None:
        """IntegrationThread から呼ばれるコールバック。スレッド安全。"""
        self._update_queue.put(record)

    def on_rfid_card(self, rfid_ev: object) -> None:
        """IntegrationThread から呼ばれる RFID カードコールバック。スレッド安全。"""
        self._rfid_card_queue.put(rfid_ev)

    def run(self) -> None:
        """mainloop を開始する（ブロッキング）。"""
        self._append_log("セッション開始。ディーラーのアナウンスを待っています...", tag="medium")
        self._refresh_header()
        self._root.after(100, self._poll_updates)
        self._root.mainloop()

    def _on_close(self) -> None:
        self._stop_event.set()
        self._root.destroy()


# ─────────────────────────────────────────────────────────────────────────────
# DashboardWindow / SeatPanel — 3-pane new dashboard (Phase 4+)
# Loaded lazily via module __getattr__ so that importing _conf_color etc.
# at test-module level does NOT trigger a top-level `import customtkinter`.
# ─────────────────────────────────────────────────────────────────────────────

_LOG_MAX_LINES = 10

_SEAT_ACTIVE   = "#2D5A3D"
_SEAT_INACTIVE = "#3A3A3A"
_SEAT_TURN     = "#1E6B4A"
_DEALER_BADGE  = "#FFD700"


def __getattr__(name: str):
    """Lazily define DashboardWindow and SeatPanel to avoid top-level ctk import."""
    if name in ("DashboardWindow", "SeatPanel"):
        _load_new_gui_classes()
        val = globals().get(name)
        if val is not None:
            return val
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _load_new_gui_classes() -> None:
    """Define SeatPanel and DashboardWindow; inject them into module globals."""
    try:
        import math
        import customtkinter as _ctk
        import tkinter as _tk
    except ImportError:
        # In headless / test environments where tkinter is unavailable, provide
        # lightweight stubs so that `from gui.dashboard import DashboardWindow`
        # still succeeds (the classes cannot render, but they can be imported).
        class SeatPanel:  # type: ignore[no-redef]
            """Stub SeatPanel (tkinter unavailable)."""
            W, H = 92, 78
            def __init__(self, master=None, seat: int = 0, **kwargs):
                self.seat = seat
            def update(self, **kwargs) -> None:
                pass

        class DashboardWindow:  # type: ignore[no-redef]
            """Stub DashboardWindow (tkinter unavailable)."""
            def __init__(self, update_queue=None, audio_queue=None,
                         game_state=None, json_writer=None,
                         stop_event=None) -> None:
                pass
            def run(self) -> None:
                pass
            def on_action(self, record: object) -> None:
                pass
            def on_rfid_card(self, rfid_ev: object) -> None:
                pass

        globals()["SeatPanel"] = SeatPanel
        globals()["DashboardWindow"] = DashboardWindow
        return

    import math

    class SeatPanel(_ctk.CTkFrame):
        """92×78 px frame widget representing one seat at the poker table."""

        W, H = 92, 78

        def __init__(self, master, seat: int, **kwargs):
            super().__init__(master, width=self.W, height=self.H,
                             corner_radius=6, **kwargs)
            self.seat = seat
            self._build()

        def _build(self) -> None:
            self.place_propagate(False)

            self._lbl_pos = _ctk.CTkLabel(self, text="—", font=("", 9),
                                           width=30, anchor="w")
            self._lbl_pos.place(x=4, y=2)

            self._lbl_dealer = _ctk.CTkLabel(
                self, text="D", font=("", 9, "bold"),
                width=14, height=14,
                fg_color=_DEALER_BADGE, text_color="#000000",
                corner_radius=7,
            )
            self._lbl_dealer.place(x=72, y=2)
            self._lbl_dealer.place_forget()

            self._lbl_name = _ctk.CTkLabel(
                self, text=f"席{self.seat}", font=("", 10, "bold"), anchor="center"
            )
            self._lbl_name.place(x=4, y=18, width=84)

            self._lbl_stack = _ctk.CTkLabel(self, text="—", font=("", 9),
                                             anchor="center")
            self._lbl_stack.place(x=4, y=34, width=84)

            self._lbl_action = _ctk.CTkLabel(self, text="", font=("", 9),
                                              anchor="center",
                                              text_color="#AAAAAA")
            self._lbl_action.place(x=4, y=50, width=84)

            self._lbl_holes = _ctk.CTkLabel(self, text="",
                                             font=("Courier", 9),
                                             anchor="center",
                                             text_color="#FFCC66")
            self._lbl_holes.place(x=4, y=62, width=84)

        def update(
            self,
            *,
            name: str = "",
            stack: int = 0,
            position: str = "",
            last_action: str = "",
            hole_cards: "list[str] | None" = None,
            is_dealer: bool = False,
            is_active: bool = True,
            is_turn: bool = False,
        ) -> None:
            if is_turn:
                bg = _SEAT_TURN
            elif is_active:
                bg = _SEAT_ACTIVE
            else:
                bg = _SEAT_INACTIVE
            self.configure(fg_color=bg)

            self._lbl_pos.configure(text=position or "—")
            self._lbl_name.configure(text=name or f"席{self.seat}")
            self._lbl_stack.configure(text=f"{stack:,}" if stack else "—")
            self._lbl_action.configure(text=last_action)
            self._lbl_holes.configure(
                text=" ".join(hole_cards) if hole_cards else ""
            )
            if is_dealer:
                self._lbl_dealer.place(x=72, y=2)
            else:
                self._lbl_dealer.place_forget()

    # ──────────────────────────────────────────────────────────────────────

    class DashboardWindow(_ctk.CTk):
        """3-pane poker dashboard: left=table view, center=game info, right=log.

        Args:
            update_queue: Queue that receives dicts with a ``"type"`` key.
            audio_queue:  Queue for audio events.
            game_state:   GameState or GameStateManager instance.
            json_writer:  JsonWriter instance.
            stop_event:   threading.Event used to signal shutdown.
        """

        def __init__(
            self,
            update_queue: queue.Queue,
            audio_queue: queue.Queue,
            game_state: object,
            json_writer: object,
            stop_event: "Optional[threading.Event]" = None,
        ) -> None:
            super().__init__()
            self._update_queue = update_queue
            self._audio_queue = audio_queue
            self._gs = game_state
            self._writer = json_writer
            self._stop_event = stop_event or threading.Event()

            _ctk.set_appearance_mode("dark")
            _ctk.set_default_color_theme("blue")

            self.title("ポーカーハンドロガー")
            self.geometry("1100x680")
            self.resizable(True, True)
            self.protocol("WM_DELETE_WINDOW", self._on_close)

            self._seat_panels: "dict[int, SeatPanel]" = {}
            self._canvas = None
            self._use_canvas = False
            self._log_lines: "list[str]" = []

            self._build_ui()
            self.after(100, self._poll_queue)

        # ── UI construction ──────────────────────────────────────────────

        def _build_ui(self) -> None:
            self.grid_rowconfigure(1, weight=1)
            self.grid_columnconfigure(0, weight=1)

            self._build_header()

            content = _ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
            content.grid(row=1, column=0, sticky="nsew", padx=6, pady=4)
            content.grid_rowconfigure(0, weight=1)
            content.grid_columnconfigure(0, weight=0)
            content.grid_columnconfigure(1, weight=1)
            content.grid_columnconfigure(2, weight=0)

            self._build_left_pane(content)
            self._build_center_pane(content)
            self._build_right_pane(content)

        def _build_header(self) -> None:
            hdr = _ctk.CTkFrame(self, height=48, corner_radius=0)
            hdr.grid(row=0, column=0, sticky="ew")
            hdr.grid_columnconfigure((0, 1, 2, 3), weight=1)

            self._lbl_session = _ctk.CTkLabel(hdr, text="セッション: —", anchor="w")
            self._lbl_session.grid(row=0, column=0, padx=12, pady=8, sticky="w")

            self._lbl_hand = _ctk.CTkLabel(hdr, text="ハンド: —", anchor="center")
            self._lbl_hand.grid(row=0, column=1, padx=12, pady=8)

            self._lbl_street = _ctk.CTkLabel(hdr, text="ストリート: —", anchor="center")
            self._lbl_street.grid(row=0, column=2, padx=12, pady=8)

            self._lbl_pot = _ctk.CTkLabel(hdr, text="ポット: 0", anchor="e")
            self._lbl_pot.grid(row=0, column=3, padx=12, pady=8, sticky="e")

        def _build_left_pane(self, parent) -> None:
            frame = _ctk.CTkFrame(parent, width=420, corner_radius=6)
            frame.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
            frame.grid_rowconfigure(1, weight=1)
            frame.grid_columnconfigure(0, weight=1)

            _ctk.CTkLabel(frame, text="テーブルビュー",
                          font=("", 12, "bold")).grid(row=0, column=0, pady=(6, 2))

            canvas_host = _ctk.CTkFrame(frame, fg_color="transparent")
            canvas_host.grid(row=1, column=0, sticky="nsew", padx=4, pady=4)

            try:
                self._canvas = _tk.Canvas(
                    canvas_host, width=400, height=320,
                    bg="#1A1A2E", highlightthickness=0,
                )
                self._canvas.pack(fill="both", expand=True)
                self._use_canvas = True
            except Exception:
                self._canvas = None
                self._use_canvas = False

            stacks = self._gs.get_stacks()
            all_seats = sorted(stacks.keys())
            n = len(all_seats)
            cx, cy, rx, ry = 200, 160, 160, 120

            for i, seat in enumerate(all_seats):
                angle = 2 * math.pi * i / max(n, 1) - math.pi / 2
                px = int(cx + rx * math.cos(angle))
                py = int(cy + ry * math.sin(angle))

                if self._use_canvas:
                    panel = SeatPanel(self._canvas, seat=seat)
                    self._canvas.create_window(px, py, window=panel, anchor="center")
                else:
                    panel = SeatPanel(canvas_host, seat=seat)
                    panel.grid(row=i // 3, column=i % 3, padx=4, pady=4)

                self._seat_panels[seat] = panel

        def _build_center_pane(self, parent) -> None:
            frame = _ctk.CTkFrame(parent, corner_radius=6)
            frame.grid(row=0, column=1, sticky="nsew", padx=4)
            frame.grid_columnconfigure(0, weight=1)

            _ctk.CTkLabel(frame, text="ゲーム情報",
                          font=("", 12, "bold")).grid(row=0, column=0, pady=(6, 2))

            self._lbl_c_street = _ctk.CTkLabel(frame, text="ストリート: —", font=("", 11))
            self._lbl_c_street.grid(row=1, column=0, pady=2, sticky="w", padx=12)

            self._lbl_c_pot = _ctk.CTkLabel(frame, text="ポット: 0", font=("", 11))
            self._lbl_c_pot.grid(row=2, column=0, pady=2, sticky="w", padx=12)

            board_f = _ctk.CTkFrame(frame, fg_color="transparent")
            board_f.grid(row=3, column=0, pady=8, padx=12, sticky="w")
            _ctk.CTkLabel(board_f, text="ボード:", font=("", 10)).grid(
                row=0, column=0, padx=(0, 4)
            )
            self._board_lbls: list = []
            for i in range(5):
                lbl = _ctk.CTkLabel(
                    board_f, text="[  ]",
                    font=("Courier", 13, "bold"),
                    text_color="#CCCCCC", width=36,
                )
                lbl.grid(row=0, column=i + 1, padx=2)
                self._board_lbls.append(lbl)

            self._lbl_rfid_summary = _ctk.CTkLabel(
                frame, text="RFID: —", font=("", 10), text_color="#888888"
            )
            self._lbl_rfid_summary.grid(row=4, column=0, pady=4, sticky="w", padx=12)

        def _build_right_pane(self, parent) -> None:
            frame = _ctk.CTkFrame(parent, width=240, corner_radius=6)
            frame.grid(row=0, column=2, sticky="nsew", padx=(4, 0))
            frame.grid_rowconfigure(1, weight=1)
            frame.grid_columnconfigure(0, weight=1)

            _ctk.CTkLabel(frame, text="ログ",
                          font=("", 12, "bold")).grid(row=0, column=0, pady=(6, 2))

            self._log_box = _ctk.CTkTextbox(
                frame, state="disabled", wrap="word", font=("Courier", 11)
            )
            self._log_box.grid(row=1, column=0, sticky="nsew", padx=4, pady=(0, 4))

            btn_frame = _ctk.CTkFrame(frame, fg_color="transparent")
            btn_frame.grid(row=2, column=0, sticky="ew", padx=4, pady=4)
            btn_frame.grid_columnconfigure(0, weight=1)

            for row_i, (label, cmd) in enumerate([
                ("ハンド強制終了",       self._cmd_force_end_hand),
                ("アクション手動入力",   self._cmd_manual_action),
                ("スタック修正",         self._cmd_fix_stack),
                ("ターン手動補正",       self._cmd_fix_turn),
                ("ディーラーボタン移動", self._cmd_move_dealer),
            ]):
                _ctk.CTkButton(btn_frame, text=label, height=28,
                               command=cmd).grid(row=row_i, column=0,
                                                 pady=2, sticky="ew")

        # ── Queue polling ────────────────────────────────────────────────

        def _poll_queue(self) -> None:
            """100ms ごとに update_queue を消費して UI を更新する。"""
            try:
                while True:
                    item = self._update_queue.get_nowait()
                    self._dispatch_item(item)
            except queue.Empty:
                pass

            self._refresh_header()
            if not self._stop_event.is_set():
                self.after(100, self._poll_queue)

        def _dispatch_item(self, item: dict) -> None:
            t = item.get("type", "")
            if t == "state":
                self._apply_state(item)
            elif t == "action":
                self._apply_action_item(item)
            elif t == "rfid_card":
                self._apply_rfid_card_item(item)
            elif t == "board_card":
                self._apply_board_card_item(item)

        def _apply_state(self, item: dict) -> None:
            gs = item.get("game_state", self._gs)
            stacks = gs.get_stacks()
            active = set(gs.get_active_seats())
            position_map = getattr(gs, "position_map", {})
            button_seat = getattr(gs, "button_seat", None)
            try:
                turn_seat = (
                    gs.current_turn_seat()
                    if hasattr(gs, "current_turn_seat")
                    else None
                )
            except Exception:
                turn_seat = None

            for seat, panel in self._seat_panels.items():
                name = (
                    gs.get_player_name(seat)
                    if hasattr(gs, "get_player_name")
                    else f"席{seat}"
                )
                panel.update(
                    name=name,
                    stack=stacks.get(seat, 0),
                    position=position_map.get(seat, ""),
                    is_active=(seat in active),
                    is_turn=(seat == turn_seat),
                    is_dealer=(seat == button_seat),
                )
            self._lbl_c_street.configure(text=f"ストリート: {gs.street}")
            self._lbl_c_pot.configure(text=f"ポット: {gs.pot:,}")

        def _apply_action_item(self, item: dict) -> None:
            seat = item.get("seat")
            action = item.get("action", "")
            if seat is not None and seat in self._seat_panels:
                self._seat_panels[seat]._lbl_action.configure(text=action)
            self._append_log(f"席{seat} {action} {item.get('amount', 0):,}")

        def _apply_rfid_card_item(self, item: dict) -> None:
            seat = item.get("seat")
            card = item.get("card", "")
            role = item.get("role", "seat")
            if role == "seat" and seat is not None and seat in self._seat_panels:
                panel = self._seat_panels[seat]
                current = panel._lbl_holes.cget("text")
                cards = current.split() if current else []
                if card and card not in cards and len(cards) < 2:
                    cards.append(card)
                panel._lbl_holes.configure(text=" ".join(cards))
                self._lbl_rfid_summary.configure(
                    text=f"RFID: 席{seat} {' '.join(cards)}"
                )
            elif role == "board":
                self._apply_board_card_item({"card": card})

        def _apply_board_card_item(self, item: dict) -> None:
            card = item.get("card", "")
            for lbl in self._board_lbls:
                if lbl.cget("text") == "[  ]":
                    lbl.configure(text=card, text_color="#FFFFFF")
                    break

        # ── Header refresh ───────────────────────────────────────────────

        def _refresh_header(self) -> None:
            gs = self._gs
            self._lbl_hand.configure(text=f"ハンド: #{gs.hand_id}")
            self._lbl_street.configure(text=f"ストリート: {gs.street}")
            self._lbl_pot.configure(text=f"ポット: {gs.pot:,}")

        # ── Manual operation dialogs ─────────────────────────────────────

        def _cmd_force_end_hand(self) -> None:
            dlg = _ctk.CTkToplevel(self)
            dlg.title("ハンド強制終了")
            dlg.geometry("320x140")
            dlg.grab_set()
            _ctk.CTkLabel(dlg, text="ウィナー席番号:").pack(pady=(16, 4))
            seats = [str(s) for s in sorted(self._gs.get_stacks().keys())]
            var = _ctk.StringVar(value=seats[0] if seats else "1")
            _ctk.CTkOptionMenu(dlg, variable=var, values=seats).pack()

            def _apply():
                try:
                    self._gs.end_hand(int(var.get()))
                    self._append_log(f"ハンド強制終了: 席{var.get()} ウィン")
                except Exception as e:
                    self._append_log(f"⚠ 強制終了失敗: {e}")
                dlg.destroy()

            _ctk.CTkButton(dlg, text="確定", command=_apply).pack(pady=8)

        def _cmd_manual_action(self) -> None:
            dlg = _ctk.CTkToplevel(self)
            dlg.title("アクション手動入力")
            dlg.geometry("340x220")
            dlg.grab_set()
            _ctk.CTkLabel(dlg, text="席番号:").pack(pady=(12, 2))
            seats = [str(s) for s in sorted(self._gs.get_stacks().keys())]
            seat_var = _ctk.StringVar(value=seats[0] if seats else "1")
            _ctk.CTkOptionMenu(dlg, variable=seat_var, values=seats).pack()
            _ctk.CTkLabel(dlg, text="アクション:").pack(pady=(8, 2))
            action_var = _ctk.StringVar(value="check")
            _ctk.CTkOptionMenu(
                dlg, variable=action_var,
                values=["bet", "raise", "call", "fold", "check", "allin"],
            ).pack()
            _ctk.CTkLabel(dlg, text="金額 (省略可):").pack(pady=(8, 2))
            amount_entry = _ctk.CTkEntry(dlg, placeholder_text="0")
            amount_entry.pack()

            def _apply():
                try:
                    seat = int(seat_var.get())
                    amt_str = amount_entry.get().strip()
                    amt = int(amt_str) if amt_str else 0
                    self._gs.apply_action(seat, action_var.get(), amt)
                    self._append_log(
                        f"手動入力: 席{seat} {action_var.get()} {amt:,}"
                    )
                except Exception as e:
                    self._append_log(f"⚠ 手動入力失敗: {e}")
                dlg.destroy()

            _ctk.CTkButton(dlg, text="適用", command=_apply).pack(pady=8)

        def _cmd_fix_stack(self) -> None:
            dlg = _ctk.CTkToplevel(self)
            dlg.title("スタック修正")
            dlg.geometry("320x180")
            dlg.grab_set()
            _ctk.CTkLabel(dlg, text="席番号:").pack(pady=(12, 2))
            seats = [str(s) for s in sorted(self._gs.get_stacks().keys())]
            seat_var = _ctk.StringVar(value=seats[0] if seats else "1")
            _ctk.CTkOptionMenu(dlg, variable=seat_var, values=seats).pack()
            _ctk.CTkLabel(dlg, text="新スタック値:").pack(pady=(8, 2))
            stack_entry = _ctk.CTkEntry(dlg, placeholder_text="例: 10000")
            stack_entry.pack()

            def _apply():
                try:
                    seat = int(seat_var.get())
                    new_stack = int(stack_entry.get().strip())
                    self._gs.update_stack(seat, new_stack)
                    self._append_log(f"スタック修正: 席{seat} → {new_stack:,}")
                    if seat in self._seat_panels:
                        self._seat_panels[seat]._lbl_stack.configure(
                            text=f"{new_stack:,}"
                        )
                except Exception as e:
                    self._append_log(f"⚠ スタック修正失敗: {e}")
                dlg.destroy()

            _ctk.CTkButton(dlg, text="適用", command=_apply).pack(pady=8)

        def _cmd_fix_turn(self) -> None:
            dlg = _ctk.CTkToplevel(self)
            dlg.title("ターン手動補正")
            dlg.geometry("320x140")
            dlg.grab_set()
            _ctk.CTkLabel(dlg, text="手番にする席番号:").pack(pady=(16, 4))
            seats = [str(s) for s in sorted(self._gs.get_stacks().keys())]
            var = _ctk.StringVar(value=seats[0] if seats else "1")
            _ctk.CTkOptionMenu(dlg, variable=var, values=seats).pack()

            def _apply():
                try:
                    seat = int(var.get())
                    gs = self._gs
                    if hasattr(gs, "turn_order") and seat in gs.turn_order:
                        gs.current_turn_idx = gs.turn_order.index(seat)
                        self._append_log(f"ターン補正: 席{seat}")
                    elif hasattr(gs, "_active_seats") and seat in gs._active_seats:
                        gs._turn_idx = gs._active_seats.index(seat)
                        self._append_log(f"ターン補正: 席{seat}")
                    else:
                        self._append_log(f"⚠ 席{seat} はターン順にありません")
                except Exception as e:
                    self._append_log(f"⚠ ターン補正失敗: {e}")
                dlg.destroy()

            _ctk.CTkButton(dlg, text="適用", command=_apply).pack(pady=8)

        def _cmd_move_dealer(self) -> None:
            dlg = _ctk.CTkToplevel(self)
            dlg.title("ディーラーボタン手動移動")
            dlg.geometry("320x140")
            dlg.grab_set()
            _ctk.CTkLabel(dlg, text="ディーラー席番号:").pack(pady=(16, 4))
            seats = [str(s) for s in sorted(self._gs.get_stacks().keys())]
            var = _ctk.StringVar(value=seats[0] if seats else "1")
            _ctk.CTkOptionMenu(dlg, variable=var, values=seats).pack()

            def _apply():
                try:
                    seat = int(var.get())
                    gs = self._gs
                    if hasattr(gs, "button_seat"):
                        gs.button_seat = seat
                        self._append_log(f"ディーラーボタン移動: 席{seat}")
                    else:
                        self._append_log("⚠ このゲーム状態はディーラーボタン管理非対応")
                except Exception as e:
                    self._append_log(f"⚠ ディーラー移動失敗: {e}")
                dlg.destroy()

            _ctk.CTkButton(dlg, text="適用", command=_apply).pack(pady=8)

        # ── Callbacks for IntegrationThread ─────────────────────────────

        def on_action(self, record: object) -> None:
            """IntegrationThread から呼ばれるコールバック。スレッド安全。"""
            self._update_queue.put({
                "type":   "action",
                "record": record,
                "seat":   getattr(record, "seat", None),
                "action": getattr(record, "action", ""),
                "amount": getattr(record, "amount", 0),
            })

        def on_rfid_card(self, rfid_ev: object) -> None:
            """RFID カードコールバック。スレッド安全。"""
            self._update_queue.put({
                "type": "rfid_card",
                "seat": getattr(rfid_ev, "seat", None),
                "card": getattr(rfid_ev, "card", ""),
                "role": getattr(rfid_ev, "role", "seat"),
            })

        # ── Logging ─────────────────────────────────────────────────────

        def _append_log(self, text: str) -> None:
            self._log_lines.append(text)
            if len(self._log_lines) > _LOG_MAX_LINES:
                self._log_lines = self._log_lines[-_LOG_MAX_LINES:]
            box = self._log_box
            box.configure(state="normal")
            box.delete("1.0", "end")
            box.insert("end", "\n".join(self._log_lines))
            box.configure(state="disabled")
            box.see("end")

        # ── ReviewPanel / CardMasterView stubs ───────────────────────────

        def _open_review_panel(self) -> None:
            from gui.review_panel import ReviewPanel  # noqa: F401
            pass

        def _open_card_master_view(self) -> None:
            from gui.card_master_view import CardMasterView  # noqa: F401
            pass

        # ── Lifecycle ────────────────────────────────────────────────────

        def run(self) -> None:
            """mainloop を開始する（ブロッキング）。"""
            self.mainloop()

        def _on_close(self) -> None:
            self._stop_event.set()
            self.destroy()

    globals()["SeatPanel"] = SeatPanel
    globals()["DashboardWindow"] = DashboardWindow
