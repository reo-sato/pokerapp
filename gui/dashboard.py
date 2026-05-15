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
import re
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


_CARD_RE = re.compile(r'[2-9TJQKA][hdcs]', re.IGNORECASE)


def _parse_cards(text: str) -> list[str]:
    """スペース区切りまたは連結表記のカード文字列を正規化リストに変換する。

    例: "AhKd" → ["Ah", "Kd"],  "ah kd" → ["Ah", "Kd"]
    """
    return [m[0].upper() + m[1].lower() for m in _CARD_RE.findall(text)]


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
        self._log_box.tag_config("review", foreground="#FF5252")

        # 下部コントロール
        ctrl = ctk.CTkFrame(root, corner_radius=0)
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
        ctrl.grid_columnconfigure((0, 1, 2, 3, 4, 5, 6, 7), weight=1)

        seats = [str(s) for s in sorted(self._gs.get_stacks().keys())]

        # ── Row 0: 新ハンド / ウィナー確定 / リバイ ──────────────────────────
        ctk.CTkButton(ctrl, text="新ハンド", width=100,
                      command=self._cmd_new_hand).grid(row=0, column=0, padx=8, pady=(10, 4))

        ctk.CTkLabel(ctrl, text="ウィナー:").grid(row=0, column=1, padx=(12, 2), pady=(10, 4))
        self._winner_var = ctk.StringVar(value=seats[0] if seats else "1")
        ctk.CTkOptionMenu(ctrl, variable=self._winner_var, values=seats,
                          width=70).grid(row=0, column=2, padx=2, pady=(10, 4))
        ctk.CTkButton(ctrl, text="確定", width=70,
                      command=self._cmd_winner).grid(row=0, column=3, padx=(2, 12), pady=(10, 4))

        ctk.CTkLabel(ctrl, text="リバイ 席:").grid(row=0, column=4, padx=(12, 2), pady=(10, 4))
        self._rebuy_seat_var = ctk.StringVar(value=seats[0] if seats else "1")
        ctk.CTkOptionMenu(ctrl, variable=self._rebuy_seat_var, values=seats,
                          width=70).grid(row=0, column=5, padx=2, pady=(10, 4))
        self._rebuy_amount_entry = ctk.CTkEntry(ctrl, width=90, placeholder_text="金額")
        self._rebuy_amount_entry.grid(row=0, column=6, padx=2, pady=(10, 4))
        ctk.CTkButton(ctrl, text="適用", width=70,
                      command=self._cmd_rebuy).grid(row=0, column=7, padx=(2, 12), pady=(10, 4))

        # ── Row 1: 手動アクション入力 ─────────────────────────────────────────
        ctk.CTkLabel(ctrl, text="手動入力:").grid(row=1, column=0, padx=8, pady=(4, 10))

        ctk.CTkLabel(ctrl, text="席:").grid(row=1, column=1, padx=(12, 2), pady=(4, 10), sticky="e")
        self._manual_seat_var = ctk.StringVar(value=seats[0] if seats else "1")
        ctk.CTkOptionMenu(ctrl, variable=self._manual_seat_var, values=seats,
                          width=70).grid(row=1, column=2, padx=2, pady=(4, 10))

        _actions = ["bet", "call", "raise", "check", "fold", "allin"]
        ctk.CTkLabel(ctrl, text="アクション:").grid(row=1, column=3, padx=(12, 2), pady=(4, 10), sticky="e")
        self._manual_action_var = ctk.StringVar(value="bet")
        ctk.CTkOptionMenu(ctrl, variable=self._manual_action_var, values=_actions,
                          width=90).grid(row=1, column=4, padx=2, pady=(4, 10))

        ctk.CTkLabel(ctrl, text="金額:").grid(row=1, column=5, padx=(12, 2), pady=(4, 10), sticky="e")
        self._manual_amount_entry = ctk.CTkEntry(ctrl, width=90, placeholder_text="0")
        self._manual_amount_entry.grid(row=1, column=6, padx=2, pady=(4, 10))

        ctk.CTkButton(ctrl, text="送信", width=70,
                      command=self._cmd_manual_action).grid(row=1, column=7, padx=(2, 12), pady=(4, 10))

        # ── Row 2: ホールカード手動入力 ───────────────────────────────────────
        ctk.CTkLabel(ctrl, text="ホールカード:").grid(row=2, column=0, padx=8, pady=(4, 4))

        ctk.CTkLabel(ctrl, text="席:").grid(row=2, column=1, padx=(12, 2), pady=(4, 4), sticky="e")
        self._manual_hole_seat_var = ctk.StringVar(value=seats[0] if seats else "1")
        ctk.CTkOptionMenu(ctrl, variable=self._manual_hole_seat_var, values=seats,
                          width=70).grid(row=2, column=2, padx=2, pady=(4, 4))

        ctk.CTkLabel(ctrl, text="カード:").grid(row=2, column=3, padx=(12, 2), pady=(4, 4), sticky="e")
        self._manual_hole_entry = ctk.CTkEntry(ctrl, width=120, placeholder_text="例: AhKd")
        self._manual_hole_entry.grid(row=2, column=4, columnspan=3, padx=2, pady=(4, 4), sticky="ew")

        ctk.CTkButton(ctrl, text="登録", width=70,
                      command=self._cmd_manual_hole_cards).grid(row=2, column=7, padx=(2, 12), pady=(4, 4))

        # ── Row 3: ボードカード手動入力 ───────────────────────────────────────
        ctk.CTkLabel(ctrl, text="ボード:").grid(row=3, column=0, padx=8, pady=(4, 10))

        ctk.CTkLabel(ctrl, text="カード:").grid(row=3, column=1, padx=(12, 2), pady=(4, 10), sticky="e")
        self._manual_board_entry = ctk.CTkEntry(ctrl, width=180, placeholder_text="例: AhKdQs (追加)")
        self._manual_board_entry.grid(row=3, column=2, columnspan=4, padx=2, pady=(4, 10), sticky="ew")

        ctk.CTkButton(ctrl, text="追加", width=70,
                      command=self._cmd_manual_board_cards).grid(row=3, column=6, padx=2, pady=(4, 10))
        ctk.CTkButton(ctrl, text="クリア", width=70, fg_color="#555555",
                      command=self._cmd_clear_board).grid(row=3, column=7, padx=(2, 12), pady=(4, 10))

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

    def _cmd_manual_action(self) -> None:
        from core.hand_log import ActionRecord

        try:
            seat = int(self._manual_seat_var.get())
        except ValueError:
            self._append_log("⚠ 席番号が不正です。", tag="review")
            return

        action = self._manual_action_var.get()

        raw = self._manual_amount_entry.get().strip()
        try:
            amount = int(raw) if raw else 0
        except ValueError:
            self._append_log(f"⚠ 金額が不正です: {raw!r}", tag="review")
            return

        try:
            self._gs.apply_action(seat, action, amount)
            needs_review = False
        except Exception as e:
            self._append_log(f"⚠ アクション適用失敗: {e}", tag="review")
            needs_review = True

        record = ActionRecord(
            hand_id=self._gs.hand_id,
            timestamp=datetime.now().isoformat(timespec="milliseconds"),
            street=self._gs.street,
            seat=seat,
            player_name=self._gs.get_player_name(seat),
            action=action,
            amount=amount,
            pot_after=self._gs.pot,
            stack_after=self._gs.get_stack(seat),
            source={"manual": True, "audio": False, "rfid": False, "camera": False},
            needs_review=needs_review,
            confidence=1.0,
        )
        self._update_queue.put(record)
        self._manual_amount_entry.delete(0, "end")

    def _cmd_manual_hole_cards(self) -> None:
        import types

        try:
            seat = int(self._manual_hole_seat_var.get())
        except ValueError:
            self._append_log("⚠ 席番号が不正です。", tag="review")
            return

        raw = self._manual_hole_entry.get().strip()
        cards = _parse_cards(raw)

        if not cards:
            self._append_log(f"⚠ カードを認識できません: {raw!r}  (例: AhKd)", tag="review")
            return
        if len(cards) > 2:
            self._append_log(f"⚠ ホールカードは2枚まで: {cards}", tag="review")
            return

        # 既存のホールカード表示をリセットしてから登録
        self._hole_cards_display[seat] = []
        if seat in self._player_rows:
            self._player_rows[seat]["hole_lbl"].configure(text="—")

        ts = time.time()
        for card in cards:
            self._rfid_card_queue.put(
                types.SimpleNamespace(role="seat", seat=seat, card=card, timestamp=ts)
            )

        self._append_log(f"手動 ホールカード 席{seat}: {' '.join(cards)}", tag="medium")
        self._manual_hole_entry.delete(0, "end")

    def _cmd_manual_board_cards(self) -> None:
        import types

        raw = self._manual_board_entry.get().strip()
        cards = _parse_cards(raw)

        if not cards:
            self._append_log(f"⚠ カードを認識できません: {raw!r}  (例: AhKdQs)", tag="review")
            return
        if len(self._board_cards_display) + len(cards) > 5:
            self._append_log(
                f"⚠ ボードは5枚まで (現在 {len(self._board_cards_display)} 枚): {cards}",
                tag="review",
            )
            return

        ts = time.time()
        for card in cards:
            self._rfid_card_queue.put(
                types.SimpleNamespace(role="board", seat=None, card=card, timestamp=ts)
            )

        self._append_log(f"手動 ボード追加: {' '.join(cards)}", tag="medium")
        self._manual_board_entry.delete(0, "end")

    def _cmd_clear_board(self) -> None:
        self._board_cards_display.clear()
        self._lbl_board.configure(text="ボード: —")
        self._append_log("ボードクリア", tag="medium")

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
        if record.source.get("manual"):
            src_flags.append("手動")
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
