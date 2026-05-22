"""gui/dashboard.py

Phase 4: customtkinter ベースの GUI ダッシュボード。

レイアウト (Phase 4-C2 で「ハンド履歴 (advisory)」パネルを追加):
  ┌──────────────────────────────────────────────────────┐
  │  ヘッダー: セッション情報 / ハンド番号 / ストリート / ポット   │
  ├─────────────────┬────────────────────────────────────┤
  │  プレイヤー一覧   │  アクションログ                       │
  │  (左パネル)      │  (中央パネル, スクロール可)             │
  ├─────────────────┴────────────────────────────────────┤
  │  ハンド履歴 (advisory): #1 [OK]  #2 [REVIEW] [RAW] ... │
  │  Latest advisory: status / reason / bootstrap / diff   │
  ├──────────────────────────────────────────────────────┤
  │  コントロール: 新ハンド / ウィナー確定 / リバイ             │
  └──────────────────────────────────────────────────────┘

スレッド安全設計:
  - IntegrationThread → on_action(record) → _update_queue.put(record)
  - IntegrationThread → on_hand_finalized(hand_id) → _hand_finalized_queue.put
    (Phase 4-C2; advisory が ``get_reconstruction_result(hand_id)`` で読める
     状態になった後に発火する)
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

# Phase 5-F: history Textbox の行数上限。超えた分は古い行 (= 上端) から削る。
# IntegrationThread 側の advisory state は MAX_ADVISORY_HANDS=500 で eviction する
# が、GUI history はざっくりログなので別途上限を設けて長時間運用での Tk widget
# メモリ圧を回避する (= advisory state とは独立に管理)。
MAX_HISTORY_LINES: int = 1000


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
        self._integration_thread: Optional[object] = None  # set_integration_thread で接続
        self._update_queue: queue.Queue["ActionRecord"] = queue.Queue()
        self._rfid_card_queue: queue.Queue = queue.Queue()
        # Phase 4-C2: hand 終局通知のスレッド安全な受け口。
        # IntegrationThread が on_hand_finalized(hand_id) を呼び、main thread の
        # _poll_updates が consume して advisory パネルを更新する。
        self._hand_finalized_queue: queue.Queue[int] = queue.Queue()
        # Phase 5-G: "Apply patch" ボタンが対象とする最新 advisory hand_id。
        # ``_apply_hand_finalized`` の末尾で更新される。``_cmd_apply_patch`` から参照。
        self._latest_advisory_hand_id: Optional[int] = None
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
        self._header = ctk.CTkFrame(root, height=80, corner_radius=0)
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

        # BTN/SB/BB/Actor/To call 詳細行 (BettingState から)
        self._lbl_betting = ctk.CTkLabel(
            self._header,
            text="BTN: — | SB: — | BB: — | Actor: — | Street: — | To call: —",
            anchor="w", font=("", 11, "bold"),
        )
        self._lbl_betting.grid(row=2, column=0, columnspan=4, padx=12, pady=(0, 4),
                                sticky="w")

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

        # Phase 4-C2: ハンド履歴 (advisory) パネル
        # 各 hand 終局時に 1 行追加される。badge tag で色分け。
        # その直下に "最新ハンドの詳細" 行を 1 段、別 label として並べる。
        self._history_frame = ctk.CTkFrame(root, corner_radius=0, height=140)
        self._history_frame.grid(row=2, column=0, sticky="ew", padx=0, pady=(2, 0))
        self._history_frame.grid_columnconfigure(0, weight=1)

        self._history_title = ctk.CTkLabel(
            self._history_frame, text="ハンド履歴 (advisory)",
            anchor="w", font=("", 11, "bold"),
        )
        self._history_title.grid(row=0, column=0, padx=8, pady=(4, 0), sticky="w")

        self._history_box = ctk.CTkTextbox(
            self._history_frame, state="disabled", wrap="none",
            font=("Courier", 10), height=80,
        )
        self._history_box.grid(row=1, column=0, padx=8, pady=(0, 2), sticky="ew")
        # badge / label tag に対応する色 (gui.reconstruction_badges と揃える)
        from gui.reconstruction_badges import (
            BADGE_COLOR_OK, BADGE_COLOR_REVIEW, BADGE_COLOR_SKIPPED,
        )
        self._history_box.tag_config("ok",      foreground=BADGE_COLOR_OK)
        self._history_box.tag_config("review",  foreground=BADGE_COLOR_REVIEW)
        self._history_box.tag_config("skipped", foreground=BADGE_COLOR_SKIPPED)

        # "最新ハンドの advisory 詳細" 行 (status / reason / bootstrap / diff)
        self._lbl_latest_advisory = ctk.CTkLabel(
            self._history_frame, anchor="w", font=("", 10),
            text="Latest advisory: —",
        )
        self._lbl_latest_advisory.grid(row=2, column=0, padx=8, pady=(0, 4), sticky="w")

        # Phase 5-H: 最新 advisory hand の patch proposal の field 単位 diff を
        # Toplevel ウィンドウに read-only 表示するボタン。apply は **しない**。
        self._btn_show_patch_details = ctk.CTkButton(
            self._history_frame, text="Show details", width=110,
            command=self._cmd_show_patch_details,
        )
        self._btn_show_patch_details.grid(
            row=2, column=1, padx=(4, 4), pady=(0, 4), sticky="e",
        )

        # Phase 5-G: 最新 advisory hand に patch proposal を適用するボタン。
        # 押下時は ``tkinter.messagebox.askyesno`` で確認ダイアログ。
        # in-memory summary correction のみで、JSON / PHH / GameStateManager には
        # 触らない (= ``IntegrationThread.apply_patch_proposal`` 経由)。
        self._btn_apply_patch = ctk.CTkButton(
            self._history_frame, text="Apply patch", width=110,
            command=self._cmd_apply_patch,
        )
        self._btn_apply_patch.grid(row=2, column=2, padx=(4, 8), pady=(0, 4), sticky="e")

        # 下部コントロール (Phase 4-C2 で row 2 → row 3 にずらした)
        ctrl = ctk.CTkFrame(root, corner_radius=0)
        ctrl.grid(row=3, column=0, sticky="ew", padx=0, pady=0)
        self._build_controls(ctrl)

        # Phase 5-Ia: Manual action pad (table view + action pad + history edit)
        # 既存の controls の下に独立 frame として配置する。row=4 で root に追加。
        self._manual_action_frame = ctk.CTkFrame(root, corner_radius=0)
        self._manual_action_frame.grid(row=4, column=0, sticky="ew", padx=0, pady=(2, 0))
        self._build_manual_action_frame()

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
        ctrl.grid_columnconfigure(tuple(range(10)), weight=1)

        seats = [str(s) for s in sorted(self._gs.get_stacks().keys())]

        # ── Row 0: 新ハンド / BTN補正 / ウィナー確定 / リバイ ─────────────────
        ctk.CTkButton(ctrl, text="新ハンド", width=100,
                      command=self._cmd_new_hand).grid(row=0, column=0, padx=8, pady=(10, 4))

        # BTN 補正: 通常は空欄 (自動回転)、値を選ぶと次ハンド1回だけ手動上書き
        ctk.CTkLabel(ctrl, text="BTN補正:").grid(row=0, column=1, padx=(12, 2), pady=(10, 4))
        self._button_seat_var = ctk.StringVar(value="")
        self._button_seat_menu = ctk.CTkOptionMenu(
            ctrl, variable=self._button_seat_var,
            values=[""] + seats, width=70,
        )
        self._button_seat_menu.grid(row=0, column=2, padx=2, pady=(10, 4))

        ctk.CTkLabel(ctrl, text="ウィナー:").grid(row=0, column=3, padx=(12, 2), pady=(10, 4))
        self._winner_var = ctk.StringVar(value=seats[0] if seats else "1")
        ctk.CTkOptionMenu(ctrl, variable=self._winner_var, values=seats,
                          width=70).grid(row=0, column=4, padx=2, pady=(10, 4))
        ctk.CTkButton(ctrl, text="確定", width=70,
                      command=self._cmd_winner).grid(row=0, column=5, padx=(2, 12), pady=(10, 4))

        ctk.CTkLabel(ctrl, text="リバイ 席:").grid(row=0, column=6, padx=(12, 2), pady=(10, 4))
        self._rebuy_seat_var = ctk.StringVar(value=seats[0] if seats else "1")
        ctk.CTkOptionMenu(ctrl, variable=self._rebuy_seat_var, values=seats,
                          width=70).grid(row=0, column=7, padx=2, pady=(10, 4))
        self._rebuy_amount_entry = ctk.CTkEntry(ctrl, width=90, placeholder_text="金額")
        self._rebuy_amount_entry.grid(row=0, column=8, padx=2, pady=(10, 4))
        ctk.CTkButton(ctrl, text="適用", width=70,
                      command=self._cmd_rebuy).grid(row=0, column=9, padx=(2, 12), pady=(10, 4))

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

        # ── Row 4: Blind level 変更 (Phase 5-C) ───────────────────────────────
        # 次 hand から有効。現在進行中の hand には影響しない (= bs.start_hand で
        # 旧 blinds が既に固定済み)。
        ctk.CTkLabel(ctrl, text="Blinds:").grid(row=4, column=0, padx=8, pady=(4, 10))

        ctk.CTkLabel(ctrl, text="SB:").grid(row=4, column=1, padx=(12, 2), pady=(4, 10), sticky="e")
        # 現在値を placeholder に出す (空欄なら現在値を維持)
        cur_sb = int(getattr(self._gs, "_sb", 0))
        cur_bb = int(getattr(self._gs, "_bb", 0))
        self._blinds_sb_entry = ctk.CTkEntry(
            ctrl, width=90, placeholder_text=str(cur_sb),
        )
        self._blinds_sb_entry.grid(row=4, column=2, padx=2, pady=(4, 10))

        ctk.CTkLabel(ctrl, text="BB:").grid(row=4, column=3, padx=(12, 2), pady=(4, 10), sticky="e")
        self._blinds_bb_entry = ctk.CTkEntry(
            ctrl, width=90, placeholder_text=str(cur_bb),
        )
        self._blinds_bb_entry.grid(row=4, column=4, padx=2, pady=(4, 10))

        ctk.CTkButton(ctrl, text="Blinds 更新", width=110,
                      command=self._cmd_update_blinds).grid(
            row=4, column=5, columnspan=2, padx=(2, 12), pady=(4, 10),
        )

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

        # BTN補正: 入力欄に値があれば1回だけ手動上書きし、入力欄をクリアする
        btn_raw = self._button_seat_var.get().strip()
        if btn_raw:
            try:
                btn = int(btn_raw)
                if self._integration_thread is not None:
                    self._integration_thread.set_next_button_seat(btn)
                self._append_log(f"BTN 補正: 次ハンド button=席{btn}", tag="medium")
                self._button_seat_var.set("")
            except ValueError:
                self._append_log(f"⚠ ボタン席の指定が不正: {btn_raw!r}", tag="review")

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

    def _cmd_update_blinds(self) -> None:
        """Phase 5-C: SB/BB 入力欄から blinds を更新する。

        値は次 hand 開始時から有効。現在進行中の hand の HandSummary には影響しない。
        IntegrationThread.update_blinds が GameStateManager / HandReconstructor の
        state も同時に同期する。
        """
        try:
            sb_raw = self._blinds_sb_entry.get().strip()
            bb_raw = self._blinds_bb_entry.get().strip()
        except AttributeError:
            self._append_log("⚠ Blind 入力欄が見つかりません。", tag="review")
            return

        try:
            sb = int(sb_raw)
            bb = int(bb_raw)
        except ValueError:
            self._append_log(
                f"⚠ Blind 入力が不正です (SB={sb_raw!r} BB={bb_raw!r})。",
                tag="review",
            )
            return

        if sb <= 0 or bb <= 0:
            self._append_log(
                f"⚠ Blind は正の整数で指定してください (SB={sb} BB={bb})。",
                tag="review",
            )
            return
        if sb >= bb:
            self._append_log(
                f"⚠ SB ({sb}) は BB ({bb}) より小さい必要があります。",
                tag="review",
            )
            return

        if self._integration_thread is None:
            self._append_log(
                "⚠ Integration thread 未接続: blind は反映されません。",
                tag="review",
            )
            return

        try:
            self._integration_thread.update_blinds(sb, bb)
        except (ValueError, AttributeError) as e:
            self._append_log(f"⚠ Blind 更新失敗: {e}", tag="review")
            return

        self._append_log(
            f"Blinds 更新: SB={sb:,} BB={bb:,} (次ハンドから有効)",
            tag="medium",
        )
        # 入力欄をクリアし、placeholder に現在値を反映する
        try:
            self._blinds_sb_entry.delete(0, "end")
            self._blinds_bb_entry.delete(0, "end")
            self._blinds_sb_entry.configure(placeholder_text=str(sb))
            self._blinds_bb_entry.configure(placeholder_text=str(bb))
        except AttributeError:
            pass

    def _cmd_show_patch_details(self) -> None:
        """Phase 5-H: 最新 advisory hand の patch proposal を Toplevel に表示する handler。

        フロー:
          1. ``_latest_advisory_hand_id`` 無 → 警告ログ → return
          2. ``_integration_thread`` 無 → 警告ログ → return
          3. accessor で summary / result を read-only に取り出す
          4. ``result.patch_proposal`` 無 → 警告ログ → return
          5. ``summarize_patch_proposal_for_view`` で ``FieldDiffView`` list を作る
          6. list が空 → "差分なし" の Toplevel を出して return
             (= proposal が field を持たない / 全 entry が malformed のケース)
          7. format してテキスト行 list にし、``_create_patch_detail_window`` で
             Toplevel を生成

        **約束**: read-only。``apply_patch_proposal`` も ``_last_summary_by_hand_id``
        も触らない (= Apply ボタンと完全独立)。
        """
        from core.patch_apply import summarize_patch_proposal_for_view

        hand_id = self._latest_advisory_hand_id
        thread = self._integration_thread
        if hand_id is None:
            self._append_log(
                "⚠ Show details: 表示できる advisory hand がありません。",
                tag="review",
            )
            return
        if thread is None:
            self._append_log(
                "⚠ Show details: integration thread が未接続です。",
                tag="review",
            )
            return
        try:
            summary = thread.get_last_summary(hand_id)
        except Exception:
            summary = None
        try:
            result = thread.get_reconstruction_result(hand_id)
        except Exception:
            result = None
        proposal = getattr(result, "patch_proposal", None) if result is not None else None
        if proposal is None:
            self._append_log(
                f"⚠ Show details: hand #{hand_id} に patch proposal が"
                "ありません。",
                tag="review",
            )
            return

        views = summarize_patch_proposal_for_view(summary, proposal)
        title = f"Patch details — hand #{hand_id}"
        if not views:
            # proposal はあるが view 化できる field が 1 つも無い (= 全 entry が
            # malformed / empty)。"差分なし" の小さな window を出して return。
            self._create_patch_detail_window(
                title, [f"hand #{hand_id} には表示可能な diff がありません。"],
            )
            return

        lines = self._format_patch_detail_lines(hand_id, views, result=result)
        self._create_patch_detail_window(title, lines)

    @staticmethod
    def _format_patch_detail_lines(
        hand_id: int,
        views: list,                     # list[FieldDiffView]
        *,
        result: object = None,
    ) -> list[str]:
        """``FieldDiffView`` のリストを Toplevel 用の表示テキスト行に整形する。

        出力フォーマット (1 field あたり 4–5 行 + 区切り)::

            [applies] resolution_type
              online:  fold_win
              offline: showdown
              note:    resolution_type differs (online=fold_win, offline=showdown)

            [skip]    winner_seat
              online:  1
              offline: 2

        ``[applies]`` / ``[skip]`` マーカーで apply 対象かを差別化。
        result.patch_applied が True の場合はヘッダーに "(patch applied)" を付ける。
        """
        header_suffix = ""
        if result is not None and getattr(result, "patch_applied", False):
            applied = getattr(result, "applied_fields", None) or []
            if applied:
                header_suffix = (
                    f"  (patch applied — applied_fields=" f"{','.join(applied)})"
                )
            else:
                header_suffix = "  (patch applied)"
        lines: list[str] = [
            f"Hand #{hand_id} patch proposal — {len(views)} field(s){header_suffix}",
            "",
        ]
        for v in views:
            marker = "[applies]" if v.is_applicable else "[skip]   "
            lines.append(f"{marker} {v.field}")
            lines.append(f"  online:  {v.online_repr}")
            lines.append(f"  offline: {v.offline_repr}")
            if v.note:
                lines.append(f"  note:    {v.note}")
            lines.append("")
        return lines

    def _create_patch_detail_window(
        self, title: str, lines: list[str],
    ) -> None:
        """Phase 5-H: Tkinter Toplevel ウィンドウを生成して text を表示する。

        テストでは ``dash._create_patch_detail_window = MagicMock()`` で差し替えて
        Toplevel 実体を作らずに ``_cmd_show_patch_details`` の動作を検証する想定。
        実装は最小限 (= Toplevel + CTkTextbox + lines を insert) に留める。
        """
        ctk = self._ctk
        try:
            top = ctk.CTkToplevel(self._root)
            top.title(title)
            top.geometry("720x520")
            box = ctk.CTkTextbox(top, font=("Courier", 11), wrap="none")
            box.pack(fill="both", expand=True, padx=8, pady=8)
            box.configure(state="normal")
            for line in lines:
                box.insert("end", line + "\n")
            box.configure(state="disabled")
        except Exception as e:
            # widget 生成に失敗しても GUI 全体を巻き込まない (= ログだけ残す)
            self._append_log(
                f"⚠ Show details: Toplevel 生成に失敗しました: {e}",
                tag="review",
            )

    def _ask_apply_patch_confirmation(
        self, title: str, message: str,
    ) -> Optional[bool]:
        """Phase 5-G: 確認ダイアログを表示する小さな hook (テストで override 可能)。

        Returns:
            True:  operator が "はい" を選んだ
            False: operator が "いいえ" / キャンセルを選んだ
            None:  ダイアログ自体が表示できなかった (= display 無し環境など)。
                   呼び出し側は False とは区別して "abort" として扱う。

        実装は ``tkinter.messagebox.askyesno`` を遅延 import で呼ぶだけ。
        tkinter が無い環境では ``ImportError`` を catch して ``None`` を返す。
        テストでは ``dash._ask_apply_patch_confirmation = MagicMock(return_value=...)``
        で差し替えれば dialog 不要に検証できる。
        """
        try:
            from tkinter import messagebox
            return bool(messagebox.askyesno(title, message))
        except Exception:
            return None

    def _cmd_apply_patch(self) -> None:
        """Phase 5-G: 最新 advisory hand に対して patch proposal を適用するボタンの handler。

        フロー:
          1. ``_latest_advisory_hand_id`` が無ければログに警告して abort
          2. ``_integration_thread`` 未接続なら警告して abort
          3. accessor で patch proposal の有無を確認 (read-only)
          4. ``_ask_apply_patch_confirmation`` で operator に確認
             - True  → 5. へ
             - False → ユーザーキャンセル log → return
             - None  → ダイアログ表示不可 log → return (= safe default: apply しない)
          5. confirm OK なら ``IntegrationThread.apply_patch_proposal(hand_id)`` を呼ぶ
          6. 成功時: ``_apply_hand_finalized(hand_id, append_to_history=False)`` で
             latest advisory ラベルだけ refresh (history 行は重複させない)
          7. 失敗 / 例外時はログに warning を出すだけで GUI を壊さない

        **約束**:
          - apply は in-memory summary correction のみ
            (= ``IntegrationThread.apply_patch_proposal`` の約束に従う)
          - confirmation ダイアログが出せない環境では **apply しない** (safe default)
        """
        hand_id = self._latest_advisory_hand_id
        thread = self._integration_thread
        if hand_id is None:
            self._append_log(
                "⚠ Apply patch: 適用対象の advisory hand がありません。",
                tag="review",
            )
            return
        if thread is None:
            self._append_log(
                "⚠ Apply patch: integration thread が未接続です。",
                tag="review",
            )
            return
        # accessor で proposal の有無を read-only 確認
        try:
            result = thread.get_reconstruction_result(hand_id)
        except Exception:
            result = None
        proposal = getattr(result, "patch_proposal", None) if result is not None else None
        if proposal is None:
            self._append_log(
                f"⚠ Apply patch: hand #{hand_id} に patch proposal がありません。",
                tag="review",
            )
            return
        # 確認ダイアログ (= operator が誤クリックで apply してしまわないように)
        ok = self._ask_apply_patch_confirmation(
            "Apply patch",
            f"hand #{hand_id} に safe patch field を適用しますか?\n"
            f"(in-memory summary correction のみ、JSON / PHH には触りません)",
        )
        if ok is None:
            # ダイアログ表示不可 (= display 無し環境など) では safe abort
            self._append_log(
                "⚠ Apply patch: 確認ダイアログが表示できないため適用を中止しました。",
                tag="review",
            )
            return
        if not ok:
            self._append_log(
                f"Apply patch: hand #{hand_id} の適用をキャンセルしました。",
                tag="medium",
            )
            return
        # apply 実行 (= IntegrationThread API)
        try:
            applied = bool(thread.apply_patch_proposal(hand_id))
        except Exception as e:
            self._append_log(
                f"⚠ Apply patch failed for hand #{hand_id}: {e}",
                tag="review",
            )
            return
        if applied:
            # history 行を重複させずに latest advisory ラベルだけ refresh する
            self._append_log(
                f"Applied patch to hand #{hand_id}.",
                tag="medium",
            )
            try:
                self._apply_hand_finalized(hand_id, append_to_history=False)
            except Exception as e:
                self._append_log(
                    f"⚠ Apply patch refresh failed: {e}",
                    tag="review",
                )
        else:
            self._append_log(
                f"⚠ Apply patch: hand #{hand_id} に適用可能な whitelist field が"
                "ありません。",
                tag="review",
            )

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
        # Phase 4-C2: hand 終局通知を処理 (advisory 表示更新)
        try:
            while True:
                hand_id = self._hand_finalized_queue.get_nowait()
                self._apply_hand_finalized(hand_id)
        except queue.Empty:
            pass
        # ゲーム状態のヘッダーを常に最新化
        self._refresh_header()
        # Phase 5-Ia: manual action pad の view も refresh
        try:
            self._refresh_manual_action_view()
        except Exception:
            pass
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

    def _apply_hand_finalized(
        self, hand_id: int, *, append_to_history: bool = True,
    ) -> None:
        """Phase 4-C2: hand 終局時に履歴パネル + 最新 advisory ラベルを更新する。

        IntegrationThread の advisory store (``_last_summary_by_hand_id`` /
        ``_last_reconstruction_by_hand_id``) を **read-only** で参照するだけで、
        online JSON / PHH / GameStateManager には触らない。

        Phase 5-E: blind 情報 (``blinds=SB/BB``, ``source=...``, ``blind_mismatch``)
        を Latest advisory ラベルと history 行に表示する。判定は ``summary.blinds`` /
        ``result.bootstrap_meta["blind_source"]`` / ``result.patch_proposal.fields``
        から read-only で行い、reconstruct ロジックには触らない。

        Phase 5-G: ``result.patch_applied`` が True のときは latest advisory に
        ``patch_applied=yes`` と ``applied_fields=...`` を出す。``_cmd_apply_patch``
        から refresh 用に呼ぶ際は ``append_to_history=False`` で history 行を
        重複させないようにする。``_latest_advisory_hand_id`` も更新するため、
        Apply ボタンが対象とする hand_id を正しく追跡する。
        """
        from gui.reconstruction_badges import (
            format_history_line, summarize_reconstruction,
        )

        # advisory accessor から HandSummary / HandReconstructionResult を引く。
        # IntegrationThread が未接続 / 対象 hand 無しなら全部 None で扱う。
        summary = None
        result = None
        thread = self._integration_thread
        if thread is not None:
            try:
                summary = thread.get_last_summary(hand_id)  # type: ignore[attr-defined]
            except Exception:
                summary = None
            try:
                result = thread.get_reconstruction_result(hand_id)  # type: ignore[attr-defined]
            except Exception:
                result = None

        badge_state = summarize_reconstruction(result)

        winner_seat = getattr(summary, "winner_seat", None) if summary is not None else None
        pot_total = getattr(summary, "pot_total", None) if summary is not None else None
        line = format_history_line(hand_id, winner_seat, pot_total, badge_state)

        # Phase 5-E: history 行に blind 情報の lightweight suffix を付ける。
        # - blind_source="current_state" のときだけ ``blinds=SB/BB (current_state)``
        #   (session_default はノイズ削減のため省略)
        # - blind_mismatch があれば ``(blind_mismatch)`` を末尾に付ける
        blind_suffix = self._format_blind_suffix_for_history(summary, result)
        if blind_suffix:
            line = line + "  " + blind_suffix

        # 履歴パネル: tag は status と同名 (ok / review / skipped) に揃える。
        # Phase 5-G: ``append_to_history=False`` のときは label のみ refresh する。
        if append_to_history:
            box = self._history_box
            box.configure(state="normal")
            box.insert("end", line + "\n", badge_state.status)
            # Phase 5-F: history Textbox を MAX_HISTORY_LINES 以下にトリム
            # (長時間運用での Tk widget メモリ圧回避)。
            self._trim_history_lines()
            box.configure(state="disabled")
            box.see("end")

        # "最新 advisory" ラベルを 1 行で更新
        detail_parts = [
            f"Latest advisory hand #{hand_id}",
            f"status={badge_state.status}",
            f"reason={badge_state.reason or 'none'}",
            f"bootstrap={badge_state.bootstrap_source}",
        ]
        # Phase 5-E: blind 情報を bootstrap と diff の間に挟む。
        # ``blinds=SB/BB (source=current_state|session_default|unknown)`` を常に表示
        # (値が無ければ ``?/?`` / ``unknown`` にフォールバック)。
        detail_parts.append(self._format_blind_for_advisory(summary, result))
        # blind_mismatch は patch_proposal に field="blinds" がある場合 (= Phase 5-D
        # の advisory が立った hand) のみ ``blind_mismatch=yes`` を出す。
        # ``=no`` は出さない (ノイズ削減)。
        if self._has_blind_patch(result):
            detail_parts.append("blind_mismatch=yes")

        if badge_state.diff_fields:
            detail_parts.append(f"diff={','.join(badge_state.diff_fields)}")
        else:
            detail_parts.append("diff=none")
        # Phase 5-A: patch proposal がある場合 field 名サマリだけ追加表示。
        # proposal の online/offline 詳細値は GUI には出さない (CLI --show-patches で見る)。
        if badge_state.patch_fields:
            detail_parts.append(f"patch_fields={','.join(badge_state.patch_fields)}")
        # Phase 5-G: patch_applied=True のときだけ "patch_applied=yes" を表示
        # (=no はノイズ削減のため出さない)。applied_fields は実際に適用された
        # field 名のリスト。
        if result is not None and getattr(result, "patch_applied", False):
            detail_parts.append("patch_applied=yes")
            applied = getattr(result, "applied_fields", None) or []
            if applied:
                detail_parts.append(f"applied_fields={','.join(applied)}")
        if badge_state.button_inferred:
            detail_parts.append("(button inferred from raw observations)")
        self._lbl_latest_advisory.configure(text="  |  ".join(detail_parts))

        # Phase 5-G: Apply ボタンが対象とする hand_id を更新。
        # 次のリフレッシュ (= apply 後の re-call) でも同じ hand を指すように維持。
        self._latest_advisory_hand_id = int(hand_id)

    def _trim_history_lines(self) -> None:
        """Phase 5-F: history Textbox の行数を ``MAX_HISTORY_LINES`` 以下に保つ。

        Tk Text widget の ``index("end-1c")`` は ``"<line>.<col>"`` 形式で、
        N 行 (= N 個の ``"\\n"`` を含む文字列) を insert した後は ``"<N+0>.X"``
        を返す (= line 番号 == 内容行数)。``MAX_HISTORY_LINES`` を超えた excess
        行を先頭から削除する。

        呼び出し側の前提:
          - ``self._history_box.configure(state="normal")`` が既に立てられている
            (= ``_apply_hand_finalized`` の insert 直後で呼ぶ)
          - MagicMock など index/delete が呼べない実装でも例外を出さず safe degrade
        """
        try:
            last = self._history_box.index("end-1c")
            line_no = int(str(last).split(".")[0])
        except (AttributeError, ValueError, TypeError, IndexError):
            return
        if line_no <= MAX_HISTORY_LINES:
            return
        excess = line_no - MAX_HISTORY_LINES
        try:
            self._history_box.delete("1.0", f"{excess + 1}.0")
        except Exception:
            # widget が想定外の状態でも GUI 全体を巻き込まない。
            # (dashboard.py には専用 logger が無いので silent swallow + 次回 invoke で
            # 再試行される)
            pass

    # ──────────────────────────────────────────────────────────────────────
    # Phase 5-E: blind advisory helpers (read-only)
    #
    # GUI が ``summary.blinds`` / ``result.bootstrap_meta`` / ``patch_proposal``
    # に触れる際の小さなヘルパ群。reconstruct ロジックには触らず、表示用に
    # 値を読むだけ。``result`` が None / 形が壊れていても例外を出さないように
    # defensively に getattr / isinstance で防御する。
    # ──────────────────────────────────────────────────────────────────────

    @staticmethod
    def _blind_source_text(result: object) -> str:
        """``bootstrap_meta["blind_source"]`` を short ラベルに正規化する。

        既知値 (``"current_state"`` / ``"session_default"``) はそのまま、
        それ以外 / 不在 / 不正は ``"unknown"`` で固定。
        """
        if result is None:
            return "unknown"
        meta = getattr(result, "bootstrap_meta", None)
        if not isinstance(meta, dict):
            return "unknown"
        src = meta.get("blind_source")
        if src in ("current_state", "session_default"):
            return src
        return "unknown"

    @staticmethod
    def _has_blind_patch(result: object) -> bool:
        """Phase 5-D の blind FieldPatch がぶら下がっているかを判定する。

        - dataclass の ``FieldPatch`` (live hook 経由) と dict 形 (JSONL 経由) の
          両方に対応 (= ``patch_proposal`` が asdict 後でも読める設計)。
        - ``proposal`` が None / fields が無い / 不正形式なら False。
        """
        if result is None:
            return False
        proposal = getattr(result, "patch_proposal", None)
        if proposal is None:
            return False
        fields = getattr(proposal, "fields", None)
        if fields is None and isinstance(proposal, dict):
            fields = proposal.get("fields")
        if not fields:
            return False
        for fp in fields:
            name = getattr(fp, "field", None)
            if name is None and isinstance(fp, dict):
                name = fp.get("field")
            if name == "blinds":
                return True
        return False

    def _format_blind_for_advisory(self, summary: object, result: object) -> str:
        """Latest advisory 用: ``blinds=<sb>/<bb> (source=<...>)`` 文字列を返す。

        ``summary.blinds`` が無い / dict でない場合は ``?/?`` にフォールバック、
        ``blind_source`` が無い場合は ``unknown`` にフォールバック。
        """
        sb_text = "?"
        bb_text = "?"
        if summary is not None:
            blinds = getattr(summary, "blinds", None)
            if isinstance(blinds, dict):
                sb = blinds.get("sb")
                bb = blinds.get("bb")
                if sb is not None:
                    sb_text = str(int(sb)) if isinstance(sb, (int, float)) else str(sb)
                if bb is not None:
                    bb_text = str(int(bb)) if isinstance(bb, (int, float)) else str(bb)
        source = self._blind_source_text(result)
        return f"blinds={sb_text}/{bb_text} (source={source})"

    def _format_blind_suffix_for_history(
        self, summary: object, result: object,
    ) -> str:
        """history 1 行用: 必要な場合だけ末尾に付ける lightweight suffix を返す。

        - blind_source="current_state" のときだけ ``blinds=SB/BB (current_state)``
          (session_default はキャッシュゲームでデフォルト状態なので省略)
        - blind FieldPatch があれば ``(blind_mismatch)`` を末尾に追記
        - どちらも該当しなければ空文字列 (= suffix なし)
        """
        parts: list[str] = []
        source = self._blind_source_text(result)
        if source == "current_state" and summary is not None:
            blinds = getattr(summary, "blinds", None)
            if isinstance(blinds, dict):
                sb = blinds.get("sb")
                bb = blinds.get("bb")
                if sb is not None and bb is not None:
                    parts.append(f"blinds={sb}/{bb} (current_state)")
        if self._has_blind_patch(result):
            parts.append("(blind_mismatch)")
        return "  ".join(parts)

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

        # BettingState 詳細 (BTN/SB/BB/Actor/Street/To call)
        bs_text = "BTN: — | SB: — | BB: — | Actor: — | Street: — | To call: —"
        actor_seat: Optional[int] = None
        if self._integration_thread is not None:
            try:
                bs = self._integration_thread.betting_state  # type: ignore[attr-defined]
                if bs.is_initialized:
                    actor_seat = bs.actor_seat
                    to_call = (
                        bs.call_amount_for(actor_seat) if actor_seat is not None else 0
                    )
                    bs_text = (
                        f"BTN: {bs.button_seat} | SB: {bs.sb_seat} | "
                        f"BB: {bs.bb_seat} | Actor: {actor_seat} | "
                        f"Street: {bs.street} | To call: {to_call}"
                    )
            except Exception:
                pass
        self._lbl_betting.configure(text=bs_text)

        # 手動入力の席ドロップダウンを現在の actor seat に追従させる
        if actor_seat is not None:
            self._sync_manual_seat(actor_seat)

        # RFID HTTP 受信機のステータスを表示
        if self._rfid_receiver is not None:
            try:
                st = self._rfid_receiver.status
                count = st.get("events_received", 0)
                port = st.get("bind_port", "")
                self._lbl_rfid.configure(text=f"RFID:{port} ({count}件)")
            except Exception:
                pass

    def set_integration_thread(self, thread: object) -> None:
        """IntegrationThread を後付けで接続する (BTN補正/BettingState表示用)。"""
        self._integration_thread = thread

    def _sync_manual_seat(self, actor_seat: int) -> None:
        """手動入力の席ドロップダウンを actor_seat に追従させる。

        actor が変わった時だけ書き込むので、ユーザが入力途中で他の seat に変えていた
        場合は同じ actor のままなら上書きしない (= 次に actor が動いた瞬間に追従する)。
        """
        new_val = str(actor_seat)
        if getattr(self, "_last_synced_actor", None) == actor_seat:
            return
        self._last_synced_actor = actor_seat
        if hasattr(self, "_manual_seat_var"):
            self._manual_seat_var.set(new_val)

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

    def on_hand_finalized(self, hand_id: int) -> None:
        """Phase 4-C2: IntegrationThread から呼ばれる hand 終局コールバック。

        ``_finalize_hand`` で advisory (reconstruction) が確定した *後* に呼ばれるので、
        main thread 側で ``get_reconstruction_result(hand_id)`` を呼べば advisory を
        安全に引ける。スレッド安全のため hand_id を queue に積むだけにする。
        """
        try:
            self._hand_finalized_queue.put(int(hand_id))
        except (TypeError, ValueError):
            pass  # 不正な hand_id は黙って破棄

    def run(self) -> None:
        """mainloop を開始する（ブロッキング）。"""
        self._append_log("セッション開始。ディーラーのアナウンスを待っています...", tag="medium")
        self._refresh_header()
        self._root.after(100, self._poll_updates)
        self._root.mainloop()

    # ──────────────────────────────────────────────────────────────────────
    # Phase 5-Ia: Manual action pad
    #
    # operator がテーブル上で actor seat に対して action / amount を直接入力
    # するための frame。3 カラム: 左 = table view (seat 一覧 + badges)、
    # 中央 = action pad (Fold / Check-Call / Bet / Raise / All-in / Undo /
    # Apply / Cancel) + keypad、右 = history edit pane (今回は read-only
    # placeholder。replace/delete/replay は Phase 5-I 将来課題)。
    #
    # 全ての action submit は ``IntegrationThread.submit_manual_action`` 経由。
    # GUI 側は backend の正本 state を保持しない。
    # ──────────────────────────────────────────────────────────────────────

    def _build_manual_action_frame(self) -> None:
        ctk = self._ctk
        frame = self._manual_action_frame
        # 3 カラム: table_view (weight 3) | action_pad (weight 2) | history_edit (weight 3)
        frame.grid_columnconfigure(0, weight=3)
        frame.grid_columnconfigure(1, weight=2)
        frame.grid_columnconfigure(2, weight=3)

        # GUI 内部 state (view のみ。正本は IntegrationThread)
        self._manual_edit_mode = "live"
        self._selected_action_index: Optional[int] = None
        self._pending_manual_action_kind: Optional[str] = None
        self._amount_var = ctk.StringVar(value="")

        # ── column 0: table view ────────────────────────────────────────
        self._table_view_frame = ctk.CTkFrame(frame, corner_radius=0)
        self._table_view_frame.grid(row=0, column=0, sticky="nsew", padx=(4, 2), pady=4)
        self._table_view_title = ctk.CTkLabel(
            self._table_view_frame, text="テーブル (manual)",
            anchor="w", font=("", 11, "bold"),
        )
        self._table_view_title.grid(row=0, column=0, padx=6, pady=(4, 2), sticky="w")
        # seat 行は ``_refresh_table_view`` で動的に作る。初期 build 時に 1 度作っておく。
        self._seat_cards: dict[int, dict] = {}
        seats = sorted(self._gs.get_stacks().keys())
        for idx, seat in enumerate(seats, start=1):
            card_frame = ctk.CTkFrame(self._table_view_frame, corner_radius=4)
            card_frame.grid(row=idx, column=0, padx=6, pady=2, sticky="ew")
            seat_lbl = ctk.CTkLabel(card_frame, text=f"席{seat}", anchor="w",
                                     font=("", 11, "bold"))
            seat_lbl.grid(row=0, column=0, padx=4, pady=2, sticky="w")
            stack_lbl = ctk.CTkLabel(card_frame, text="0", anchor="e",
                                      font=("Courier", 10))
            stack_lbl.grid(row=0, column=1, padx=4, pady=2, sticky="e")
            badge_lbl = ctk.CTkLabel(card_frame, text="", anchor="w",
                                      font=("", 10), text_color="#FFB347")
            badge_lbl.grid(row=1, column=0, columnspan=2, padx=4, pady=(0, 2),
                            sticky="w")
            last_lbl = ctk.CTkLabel(card_frame, text="—", anchor="w",
                                     font=("Courier", 9), text_color="#AAAAAA")
            last_lbl.grid(row=2, column=0, columnspan=2, padx=4, pady=(0, 2),
                           sticky="w")
            self._seat_cards[seat] = {
                "frame": card_frame, "seat_lbl": seat_lbl,
                "stack_lbl": stack_lbl, "badge_lbl": badge_lbl,
                "last_lbl": last_lbl,
            }

        # ── column 1: action pad + keypad ───────────────────────────────
        self._action_pad_frame = ctk.CTkFrame(frame, corner_radius=0)
        self._action_pad_frame.grid(row=0, column=1, sticky="nsew", padx=2, pady=4)

        self._lbl_action_pad_info = ctk.CTkLabel(
            self._action_pad_frame,
            text="Target: — | Mode: Live | Street: — | To call: — | Bet: — | MinR: —",
            anchor="w", font=("", 10),
        )
        self._lbl_action_pad_info.grid(row=0, column=0, columnspan=4,
                                        padx=6, pady=(4, 2), sticky="w")

        # action buttons (2x4 grid)
        self._btn_manual_fold = ctk.CTkButton(
            self._action_pad_frame, text="Fold", width=80,
            command=self._cmd_manual_fold,
        )
        self._btn_manual_fold.grid(row=1, column=0, padx=2, pady=2)
        self._btn_manual_check_call = ctk.CTkButton(
            self._action_pad_frame, text="Check", width=80,
            command=self._cmd_manual_check_call,
        )
        self._btn_manual_check_call.grid(row=1, column=1, padx=2, pady=2)
        self._btn_manual_bet = ctk.CTkButton(
            self._action_pad_frame, text="Bet", width=80,
            command=self._cmd_manual_bet,
        )
        self._btn_manual_bet.grid(row=1, column=2, padx=2, pady=2)
        self._btn_manual_raise = ctk.CTkButton(
            self._action_pad_frame, text="Raise", width=80,
            command=self._cmd_manual_raise,
        )
        self._btn_manual_raise.grid(row=1, column=3, padx=2, pady=2)
        self._btn_manual_all_in = ctk.CTkButton(
            self._action_pad_frame, text="All-in", width=80,
            command=self._cmd_manual_all_in,
        )
        self._btn_manual_all_in.grid(row=2, column=0, padx=2, pady=2)
        self._btn_manual_undo = ctk.CTkButton(
            self._action_pad_frame, text="Undo", width=80,
            fg_color="#555555",
            command=self._cmd_manual_undo,
        )
        self._btn_manual_undo.grid(row=2, column=1, padx=2, pady=2)
        self._btn_manual_apply = ctk.CTkButton(
            self._action_pad_frame, text="Apply", width=80,
            command=self._submit_pending_manual_action,
        )
        self._btn_manual_apply.grid(row=2, column=2, padx=2, pady=2)
        # Cancel Edit は将来の Edit/Replay モード切替用 placeholder (今回は何もしない)
        self._btn_manual_cancel_edit = ctk.CTkButton(
            self._action_pad_frame, text="Cancel Edit", width=80,
            fg_color="#555555", state="disabled",
            command=self._cmd_manual_cancel_edit,
        )
        self._btn_manual_cancel_edit.grid(row=2, column=3, padx=2, pady=2)

        # amount display
        self._lbl_amount_display = ctk.CTkLabel(
            self._action_pad_frame, textvariable=self._amount_var,
            anchor="e", font=("Courier", 14, "bold"), text_color="#FFFFFF",
        )
        self._lbl_amount_display.grid(row=3, column=0, columnspan=4,
                                       padx=6, pady=(4, 2), sticky="ew")

        # ── keypad (action_pad_frame 下部) ──────────────────────────────
        self._keypad_frame = ctk.CTkFrame(self._action_pad_frame, corner_radius=0)
        self._keypad_frame.grid(row=4, column=0, columnspan=4, padx=4, pady=(2, 4),
                                  sticky="ew")
        self._keypad_buttons: dict[str, object] = {}
        # 1..9
        for i, digit in enumerate(["1", "2", "3", "4", "5", "6", "7", "8", "9"]):
            r, c = divmod(i, 3)
            btn = ctk.CTkButton(
                self._keypad_frame, text=digit, width=46,
                command=lambda d=digit: self._cmd_keypad_digit(d),
            )
            btn.grid(row=r, column=c, padx=1, pady=1)
            self._keypad_buttons[digit] = btn
        # row 3: 00, 0, backspace
        self._keypad_buttons["00"] = ctk.CTkButton(
            self._keypad_frame, text="00", width=46,
            command=lambda: self._cmd_keypad_digit("00"),
        )
        self._keypad_buttons["00"].grid(row=3, column=0, padx=1, pady=1)
        self._keypad_buttons["0"] = ctk.CTkButton(
            self._keypad_frame, text="0", width=46,
            command=lambda: self._cmd_keypad_digit("0"),
        )
        self._keypad_buttons["0"].grid(row=3, column=1, padx=1, pady=1)
        self._keypad_buttons["bs"] = ctk.CTkButton(
            self._keypad_frame, text="⌫", width=46,
            command=self._cmd_keypad_backspace,
        )
        self._keypad_buttons["bs"].grid(row=3, column=2, padx=1, pady=1)
        # row 4: clear, enter, quick amounts (+BB, x2, POT, ALL-IN, +SB)
        self._keypad_buttons["clear"] = ctk.CTkButton(
            self._keypad_frame, text="Clear", width=46, fg_color="#555555",
            command=self._cmd_keypad_clear,
        )
        self._keypad_buttons["clear"].grid(row=4, column=0, padx=1, pady=1)
        self._keypad_buttons["enter"] = ctk.CTkButton(
            self._keypad_frame, text="Enter", width=46,
            command=self._cmd_keypad_enter,
        )
        self._keypad_buttons["enter"].grid(row=4, column=1, padx=1, pady=1)
        self._keypad_buttons["+sb"] = ctk.CTkButton(
            self._keypad_frame, text="+SB", width=46,
            command=lambda: self._cmd_keypad_quick("+sb"),
        )
        self._keypad_buttons["+sb"].grid(row=5, column=0, padx=1, pady=1)
        self._keypad_buttons["+bb"] = ctk.CTkButton(
            self._keypad_frame, text="+BB", width=46,
            command=lambda: self._cmd_keypad_quick("+bb"),
        )
        self._keypad_buttons["+bb"].grid(row=5, column=1, padx=1, pady=1)
        self._keypad_buttons["x2"] = ctk.CTkButton(
            self._keypad_frame, text="x2", width=46,
            command=lambda: self._cmd_keypad_quick("x2"),
        )
        self._keypad_buttons["x2"].grid(row=5, column=2, padx=1, pady=1)
        self._keypad_buttons["pot"] = ctk.CTkButton(
            self._keypad_frame, text="POT", width=46,
            command=lambda: self._cmd_keypad_quick("pot"),
        )
        self._keypad_buttons["pot"].grid(row=6, column=0, padx=1, pady=1)
        self._keypad_buttons["allin"] = ctk.CTkButton(
            self._keypad_frame, text="ALL-IN", width=46,
            command=lambda: self._cmd_keypad_quick("allin"),
        )
        self._keypad_buttons["allin"].grid(row=6, column=1, padx=1, pady=1)

        # ── column 2: history edit pane (Phase 5-I 将来課題のための placeholder)
        self._history_edit_frame = ctk.CTkFrame(frame, corner_radius=0)
        self._history_edit_frame.grid(row=0, column=2, sticky="nsew", padx=(2, 4),
                                        pady=4)
        self._history_edit_title = ctk.CTkLabel(
            self._history_edit_frame, text="Action history",
            anchor="w", font=("", 11, "bold"),
        )
        self._history_edit_title.grid(row=0, column=0, columnspan=3, padx=6,
                                        pady=(4, 2), sticky="w")
        self._history_edit_box = ctk.CTkTextbox(
            self._history_edit_frame, state="disabled", wrap="none",
            font=("Courier", 10), height=120,
        )
        self._history_edit_box.grid(row=1, column=0, columnspan=3, padx=6,
                                      pady=(0, 2), sticky="ew")
        self._lbl_history_selection = ctk.CTkLabel(
            self._history_edit_frame,
            text="(no selection — past edit not yet supported)",
            anchor="w", font=("", 9), text_color="#888888",
        )
        self._lbl_history_selection.grid(row=2, column=0, columnspan=3, padx=6,
                                            pady=(0, 2), sticky="w")
        # disabled placeholder buttons (Phase 5-Ic/Id で有効化)
        self._btn_history_replace = ctk.CTkButton(
            self._history_edit_frame, text="Replace selected", width=130,
            state="disabled", fg_color="#555555",
        )
        self._btn_history_replace.grid(row=3, column=0, padx=4, pady=(2, 4))
        self._btn_history_delete = ctk.CTkButton(
            self._history_edit_frame, text="Delete selected", width=130,
            state="disabled", fg_color="#555555",
        )
        self._btn_history_delete.grid(row=3, column=1, padx=4, pady=(2, 4))
        self._btn_history_replay = ctk.CTkButton(
            self._history_edit_frame, text="Replay from here", width=130,
            state="disabled", fg_color="#555555",
        )
        self._btn_history_replay.grid(row=3, column=2, padx=4, pady=(2, 4))

    # ── refresh ─────────────────────────────────────────────────────────

    def _refresh_manual_action_view(self) -> None:
        """``_poll_updates`` から呼び、IntegrationThread の現在状態で 3 ペインを更新する。"""
        state = self._fetch_manual_state()
        self._refresh_table_view(state)
        self._refresh_action_pad(state)
        self._refresh_history_edit_view(state)

    def _fetch_manual_state(self) -> dict:
        thread = self._integration_thread
        if thread is None:
            return {
                "actor_seat": None, "street": "—", "to_call": 0,
                "current_bet": 0, "min_raise": 0,
                "button_seat": None, "sb_seat": None, "bb_seat": None,
                "seat_views": [], "history_lines": [], "hand_id": 0,
                "is_initialized": False,
            }
        try:
            return thread.get_manual_action_state()  # type: ignore[attr-defined]
        except Exception:
            return {
                "actor_seat": None, "street": "—", "to_call": 0,
                "current_bet": 0, "min_raise": 0,
                "button_seat": None, "sb_seat": None, "bb_seat": None,
                "seat_views": [], "history_lines": [], "hand_id": 0,
                "is_initialized": False,
            }

    def _refresh_table_view(self, state: dict) -> None:
        for view in state.get("seat_views", []):
            seat = view["seat"]
            row = self._seat_cards.get(seat)
            if row is None:
                continue
            stack = view.get("stack", 0)
            try:
                row["stack_lbl"].configure(text=f"{int(stack):,}")
            except Exception:
                pass
            badges = view.get("badges", []) or []
            try:
                row["badge_lbl"].configure(text=" ".join(badges))
            except Exception:
                pass
            last = view.get("last_action")
            try:
                if last:
                    row["last_lbl"].configure(
                        text=f"{last.get('action')} {last.get('amount')}",
                    )
                else:
                    row["last_lbl"].configure(text="—")
            except Exception:
                pass
            # actor は seat_lbl の色で強調
            try:
                if view.get("is_actor"):
                    row["seat_lbl"].configure(text_color="#FFD54F")
                else:
                    row["seat_lbl"].configure(text_color="#CCCCCC")
            except Exception:
                pass

    def _refresh_action_pad(self, state: dict) -> None:
        actor = state.get("actor_seat")
        to_call = state.get("to_call", 0) or 0
        current_bet = state.get("current_bet", 0) or 0
        min_raise = state.get("min_raise", 0) or 0
        street = state.get("street", "—")
        info = (
            f"Target: {actor if actor is not None else '—'}  |  "
            f"Mode: {self._manual_edit_mode.capitalize()}  |  "
            f"Street: {street}  |  To call: {to_call}  |  "
            f"Bet: {current_bet}  |  MinR: {min_raise}"
        )
        try:
            self._lbl_action_pad_info.configure(text=info)
        except Exception:
            pass
        # Check/Call label 切替
        try:
            label = "Check" if int(to_call) == 0 else f"Call {int(to_call)}"
            self._btn_manual_check_call.configure(text=label)
        except Exception:
            pass

    def _refresh_history_edit_view(self, state: dict) -> None:
        try:
            box = self._history_edit_box
            box.configure(state="normal")
            box.delete("1.0", "end")
            for idx, line in enumerate(state.get("history_lines", []) or []):
                box.insert("end", f"{idx:02d}  {line}\n")
            box.configure(state="disabled")
        except Exception:
            pass

    # ── action button handlers ─────────────────────────────────────────

    def _resolve_manual_actor(self) -> Optional[int]:
        state = self._fetch_manual_state()
        actor = state.get("actor_seat")
        if isinstance(actor, int):
            return actor
        return None

    def _cmd_manual_fold(self) -> None:
        seat = self._resolve_manual_actor()
        if seat is None:
            self._append_log("⚠ Manual Fold: actor seat 不明。", tag="review")
            return
        self._invoke_manual_submit(seat, "fold", 0)

    def _cmd_manual_check_call(self) -> None:
        seat = self._resolve_manual_actor()
        if seat is None:
            self._append_log("⚠ Manual Check/Call: actor seat 不明。", tag="review")
            return
        state = self._fetch_manual_state()
        to_call = int(state.get("to_call", 0) or 0)
        if to_call == 0:
            self._invoke_manual_submit(seat, "check", 0)
        else:
            self._invoke_manual_submit(seat, "call", to_call)

    def _cmd_manual_bet(self) -> None:
        self._pending_manual_action_kind = "bet"
        self._append_log("Manual Bet: 金額をテンキーで入力 → Apply", tag="medium")

    def _cmd_manual_raise(self) -> None:
        self._pending_manual_action_kind = "raise"
        self._append_log("Manual Raise: 金額をテンキーで入力 → Apply", tag="medium")

    def _cmd_manual_all_in(self) -> None:
        seat = self._resolve_manual_actor()
        if seat is None:
            self._append_log("⚠ Manual All-in: actor seat 不明。", tag="review")
            return
        try:
            stack = int(self._gs.get_stack(seat))
        except Exception:
            self._append_log("⚠ Manual All-in: stack 不明。", tag="review")
            return
        if stack <= 0:
            self._append_log(f"⚠ Manual All-in: 席{seat} stack=0", tag="review")
            return
        self._invoke_manual_submit(seat, "allin", stack)

    def _cmd_manual_undo(self) -> None:
        thread = self._integration_thread
        if thread is None:
            self._append_log("⚠ Manual Undo: integration thread 未接続。",
                              tag="review")
            return
        try:
            ok = bool(thread.undo_last_manual_action())  # type: ignore[attr-defined]
        except Exception as e:
            self._append_log(f"⚠ Manual Undo failed: {e}", tag="review")
            return
        if ok:
            self._append_log("Manual Undo: 直前 record を削除しました。",
                              tag="medium")
        else:
            self._append_log("Manual Undo: 履歴が空です。", tag="review")

    def _cmd_manual_cancel_edit(self) -> None:
        # Phase 5-I 将来の Edit/Replay モード切替用。今は pending kind だけ落とす。
        self._pending_manual_action_kind = None
        self._amount_var.set("")

    # ── keypad handlers ────────────────────────────────────────────────

    def _cmd_keypad_digit(self, digit: str) -> None:
        try:
            cur = self._amount_var.get()
        except Exception:
            cur = ""
        new_val = (cur or "") + str(digit)
        # 先頭 0 のクリーンアップ (= "0123" 入力を許容しない)
        new_val = new_val.lstrip("0") or "0"
        try:
            self._amount_var.set(new_val)
        except Exception:
            pass

    def _cmd_keypad_backspace(self) -> None:
        try:
            cur = self._amount_var.get() or ""
        except Exception:
            cur = ""
        self._amount_var.set(cur[:-1])

    def _cmd_keypad_clear(self) -> None:
        try:
            self._amount_var.set("")
        except Exception:
            pass

    def _cmd_keypad_enter(self) -> None:
        # Enter = Apply の同義 (= pending action を amount 付きで送信)
        self._submit_pending_manual_action()

    def _cmd_keypad_quick(self, kind: str) -> None:
        state = self._fetch_manual_state()
        sb = state.get("current_bet", 0) or 0  # 仮 fallback
        bb_amount = 0
        thread = self._integration_thread
        if thread is not None:
            bs = getattr(thread, "betting_state", None)
            if bs is not None:
                sb = getattr(bs, "sb_amount", 0) or sb
                bb_amount = getattr(bs, "bb_amount", 0) or 0
        try:
            cur_amount = int(self._amount_var.get() or "0")
        except ValueError:
            cur_amount = 0
        if kind == "+sb":
            new_val = cur_amount + int(sb)
        elif kind == "+bb":
            new_val = cur_amount + int(bb_amount)
        elif kind == "x2":
            new_val = cur_amount * 2
        elif kind == "pot":
            # 簡略実装: 現在の pot total (= gs.pot) を amount に
            new_val = int(getattr(self._gs, "pot", 0))
        elif kind == "allin":
            actor = self._resolve_manual_actor()
            if actor is None:
                return
            new_val = int(self._gs.get_stack(actor))
        else:
            return
        try:
            self._amount_var.set(str(max(0, new_val)))
        except Exception:
            pass

    # ── submit pending ─────────────────────────────────────────────────

    def _submit_pending_manual_action(self) -> None:
        kind = self._pending_manual_action_kind
        if kind not in ("bet", "raise"):
            self._append_log(
                "⚠ Apply: Bet または Raise を先に選択してください。",
                tag="review",
            )
            return
        try:
            raw = self._amount_var.get() or ""
        except Exception:
            raw = ""
        raw = raw.strip()
        if not raw:
            self._append_log(
                f"⚠ Apply: 金額が未入力です ({kind})。",
                tag="review",
            )
            return
        try:
            amount = int(raw)
        except ValueError:
            self._append_log(f"⚠ Apply: 金額が不正です: {raw!r}", tag="review")
            return
        if amount <= 0:
            self._append_log(
                f"⚠ Apply: 金額は正の整数で指定してください: {amount}",
                tag="review",
            )
            return
        seat = self._resolve_manual_actor()
        if seat is None:
            self._append_log("⚠ Apply: actor seat 不明。", tag="review")
            return
        self._invoke_manual_submit(seat, kind, amount)
        # 送信後は pending state をリセット
        self._pending_manual_action_kind = None
        try:
            self._amount_var.set("")
        except Exception:
            pass

    def _invoke_manual_submit(
        self, seat: int, action: str, amount: int,
    ) -> None:
        thread = self._integration_thread
        if thread is None:
            self._append_log(
                "⚠ Manual action: integration thread が未接続です。",
                tag="review",
            )
            return
        try:
            thread.submit_manual_action(seat, action, amount)  # type: ignore[attr-defined]
        except Exception as e:
            self._append_log(
                f"⚠ Manual action failed (seat={seat} {action} {amount}): {e}",
                tag="review",
            )
            return
        self._append_log(
            f"Manual: 席{seat} {action} {amount}",
            tag="medium",
        )

    def _on_close(self) -> None:
        self._stop_event.set()
        self._root.destroy()
