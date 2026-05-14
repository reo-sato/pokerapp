#!/usr/bin/env python3
"""tools/gui_smoke_test.py

GUIDashboard の目視確認用スモークテストスクリプト。
実機（マイク・RFID・カメラ）なしで GUI を起動し、ダミーデータを自動投入する。

起動方法:
    python tools/gui_smoke_test.py

確認できる要素（起動後 1〜2 秒以内）:
    - 左パネル: seat 1〜3 のプレイヤー名・スタック
    - seat 1 の hole cards (Ah Kd)
    - ヘッダーにボードカード (7c 8d 9h Ts Jd)
    - アクションログに 5 件（うち 1 件に ⚠要確認）
    - 下部テストバーから CardMasterView / ReviewPanel を開けること
"""
from __future__ import annotations

import queue
import sys
import tempfile
import threading
import time
import types
from datetime import datetime
from pathlib import Path

# プロジェクトルートを sys.path に追加（tools/ の親ディレクトリ）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import customtkinter as ctk  # noqa: E402  (sys.path 設定後)

from core.game_state import GameStateManager, PlayerState
from core.hand_log import ActionRecord
from gui.dashboard import GUIDashboard
from output.json_writer import JsonWriter


# ――― ユーティリティ ―――

def _now_iso() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


# ――― ダミーデータ生成 ―――

def _make_game() -> GameStateManager:
    """3席のダミーゲーム状態を生成する。"""
    players = [
        PlayerState(seat=1, name="Alice",   stack=25000),
        PlayerState(seat=2, name="Bob",     stack=18500),
        PlayerState(seat=3, name="Charlie", stack=9000),
    ]
    gs = GameStateManager(players=players, sb=100, bb=200)
    gs.new_hand()
    return gs


def _make_dummy_records() -> list[ActionRecord]:
    """目視確認用 ActionRecord: 4件（通常）+ 1件（needs_review）。"""
    ts = _now_iso()
    return [
        ActionRecord(
            hand_id=1, timestamp=ts, street="preflop",
            seat=1, player_name="Alice", action="bet", amount=1000,
            pot_after=1200, stack_after=24000,
            source={"rfid": True, "audio": True, "camera": False},
            needs_review=False, confidence=0.95,
        ),
        ActionRecord(
            hand_id=1, timestamp=ts, street="preflop",
            seat=2, player_name="Bob", action="fold", amount=0,
            pot_after=1200, stack_after=18500,
            source={"rfid": False, "audio": True, "camera": False},
            needs_review=False, confidence=0.50,
        ),
        ActionRecord(
            hand_id=1, timestamp=ts, street="preflop",
            seat=3, player_name="Charlie", action="call", amount=1000,
            pot_after=2200, stack_after=8000,
            source={"rfid": False, "audio": True, "camera": True},
            needs_review=False, confidence=0.80,
        ),
        ActionRecord(
            hand_id=1, timestamp=ts, street="flop",
            seat=1, player_name="Alice", action="raise", amount=3000,
            pot_after=5200, stack_after=21000,
            source={"rfid": True, "audio": True, "camera": True},
            needs_review=False, confidence=1.00,
        ),
        # needs_review=True のレコード（赤太字で表示される）
        ActionRecord(
            hand_id=1, timestamp=ts, street="flop",
            seat=3, player_name="Charlie", action="bet", amount=500,
            pot_after=5700, stack_after=7500,
            source={"rfid": False, "audio": False, "camera": True},
            needs_review=True, confidence=0.30,
        ),
    ]


# ――― テストウィンドウ ―――

def _open_card_master(parent: ctk.CTk) -> None:
    """CardMasterView の目視確認用ダミーウィンドウ。"""
    win = ctk.CTkToplevel(parent)
    win.title("CardMasterView（スモークテスト）")
    win.geometry("500x310")
    win.lift()

    ctk.CTkLabel(win, text="CardMasterView",
                 font=("", 16, "bold")).pack(pady=(16, 2))
    ctk.CTkLabel(win, text="RFID タグ ↔ カード マッピング登録ビュー（ダミー表示）",
                 font=("", 11)).pack(pady=(0, 12))

    tbl = ctk.CTkFrame(win)
    tbl.pack(fill="x", padx=20)
    for col, header in enumerate(("Tag ID", "Card", "備考")):
        ctk.CTkLabel(tbl, text=header, font=("", 11, "bold"),
                     width=160, anchor="w").grid(row=0, column=col, padx=6, pady=2)
    dummy_rows = [
        ("04:AA:BB:CC:DD", "Ah", "席1 ホールカード"),
        ("04:11:22:33:44", "Kd", "席1 ホールカード"),
        ("04:55:66:77:88", "7c", "ボード pos-1"),
        ("04:99:AA:BB:CC", "8d", "ボード pos-2"),
    ]
    for r, (tag, card, note) in enumerate(dummy_rows, 1):
        for col, val in enumerate((tag, card, note)):
            ctk.CTkLabel(tbl, text=val, width=160, anchor="w").grid(
                row=r, column=col, padx=6, pady=2)

    ctk.CTkButton(win, text="閉じる", command=win.destroy).pack(pady=16)


def _open_review_panel(parent: ctk.CTk) -> None:
    """ReviewPanel の目視確認用ダミーウィンドウ。"""
    win = ctk.CTkToplevel(parent)
    win.title("ReviewPanel（スモークテスト）")
    win.geometry("740x360")
    win.lift()

    ctk.CTkLabel(win, text="ReviewPanel",
                 font=("", 16, "bold")).pack(pady=(16, 2))
    ctk.CTkLabel(win, text="needs_review=True のアクションを確認・修正するビュー",
                 font=("", 11)).pack(pady=(0, 8))

    box = ctk.CTkTextbox(win, font=("Courier", 12), height=150)
    box.pack(fill="both", expand=True, padx=20, pady=4)
    box.insert(
        "end",
        "⚠ 要確認レコード — 1件\n"
        "──────────────────────────────────────────────────────────────\n"
        "[flop    ] 席3 Charlie  bet       500"
        "  pot=  5,700  conf=0.30 (カメラ)  ⚠要確認\n",
    )
    box.configure(state="disabled")

    btns = ctk.CTkFrame(win)
    btns.pack(fill="x", padx=20, pady=(4, 12))
    ctk.CTkButton(btns, text="✓ 承認", width=100,
                  fg_color="#4CAF50").pack(side="left", padx=4)
    ctk.CTkButton(btns, text="✏ 修正", width=100,
                  fg_color="#FF9800").pack(side="left", padx=4)
    ctk.CTkButton(btns, text="✗ 破棄", width=100,
                  fg_color="#F44336").pack(side="left", padx=4)
    ctk.CTkButton(btns, text="閉じる", command=win.destroy).pack(side="right", padx=4)


# ――― メイン ―――

def main() -> None:
    # --- 依存オブジェクト生成 ---
    gs = _make_game()
    tmp_dir = tempfile.mkdtemp(prefix="poker_smoke_")
    writer  = JsonWriter(log_dir=tmp_dir, session_id="smoke_test")
    audio_q: queue.Queue = queue.Queue()
    stop_event = threading.Event()

    # --- GUIDashboard 生成（mainloop はまだ起動しない）---
    dash = GUIDashboard(
        game_state=gs,
        json_writer=writer,
        audio_queue=audio_q,
        stop_event=stop_event,
    )

    # --- ダミーデータ注入スケジュール（mainloop 開始後 800ms）---
    def _inject() -> None:
        now = time.time()
        # ホールカード: seat 1 に Ah Kd
        for card in ("Ah", "Kd"):
            dash.on_rfid_card(
                types.SimpleNamespace(role="seat", seat=1, card=card, timestamp=now)
            )
        # ボードカード: 5枚（フロップ3 + ターン1 + リバー1）
        for card in ("7c", "8d", "9h", "Ts", "Jd"):
            dash.on_rfid_card(
                types.SimpleNamespace(role="board", seat=None, card=card, timestamp=now)
            )
        # アクションログ
        for rec in _make_dummy_records():
            dash.on_action(rec)

    dash._root.after(800, _inject)

    # --- テストバー（row=3）: CardMasterView / ReviewPanel ボタン ---
    test_bar = ctk.CTkFrame(dash._root, height=46, corner_radius=0,
                             fg_color="#1a1a2e")
    test_bar.grid(row=3, column=0, sticky="ew")

    ctk.CTkLabel(test_bar, text="▶ スモークテスト:",
                 font=("", 11)).pack(side="left", padx=(12, 6), pady=10)
    ctk.CTkButton(
        test_bar,
        text="CardMasterView を開く",
        width=195,
        command=lambda: _open_card_master(dash._root),
    ).pack(side="left", padx=6, pady=10)
    ctk.CTkButton(
        test_bar,
        text="ReviewPanel を開く",
        width=175,
        command=lambda: _open_review_panel(dash._root),
    ).pack(side="left", padx=6, pady=10)

    # --- mainloop 起動（1回だけ・ブロッキング）---
    dash.run()


if __name__ == "__main__":
    main()
