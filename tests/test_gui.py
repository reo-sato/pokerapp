"""tests/test_gui.py

Phase 4: GUIDashboard のロジック部分のテスト（tkinter 不要）。
- _conf_color() の閾値
- on_action() → _update_queue への投入
- _cmd_new_hand / _cmd_winner が audio_queue に正しいイベントを投入する
"""
from __future__ import annotations

import queue
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.events import AudioEvent
from gui.dashboard import _conf_color, _CONF_COLOR_HIGH, _CONF_COLOR_MEDIUM, _CONF_COLOR_LOW


# ――― _conf_color ―――

class TestConfColor:
    def test_high_threshold(self):
        assert _conf_color(0.75) == _CONF_COLOR_HIGH
        assert _conf_color(1.0)  == _CONF_COLOR_HIGH

    def test_medium_threshold(self):
        assert _conf_color(0.5)  == _CONF_COLOR_MEDIUM
        assert _conf_color(0.74) == _CONF_COLOR_MEDIUM

    def test_low_threshold(self):
        assert _conf_color(0.0)  == _CONF_COLOR_LOW
        assert _conf_color(0.49) == _CONF_COLOR_LOW


# ――― GUIDashboard ロジック（tkinter をモックして構築） ―――

def _make_mock_dashboard(tmp_path: Path):
    """customtkinter と tkinter をモックして GUIDashboard を生成する。"""
    from core.game_state import GameStateManager, PlayerState
    from output.json_writer import JsonWriter

    players = [
        PlayerState(seat=1, name="Alice", stack=10000),
        PlayerState(seat=2, name="Bob",   stack=10000),
    ]
    gs = GameStateManager(players=players, sb=100, bb=200)
    gs.new_hand()
    writer = JsonWriter(log_dir=tmp_path, session_id="gui_test")
    audio_q: queue.Queue = queue.Queue()
    stop = threading.Event()

    # customtkinter 全体をモック
    ctk_mock = MagicMock()
    ctk_mock.CTkLabel.return_value = MagicMock()
    ctk_mock.CTkFrame.return_value = MagicMock()
    ctk_mock.CTkScrollableFrame.return_value = MagicMock()
    ctk_mock.CTkTextbox.return_value = MagicMock()
    ctk_mock.CTkButton.return_value = MagicMock()
    ctk_mock.CTkOptionMenu.return_value = MagicMock()
    ctk_mock.CTkEntry.return_value = MagicMock()
    ctk_mock.StringVar.return_value = MagicMock(get=MagicMock(return_value="1"))
    ctk_mock.CTk.return_value = MagicMock()

    with patch.dict("sys.modules", {"customtkinter": ctk_mock}):
        from gui.dashboard import GUIDashboard
        dash = GUIDashboard(
            game_state=gs,
            json_writer=writer,
            audio_queue=audio_q,
            stop_event=stop,
        )

    return dash, gs, audio_q, stop


class TestGUIDashboardLogic:
    def test_on_action_puts_to_update_queue(self, tmp_path: Path):
        from core.hand_log import ActionRecord
        dash, gs, audio_q, stop = _make_mock_dashboard(tmp_path)

        record = ActionRecord(
            hand_id=1, timestamp="2026-04-02T12:00:00",
            street="preflop", seat=1, player_name="Alice",
            action="bet", amount=500, pot_after=500, stack_after=9500,
            source={"camera": False, "audio": True, "rfid": False},
            needs_review=False, confidence=0.5,
        )
        dash.on_action(record)

        assert not dash._update_queue.empty()
        got = dash._update_queue.get_nowait()
        assert got is record

    def test_cmd_new_hand_puts_audio_event(self, tmp_path: Path):
        dash, gs, audio_q, stop = _make_mock_dashboard(tmp_path)
        dash._cmd_new_hand()

        ev = audio_q.get_nowait()
        assert isinstance(ev, AudioEvent)
        assert ev.action == "new_hand"

    def test_cmd_winner_puts_audio_event_with_seat(self, tmp_path: Path):
        dash, gs, audio_q, stop = _make_mock_dashboard(tmp_path)
        # StringVar.get() は "1" を返すようにモック済み
        dash._cmd_winner()

        ev = audio_q.get_nowait()
        assert isinstance(ev, AudioEvent)
        assert ev.action == "winner"
        assert "シート1" in ev.raw_text

    def test_cmd_rebuy_invalid_amount_does_not_crash(self, tmp_path: Path):
        dash, gs, audio_q, stop = _make_mock_dashboard(tmp_path)
        # entry.get() が空文字列を返す場合
        dash._rebuy_amount_entry = MagicMock(get=MagicMock(return_value="abc"))
        dash._rebuy_seat_var = MagicMock(get=MagicMock(return_value="1"))
        dash._append_log = MagicMock()
        dash._cmd_rebuy()
        # クラッシュせず、_append_log が呼ばれる
        dash._append_log.assert_called_once()


# ――― Phase 4-C2: hand finalized → advisory パネル更新 ―――


def _stub_summary(hand_id: int = 1, winner_seat: int = 2, pot_total: int = 300):
    """`HandSummary` の代わりに使う最小 stub (getattr で読まれる field だけ持つ)。"""
    from types import SimpleNamespace
    return SimpleNamespace(
        hand_id=hand_id, winner_seat=winner_seat, pot_total=pot_total,
    )


def _stub_result(
    *,
    needs_review: bool = False,
    reason: str = "reconstructed_no_diff",
    diff=None,
    summary=None,
    bootstrap_source: str = "online_summary",
    bootstrap_meta=None,
    patch_proposal=None,
):
    """`HandReconstructionResult` の duck-typed stub。"""
    from types import SimpleNamespace
    return SimpleNamespace(
        needs_review=needs_review, reason=reason, diff=diff,
        summary=summary if summary is not None else object(),
        bootstrap_source=bootstrap_source, bootstrap_meta=bootstrap_meta,
        patch_proposal=patch_proposal,
    )


class TestOnHandFinalizedQueue:
    def test_on_hand_finalized_puts_to_queue(self, tmp_path: Path):
        dash, _gs, _audio_q, _stop = _make_mock_dashboard(tmp_path)
        dash.on_hand_finalized(42)
        assert dash._hand_finalized_queue.get_nowait() == 42

    def test_on_hand_finalized_coerces_to_int(self, tmp_path: Path):
        dash, _gs, _audio_q, _stop = _make_mock_dashboard(tmp_path)
        dash.on_hand_finalized("7")  # type: ignore[arg-type]
        assert dash._hand_finalized_queue.get_nowait() == 7

    def test_on_hand_finalized_bad_value_does_not_crash(self, tmp_path: Path):
        dash, _gs, _audio_q, _stop = _make_mock_dashboard(tmp_path)
        # int 化できない値が渡されても例外を投げない (= IntegrationThread を壊さない)
        dash.on_hand_finalized("not-an-int")  # type: ignore[arg-type]
        assert dash._hand_finalized_queue.empty()


class TestApplyHandFinalized:
    def test_apply_writes_ok_line_to_history_box(self, tmp_path: Path):
        """OK ケース: integration thread から advisory を引き、_history_box に行追加。"""
        dash, _gs, _audio_q, _stop = _make_mock_dashboard(tmp_path)

        # IntegrationThread accessors を stub
        thread = MagicMock()
        thread.get_last_summary.return_value = _stub_summary(
            hand_id=1, winner_seat=2, pot_total=300,
        )
        thread.get_reconstruction_result.return_value = _stub_result(
            needs_review=False, reason="reconstructed_no_diff",
            bootstrap_source="online_summary",
        )
        dash._integration_thread = thread

        dash._apply_hand_finalized(1)

        # _history_box.insert(...) が呼ばれ、tag="ok" で、テキストに [OK] が含まれる
        thread.get_last_summary.assert_called_once_with(1)
        thread.get_reconstruction_result.assert_called_once_with(1)
        insert_calls = dash._history_box.insert.call_args_list
        assert len(insert_calls) == 1
        args, _kwargs = insert_calls[0]
        # signature: insert("end", text, tag)
        assert args[0] == "end"
        assert "#1" in args[1]
        assert "winner=seat2" in args[1]
        assert "[OK]" in args[1]
        assert "[RAW]" not in args[1]
        assert args[2] == "ok"

        # latest advisory ラベルも更新される
        configure_calls = dash._lbl_latest_advisory.configure.call_args_list
        assert configure_calls
        last_text = configure_calls[-1][1]["text"]
        assert "Latest advisory hand #1" in last_text
        assert "status=ok" in last_text
        assert "reason=reconstructed_no_diff" in last_text
        assert "bootstrap=online_summary" in last_text

    def test_apply_writes_review_line_with_raw_badge(self, tmp_path: Path):
        dash, _gs, _audio_q, _stop = _make_mock_dashboard(tmp_path)
        thread = MagicMock()
        thread.get_last_summary.return_value = _stub_summary(
            hand_id=2, winner_seat=1, pot_total=600,
        )
        # Phase 5-A: patch_proposal もぶら下がっている stub
        from core.patch_proposal import FieldPatch, HandPatchProposal
        proposal = HandPatchProposal(
            hand_id=2, can_patch_automatically=False,
            fields=[
                FieldPatch(field="resolution_type",
                           online="fold_win", offline="showdown"),
                FieldPatch(field="seat_payouts",
                           online={1: 600}, offline={2: 600}),
            ],
        )
        thread.get_reconstruction_result.return_value = _stub_result(
            needs_review=True, reason="reconstructed_with_diff",
            diff={"resolution_type": {}, "seat_payouts": {}},
            bootstrap_source="raw",
            bootstrap_meta={"button_inferred": True},
            patch_proposal=proposal,
        )
        dash._integration_thread = thread

        dash._apply_hand_finalized(2)

        args, _ = dash._history_box.insert.call_args_list[0]
        assert "#2" in args[1]
        assert "[REVIEW]" in args[1]
        assert "[RAW]" in args[1]
        assert "diff=resolution_type,seat_payouts" in args[1]
        assert args[2] == "review"

        last_text = dash._lbl_latest_advisory.configure.call_args_list[-1][1]["text"]
        assert "status=review" in last_text
        assert "diff=resolution_type,seat_payouts" in last_text
        # Phase 5-A: patch_fields が detail label に出る
        assert "patch_fields=resolution_type,seat_payouts" in last_text
        assert "button inferred" in last_text

    def test_apply_omits_patch_fields_when_no_proposal(self, tmp_path: Path):
        """Phase 5-A: patch_proposal が None の hand (OK 等) は patch_fields= を出さない。"""
        dash, _gs, _audio_q, _stop = _make_mock_dashboard(tmp_path)
        thread = MagicMock()
        thread.get_last_summary.return_value = _stub_summary(
            hand_id=1, winner_seat=2, pot_total=300,
        )
        thread.get_reconstruction_result.return_value = _stub_result(
            needs_review=False, reason="reconstructed_no_diff",
            bootstrap_source="online_summary",
            patch_proposal=None,
        )
        dash._integration_thread = thread

        dash._apply_hand_finalized(1)
        last_text = dash._lbl_latest_advisory.configure.call_args_list[-1][1]["text"]
        assert "patch_fields" not in last_text

    def test_apply_writes_skipped_line_when_result_is_none(self, tmp_path: Path):
        dash, _gs, _audio_q, _stop = _make_mock_dashboard(tmp_path)
        thread = MagicMock()
        thread.get_last_summary.return_value = _stub_summary(
            hand_id=3, winner_seat=1, pot_total=300,
        )
        thread.get_reconstruction_result.return_value = None    # 対象 hand 無し
        dash._integration_thread = thread

        dash._apply_hand_finalized(3)

        args, _ = dash._history_box.insert.call_args_list[0]
        assert "[SKIPPED]" in args[1]
        assert "[RAW]" not in args[1]
        assert args[2] == "skipped"

    def test_apply_handles_integration_thread_none(self, tmp_path: Path):
        """thread 未接続でもクラッシュせず、SKIPPED で抜ける (= 例外 propagation なし)。"""
        dash, _gs, _audio_q, _stop = _make_mock_dashboard(tmp_path)
        dash._integration_thread = None
        dash._apply_hand_finalized(99)

        # history box には 1 行 (SKIPPED) が入る
        insert_calls = dash._history_box.insert.call_args_list
        assert len(insert_calls) == 1
        assert insert_calls[0][0][2] == "skipped"
        assert "#99" in insert_calls[0][0][1]
        assert "[SKIPPED]" in insert_calls[0][0][1]

    def test_apply_handles_accessor_exception(self, tmp_path: Path):
        """accessor が例外を投げても GUI 側は SKIPPED で抜ける。"""
        dash, _gs, _audio_q, _stop = _make_mock_dashboard(tmp_path)
        thread = MagicMock()
        thread.get_last_summary.side_effect = RuntimeError("boom")
        thread.get_reconstruction_result.side_effect = RuntimeError("boom")
        dash._integration_thread = thread

        # 例外で _apply_hand_finalized が落ちないこと
        dash._apply_hand_finalized(5)
        args, _ = dash._history_box.insert.call_args_list[0]
        assert "[SKIPPED]" in args[1]
        assert args[2] == "skipped"
