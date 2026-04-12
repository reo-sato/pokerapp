"""tests/test_e2e.py

6人テーブル 1ハンド通しの E2E テスト。

シナリオ:
  button_seat=1 → BTN=席1, SB=席2, BB=席3, UTG=席4, HJ=席5, CO=席6
  Preflop: 席4 が BET 600、残り5席が全員 CALL 600
    → フォールドなし: active_count=6 を維持し street_overflow は発火しない
  RFID ボードカード 5 枚: As, Ks, Qs, Js, Ts (board_index=1〜5)
  席4 をウィナーとして宣言 → HandSummary が JSON に書き込まれる

アサート:
  1. hand_id=1 の ActionRecord が 6 件存在する
  2. HandSummary の button_seat == 1
  3. needs_review=True の ActionRecord が 0 件
  4. HandSummary の board == ["As","Ks","Qs","Js","Ts"]
"""
from __future__ import annotations

import json
import queue
import threading
import time
from pathlib import Path

import pytest

from core.event_queue import make_audio_queue, make_rfid_queue
from core.events import AudioEvent, RFIDEvent
from core.game_state import GameState
from core.hand_log import ActionRecord, PlayerState
from integration.engine import IntegrationThread
from output.json_writer import JsonWriter

# ── 定数 ─────────────────────────────────────────────────────────────────────

_BOARD_CARDS = ["As", "Ks", "Qs", "Js", "Ts"]
_WINNER_SEAT = 4


# ── ヘルパー ──────────────────────────────────────────────────────────────────

def _audio(action: str, amount: int | None = None, raw_text: str = "") -> AudioEvent:
    """テスト用 AudioEvent を生成する。"""
    return AudioEvent(
        action=action,
        amount=amount,
        timestamp="2026-04-12T00:00:00",
        raw_text=raw_text or action,
    )


def _board_rfid(card: str, board_idx: int) -> RFIDEvent:
    """テスト用ボード RFID イベントを生成する（role="board", board_index 付き）。"""
    return RFIDEvent(
        tag_id=f"AA:BB:CC:DD:0{board_idx}",
        card=card,
        reader_id=f"board_{board_idx}",
        role="board",
        seat=None,
        timestamp=time.time(),
        raw_tag_id=f"0{board_idx}",
        board_index=board_idx,
    )


def _wait_until(condition, timeout: float = 3.0, interval: float = 0.05) -> None:
    """condition() が True になるまで最大 timeout 秒ポーリングする。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return
        time.sleep(interval)
    raise TimeoutError(f"condition not met within {timeout}s")


def _make_6player_state() -> GameState:
    """button_seat=1 の 6人テーブルを new_hand() 済みで返す。"""
    players = [PlayerState(seat=i, name=f"P{i}", stack=10_000) for i in range(1, 7)]
    gs = GameState(players=players, sb=100, bb=200, button_seat=1)
    gs.new_hand()
    return gs


# ── テストクラス ──────────────────────────────────────────────────────────────

class TestE2EOneHand:
    """1ハンド通しの統合テスト（FR-05, FR-22, FR-24, FR-26, FR-35）。"""

    def test_6player_one_hand(self, tmp_path: Path) -> None:
        """6人テーブル・プリフロップ全員コール → RFID ボード → ウィナー宣言。

        needs_review=False を保証するシナリオ設計:
          - call_amount=0 の状態で UTG が BET → call_amount=600 に変化
          - 残り5席が CALL: フォールドなしなので active_count=6 を維持
          - street_action_count=1〜6, active_count=6 なので overflow (count>6) は発火しない
          - 各ステップで current_turn_seat が 0.90 スコアを取得し ambiguity 閾値を超える
        """
        # ── セットアップ ──────────────────────────────────────────────────────
        gs = _make_6player_state()
        audio_q = make_audio_queue()
        rfid_q = make_rfid_queue()
        record_q: queue.Queue[ActionRecord] = queue.Queue()
        stop = threading.Event()

        writer = JsonWriter(log_dir=tmp_path, session_id="e2e_test")
        it = IntegrationThread(
            audio_queue=audio_q,
            game_state=gs,
            json_writer=writer,
            rfid_queue=rfid_q,
            on_action=record_q.put,
            stop_event=stop,
        )
        it.start()

        # ── Preflop アクション投入 ───────────────────────────────────────────
        # button_seat=1 → preflop turn_order = [4, 5, 6, 1, 2, 3]
        # 初期 call_amount=0: seat4 が BET で call_amount=600 になる
        audio_q.put(_audio("bet",  600, "ベット600"))   # seat4 (UTG) — BET
        audio_q.put(_audio("call", 600, "コール600"))   # seat5
        audio_q.put(_audio("call", 600, "コール600"))   # seat6
        audio_q.put(_audio("call", 600, "コール600"))   # seat1
        audio_q.put(_audio("call", 600, "コール600"))   # seat2
        audio_q.put(_audio("call", 600, "コール600"))   # seat3

        # 6件の ActionRecord が届くまで待機
        records: list[ActionRecord] = []
        deadline = time.monotonic() + 8.0
        while len(records) < 6 and time.monotonic() < deadline:
            try:
                records.append(record_q.get(timeout=0.2))
            except queue.Empty:
                pass

        # ── RFID ボードカード注入 ────────────────────────────────────────────
        # board_index=1〜3 でフロップ街変更、4でターン、5でリバー
        # winner より前に rfid_q へ注入し、エンジンが次ループで drain するのを待つ
        for idx, card in enumerate(_BOARD_CARDS, start=1):
            rfid_q.put(_board_rfid(card, idx))

        # board_cards が 5 枚揃うまで待機（RFID ドレイン完了を確実に検知）
        # IntegrationThread は self._board_cards に蓄積する（gs.board_cards とは別）
        _wait_until(lambda: len(it._board_cards) == 5)

        # ── ウィナー宣言 ─────────────────────────────────────────────────────
        audio_q.put(_audio("winner", None, f"シート{_WINNER_SEAT} ウィナー"))

        # HandSummary が JSON ファイルに書き込まれるまでポーリング待機
        json_path = tmp_path / "e2e_test.json"
        hand_data: dict | None = None
        deadline = time.monotonic() + 6.0
        while time.monotonic() < deadline:
            if json_path.exists():
                try:
                    raw = json.loads(json_path.read_text(encoding="utf-8"))
                    if raw.get("hands"):
                        hand_data = raw["hands"][0]
                        break
                except (json.JSONDecodeError, OSError):
                    pass
            time.sleep(0.1)

        stop.set()
        it.join(timeout=3)

        # ── アサート ─────────────────────────────────────────────────────────

        # 1. hand_id=1 の ActionRecord が 6 件存在する
        assert len(records) == 6, (
            f"Expected 6 ActionRecords, got {len(records)}. "
            "BET + 5xCALL が処理されませんでした。"
        )
        assert all(r.hand_id == 1 for r in records), (
            f"All records must have hand_id=1; got: {[r.hand_id for r in records]}"
        )

        # 2. button_seat=1 が HandSummary に記録されている
        assert hand_data is not None, (
            "HandSummary が JSON に書き込まれませんでした。"
            "winner イベントが処理されていない可能性があります。"
        )
        assert hand_data["button_seat"] == 1, (
            f"Expected button_seat=1, got {hand_data.get('button_seat')}"
        )

        # 3. needs_review=True のレコードが 0 件
        review_records = [r for r in records if r.needs_review]
        assert len(review_records) == 0, (
            f"{len(review_records)} 件の needs_review=True レコードがあります:\n"
            + "\n".join(
                f"  seat={r.seat} action={r.action} actor_conf={r.actor_confidence:.3f}"
                for r in review_records
            )
        )

        # 4. board_cards = ["As","Ks","Qs","Js","Ts"]
        assert hand_data["board"] == _BOARD_CARDS, (
            f"Expected board={_BOARD_CARDS}, got {hand_data.get('board')}"
        )
