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

    def test_cmd_manual_action_pushes_to_integration_manual_queue(
        self, tmp_path: Path,
    ):
        """Phase 5-I: 手動アクションは ``ManualActionEvent`` に変換されて
        ``integration_thread.manual_queue`` に push される (= gs を直叩きしない)。
        """
        from core.events import ManualActionEvent
        dash, gs, audio_q, _stop = _make_mock_dashboard(tmp_path)
        # integration thread を mock し、manual_queue を捕捉
        thread = MagicMock()
        manual_q = MagicMock()
        thread.manual_queue = manual_q
        dash._integration_thread = thread

        # 入力値を mock (mock dashboard は seat 1/2 のみなので seat=1 を使う)
        dash._manual_seat_var = MagicMock(get=MagicMock(return_value="1"))
        dash._manual_action_var = MagicMock(get=MagicMock(return_value="raise"))
        dash._manual_amount_entry = MagicMock(
            get=MagicMock(return_value="600"),
            delete=MagicMock(),
        )

        # gs の直叩きが起きないことを担保。raise=600 を直叩きで apply されると
        # pot に 600 入るはずだが、新実装ではそれが起きない。
        pot_before = gs.pot
        stack_before = gs.get_stack(1)

        dash._cmd_manual_action()

        # gs.apply_action は呼ばれていない (= pot / stack は不変のまま)
        assert gs.pot == pot_before
        assert gs.get_stack(1) == stack_before
        # manual_queue に ManualActionEvent が put された
        manual_q.put.assert_called_once()
        pushed = manual_q.put.call_args[0][0]
        assert isinstance(pushed, ManualActionEvent)
        assert pushed.seat == 1
        assert pushed.action == "raise"
        assert pushed.amount == 600
        # amount entry がクリアされる
        dash._manual_amount_entry.delete.assert_called_once_with(0, "end")

    def test_cmd_manual_action_no_integration_thread_logs_warning(
        self, tmp_path: Path,
    ):
        """integration thread 未接続なら warning ログ + queue へ push しない。"""
        dash, _gs, _audio_q, _stop = _make_mock_dashboard(tmp_path)
        dash._integration_thread = None
        dash._manual_seat_var = MagicMock(get=MagicMock(return_value="1"))
        dash._manual_action_var = MagicMock(get=MagicMock(return_value="fold"))
        dash._manual_amount_entry = MagicMock(get=MagicMock(return_value=""))
        dash._append_log = MagicMock()

        dash._cmd_manual_action()

        # warning log が出る
        msg = dash._append_log.call_args_list[-1][0][0]
        assert "integration thread" in msg.lower() or "not connected" in msg.lower()

    def test_cmd_manual_action_invalid_seat_logs_warning(self, tmp_path: Path):
        """席番号が不正な文字列なら ValueError は出さず warning ログ。"""
        dash, _gs, _audio_q, _stop = _make_mock_dashboard(tmp_path)
        thread = MagicMock()
        thread.manual_queue = MagicMock()
        dash._integration_thread = thread
        dash._manual_seat_var = MagicMock(get=MagicMock(return_value="not-an-int"))
        dash._manual_action_var = MagicMock(get=MagicMock(return_value="fold"))
        dash._manual_amount_entry = MagicMock(get=MagicMock(return_value=""))
        dash._append_log = MagicMock()

        dash._cmd_manual_action()

        thread.manual_queue.put.assert_not_called()
        msg = dash._append_log.call_args_list[-1][0][0]
        assert "席" in msg

    def test_cmd_manual_action_invalid_amount_logs_warning(self, tmp_path: Path):
        """金額が不正な文字列なら ValueError は出さず warning ログ。"""
        dash, _gs, _audio_q, _stop = _make_mock_dashboard(tmp_path)
        thread = MagicMock()
        thread.manual_queue = MagicMock()
        dash._integration_thread = thread
        dash._manual_seat_var = MagicMock(get=MagicMock(return_value="1"))
        dash._manual_action_var = MagicMock(get=MagicMock(return_value="bet"))
        dash._manual_amount_entry = MagicMock(get=MagicMock(return_value="abc"))
        dash._append_log = MagicMock()

        dash._cmd_manual_action()

        thread.manual_queue.put.assert_not_called()
        msg = dash._append_log.call_args_list[-1][0][0]
        assert "金額" in msg

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


def _stub_summary(
    hand_id: int = 1,
    winner_seat: int = 2,
    pot_total: int = 300,
    blinds=None,
):
    """`HandSummary` の代わりに使う最小 stub (getattr で読まれる field だけ持つ)。

    Phase 5-E: ``blinds`` (dict 形式: ``{"sb": ..., "bb": ...}``) を任意で渡せる。
    default は ``{}`` (= 既存 Phase 4-C2 / 5-A / 5-C 系テストでは blinds 属性は
    あるが空、``_format_blind_for_advisory`` は ``?/?`` にフォールバック)。
    """
    from types import SimpleNamespace
    return SimpleNamespace(
        hand_id=hand_id, winner_seat=winner_seat, pot_total=pot_total,
        blinds=blinds if blinds is not None else {},
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
    patch_applied: bool = False,
    applied_fields=None,
):
    """`HandReconstructionResult` の duck-typed stub。

    Phase 5-G: ``patch_applied`` / ``applied_fields`` を任意で渡せる。default は
    False / 空 list なので、既存テストでは無視される動作と等価。
    """
    from types import SimpleNamespace
    return SimpleNamespace(
        needs_review=needs_review, reason=reason, diff=diff,
        summary=summary if summary is not None else object(),
        bootstrap_source=bootstrap_source, bootstrap_meta=bootstrap_meta,
        patch_proposal=patch_proposal,
        patch_applied=patch_applied,
        applied_fields=applied_fields if applied_fields is not None else [],
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

    def test_phase5c_cmd_update_blinds_calls_integration_thread(
        self, tmp_path: Path,
    ):
        """Phase 5-C: _cmd_update_blinds が IntegrationThread.update_blinds(sb, bb)
        を呼ぶ (= GUI → integration → reconstructor の経路)。
        """
        dash, _gs, _audio_q, _stop = _make_mock_dashboard(tmp_path)
        thread = MagicMock()
        dash._integration_thread = thread

        # 入力欄を mock し、有効な値を返す
        dash._blinds_sb_entry = MagicMock(
            get=MagicMock(return_value="300"),
            delete=MagicMock(),
            configure=MagicMock(),
        )
        dash._blinds_bb_entry = MagicMock(
            get=MagicMock(return_value="600"),
            delete=MagicMock(),
            configure=MagicMock(),
        )
        dash._append_log = MagicMock()

        dash._cmd_update_blinds()

        thread.update_blinds.assert_called_once_with(300, 600)
        # success 時はログに blinds 更新メッセージが出る
        called_text = dash._append_log.call_args_list[-1][0][0]
        assert "Blinds 更新" in called_text
        assert "300" in called_text
        assert "600" in called_text

    def test_phase5c_cmd_update_blinds_rejects_invalid(self, tmp_path: Path):
        """Phase 5-C: 不正値 (sb >= bb / 負値 / 非数値) は IntegrationThread を呼ばず
        ログにエラーを出す。"""
        dash, _gs, _audio_q, _stop = _make_mock_dashboard(tmp_path)
        thread = MagicMock()
        dash._integration_thread = thread
        dash._append_log = MagicMock()

        # ケース 1: 非数値
        dash._blinds_sb_entry = MagicMock(get=MagicMock(return_value="abc"))
        dash._blinds_bb_entry = MagicMock(get=MagicMock(return_value="200"))
        dash._cmd_update_blinds()
        thread.update_blinds.assert_not_called()

        # ケース 2: 負値
        dash._blinds_sb_entry = MagicMock(get=MagicMock(return_value="-5"))
        dash._blinds_bb_entry = MagicMock(get=MagicMock(return_value="200"))
        dash._cmd_update_blinds()
        thread.update_blinds.assert_not_called()

        # ケース 3: SB >= BB
        dash._blinds_sb_entry = MagicMock(get=MagicMock(return_value="400"))
        dash._blinds_bb_entry = MagicMock(get=MagicMock(return_value="200"))
        dash._cmd_update_blinds()
        thread.update_blinds.assert_not_called()

    def test_phase5c_cmd_update_blinds_no_thread_logs_warning(
        self, tmp_path: Path,
    ):
        """Integration thread 未接続なら更新せず警告ログ。"""
        dash, _gs, _audio_q, _stop = _make_mock_dashboard(tmp_path)
        dash._integration_thread = None
        dash._append_log = MagicMock()

        dash._blinds_sb_entry = MagicMock(get=MagicMock(return_value="300"))
        dash._blinds_bb_entry = MagicMock(get=MagicMock(return_value="600"))

        dash._cmd_update_blinds()
        # warning ログが残る
        msg = dash._append_log.call_args_list[-1][0][0]
        assert "Integration thread 未接続" in msg

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


# ――― Phase 5-E: blind 関連 advisory 表示 ―――


class TestPhase5EBlindAdvisoryDisplay:
    """Phase 5-E: Latest advisory ラベル と history 行に blind 情報を表示する。"""

    def test_advisory_shows_blinds_with_current_state_source(
        self, tmp_path: Path,
    ) -> None:
        """summary.blinds + bootstrap_meta.blind_source=current_state →
        Latest advisory に ``blinds=200/400`` と ``source=current_state`` が含まれる。
        mismatch なしなので ``blind_mismatch=yes`` は出ない。
        """
        dash, _gs, _audio_q, _stop = _make_mock_dashboard(tmp_path)
        thread = MagicMock()
        thread.get_last_summary.return_value = _stub_summary(
            hand_id=1, winner_seat=2, pot_total=600,
            blinds={"sb": 200, "bb": 400},
        )
        thread.get_reconstruction_result.return_value = _stub_result(
            needs_review=False, reason="reconstructed_no_diff",
            bootstrap_source="raw",
            bootstrap_meta={"blind_source": "current_state",
                            "sb_amount": 200, "bb_amount": 400},
        )
        dash._integration_thread = thread

        dash._apply_hand_finalized(1)
        last_text = dash._lbl_latest_advisory.configure.call_args_list[-1][1]["text"]
        assert "blinds=200/400" in last_text
        assert "source=current_state" in last_text
        # mismatch なしなので blind_mismatch=yes は出ない (ノイズ削減)
        assert "blind_mismatch" not in last_text

    def test_advisory_shows_blinds_with_session_default_source(
        self, tmp_path: Path,
    ) -> None:
        """blind_source=session_default のときも Latest advisory には表示する。"""
        dash, _gs, _audio_q, _stop = _make_mock_dashboard(tmp_path)
        thread = MagicMock()
        thread.get_last_summary.return_value = _stub_summary(
            hand_id=1, winner_seat=2, pot_total=300,
            blinds={"sb": 100, "bb": 200},
        )
        thread.get_reconstruction_result.return_value = _stub_result(
            needs_review=False, reason="reconstructed_no_diff",
            bootstrap_source="raw",
            bootstrap_meta={"blind_source": "session_default",
                            "sb_amount": 100, "bb_amount": 200},
        )
        dash._integration_thread = thread

        dash._apply_hand_finalized(1)
        last_text = dash._lbl_latest_advisory.configure.call_args_list[-1][1]["text"]
        assert "blinds=100/200" in last_text
        assert "source=session_default" in last_text

    def test_advisory_shows_blind_mismatch_when_patch_has_blinds_field(
        self, tmp_path: Path,
    ) -> None:
        """patch_proposal.fields に field='blinds' があれば
        ``blind_mismatch=yes`` が Latest advisory に追加される。
        """
        from core.patch_proposal import FieldPatch, HandPatchProposal
        proposal = HandPatchProposal(
            hand_id=2, can_patch_automatically=False,
            fields=[
                FieldPatch(
                    field="blinds",
                    online={"sb": 200, "bb": 400},
                    offline={"sb": 100, "bb": 200},
                    note="blind amounts differ",
                ),
            ],
            summary_note="blind mismatch: blind_amount_mismatch",
        )
        dash, _gs, _audio_q, _stop = _make_mock_dashboard(tmp_path)
        thread = MagicMock()
        thread.get_last_summary.return_value = _stub_summary(
            hand_id=2, winner_seat=2, pot_total=600,
            blinds={"sb": 200, "bb": 400},
        )
        thread.get_reconstruction_result.return_value = _stub_result(
            needs_review=True, reason="reconstructed_with_blind_mismatch",
            bootstrap_source="raw",
            bootstrap_meta={"blind_source": "session_default",
                            "sb_amount": 100, "bb_amount": 200},
            patch_proposal=proposal,
        )
        dash._integration_thread = thread

        dash._apply_hand_finalized(2)
        last_text = dash._lbl_latest_advisory.configure.call_args_list[-1][1]["text"]
        # blind_mismatch=yes が出る
        assert "blind_mismatch=yes" in last_text
        # patch_fields に blinds が含まれる (Phase 5-A の既存表示)
        assert "patch_fields=blinds" in last_text

    def test_advisory_degrades_with_missing_blinds_attr(
        self, tmp_path: Path,
    ) -> None:
        """summary に blinds なし / bootstrap_meta に blind_source なしでも
        例外を投げず ``blinds=?/? (source=unknown)`` で degrade する。
        """
        dash, _gs, _audio_q, _stop = _make_mock_dashboard(tmp_path)
        thread = MagicMock()
        # blinds 引数を渡さない → default の空 dict (sb/bb 不在 → "?")
        thread.get_last_summary.return_value = _stub_summary(hand_id=9)
        thread.get_reconstruction_result.return_value = _stub_result(
            needs_review=False, reason="reconstructed_no_diff",
            bootstrap_source="online_summary",
            bootstrap_meta=None,    # meta 自体が None
        )
        dash._integration_thread = thread

        # 例外を投げない
        dash._apply_hand_finalized(9)
        last_text = dash._lbl_latest_advisory.configure.call_args_list[-1][1]["text"]
        assert "blinds=?/?" in last_text
        assert "source=unknown" in last_text

    def test_advisory_unknown_when_integration_thread_none(
        self, tmp_path: Path,
    ) -> None:
        """_integration_thread が None でも blind segment は ``?/?`` で出力される
        (skipped status の hand でも degradation 動作する)。
        """
        dash, _gs, _audio_q, _stop = _make_mock_dashboard(tmp_path)
        dash._integration_thread = None

        dash._apply_hand_finalized(1)
        last_text = dash._lbl_latest_advisory.configure.call_args_list[-1][1]["text"]
        assert "blinds=?/?" in last_text
        assert "source=unknown" in last_text

    def test_history_suffix_blinds_shown_only_for_current_state(
        self, tmp_path: Path,
    ) -> None:
        """history 行 suffix: blind_source=current_state のときだけ
        ``blinds=SB/BB (current_state)`` が末尾に付く。
        """
        dash, _gs, _audio_q, _stop = _make_mock_dashboard(tmp_path)
        thread = MagicMock()
        thread.get_last_summary.return_value = _stub_summary(
            hand_id=1, winner_seat=2, pot_total=600,
            blinds={"sb": 200, "bb": 400},
        )
        thread.get_reconstruction_result.return_value = _stub_result(
            needs_review=False, reason="reconstructed_no_diff",
            bootstrap_source="raw",
            bootstrap_meta={"blind_source": "current_state",
                            "sb_amount": 200, "bb_amount": 400},
        )
        dash._integration_thread = thread

        dash._apply_hand_finalized(1)
        history_text = dash._history_box.insert.call_args_list[0][0][1]
        assert "blinds=200/400 (current_state)" in history_text

    def test_history_suffix_session_default_omits_blinds(
        self, tmp_path: Path,
    ) -> None:
        """history 行: blind_source=session_default では blinds suffix を **省略**
        (ノイズ削減方針: キャッシュゲームでデフォルト状態は通常運用なので)。
        Latest advisory には依然出る (上のテスト参照)。
        """
        dash, _gs, _audio_q, _stop = _make_mock_dashboard(tmp_path)
        thread = MagicMock()
        thread.get_last_summary.return_value = _stub_summary(
            hand_id=1, winner_seat=2, pot_total=300,
            blinds={"sb": 100, "bb": 200},
        )
        thread.get_reconstruction_result.return_value = _stub_result(
            needs_review=False, reason="reconstructed_no_diff",
            bootstrap_source="raw",
            bootstrap_meta={"blind_source": "session_default",
                            "sb_amount": 100, "bb_amount": 200},
        )
        dash._integration_thread = thread

        dash._apply_hand_finalized(1)
        history_text = dash._history_box.insert.call_args_list[0][0][1]
        # session_default の場合 history 行に blinds= は出さない
        assert "blinds=" not in history_text
        assert "(session_default)" not in history_text

    def test_history_suffix_blind_mismatch_marker(
        self, tmp_path: Path,
    ) -> None:
        """history 行: blind FieldPatch があれば ``(blind_mismatch)`` を末尾に付ける。
        この情報だけは current_state でなくても表示する (= operator が一覧で
        review 必要 hand を見分けるため)。
        """
        from core.patch_proposal import FieldPatch, HandPatchProposal
        proposal = HandPatchProposal(
            hand_id=2, can_patch_automatically=False,
            fields=[
                FieldPatch(field="blinds",
                           online={"sb": 200, "bb": 400},
                           offline={"sb": 100, "bb": 200}),
            ],
            summary_note="blind mismatch",
        )
        dash, _gs, _audio_q, _stop = _make_mock_dashboard(tmp_path)
        thread = MagicMock()
        thread.get_last_summary.return_value = _stub_summary(
            hand_id=2, winner_seat=2, pot_total=600,
            blinds={"sb": 200, "bb": 400},
        )
        thread.get_reconstruction_result.return_value = _stub_result(
            needs_review=True, reason="reconstructed_with_blind_mismatch",
            bootstrap_source="raw",
            bootstrap_meta={"blind_source": "session_default",
                            "sb_amount": 100, "bb_amount": 200},
            patch_proposal=proposal,
        )
        dash._integration_thread = thread

        dash._apply_hand_finalized(2)
        history_text = dash._history_box.insert.call_args_list[0][0][1]
        assert "(blind_mismatch)" in history_text

    def test_blind_helpers_handle_dict_proposal(self, tmp_path: Path) -> None:
        """JSONL から読まれた dict 形 patch_proposal でも _has_blind_patch が動く
        (= summarize_reconstruction の duck-typed pattern と同じ寛容性)。
        """
        dash, _gs, _audio_q, _stop = _make_mock_dashboard(tmp_path)
        dict_proposal = {
            "hand_id": 3, "can_patch_automatically": False,
            "fields": [
                {"field": "blinds",
                 "online": {"sb": 200, "bb": 400},
                 "offline": {"sb": 100, "bb": 200}},
            ],
            "summary_note": "blind mismatch",
        }
        # dict 形 proposal を直接渡す
        from types import SimpleNamespace
        result = SimpleNamespace(
            needs_review=True, reason="reconstructed_with_blind_mismatch",
            diff=None, summary=object(),
            bootstrap_source="raw",
            bootstrap_meta={"blind_source": "session_default"},
            patch_proposal=dict_proposal,
        )
        # helper 直接テスト
        assert dash._has_blind_patch(result) is True

    def test_blind_source_text_normalizes_unknown_values(
        self, tmp_path: Path,
    ) -> None:
        """``_blind_source_text`` は known 値以外を ``unknown`` に正規化する。"""
        dash, _gs, _audio_q, _stop = _make_mock_dashboard(tmp_path)
        from types import SimpleNamespace
        # 既知値
        r1 = SimpleNamespace(bootstrap_meta={"blind_source": "current_state"})
        assert dash._blind_source_text(r1) == "current_state"
        r2 = SimpleNamespace(bootstrap_meta={"blind_source": "session_default"})
        assert dash._blind_source_text(r2) == "session_default"
        # 不明値
        r3 = SimpleNamespace(bootstrap_meta={"blind_source": "weird_value"})
        assert dash._blind_source_text(r3) == "unknown"
        # meta なし
        r4 = SimpleNamespace(bootstrap_meta=None)
        assert dash._blind_source_text(r4) == "unknown"
        # result 自体 None
        assert dash._blind_source_text(None) == "unknown"


# ――― Phase 5-G: Apply patch ボタン + patch_applied 表示 ―――


class TestPhase5GApplyPatchCommand:
    """Phase 5-G: ``_cmd_apply_patch`` の動作。"""

    def _make_proposal_with_whitelist_field(self):
        from core.patch_proposal import FieldPatch, HandPatchProposal
        return HandPatchProposal(
            hand_id=1, can_patch_automatically=False,
            fields=[FieldPatch(field="resolution_type",
                                online="fold_win", offline="showdown")],
        )

    def test_confirmation_yes_calls_apply(self, tmp_path: Path):
        """確認ダイアログで OK → IntegrationThread.apply_patch_proposal を呼ぶ。"""
        dash, _gs, _audio_q, _stop = _make_mock_dashboard(tmp_path)
        thread = MagicMock()
        thread.get_reconstruction_result.return_value = _stub_result(
            patch_proposal=self._make_proposal_with_whitelist_field(),
        )
        thread.apply_patch_proposal.return_value = True
        dash._integration_thread = thread
        dash._latest_advisory_hand_id = 1
        dash._append_log = MagicMock()
        # 確認ダイアログ hook を override (True を返す = OK)
        dash._ask_apply_patch_confirmation = MagicMock(return_value=True)

        dash._cmd_apply_patch()

        dash._ask_apply_patch_confirmation.assert_called_once()
        thread.apply_patch_proposal.assert_called_once_with(1)
        # success log
        msgs = [c[0][0] for c in dash._append_log.call_args_list]
        assert any("Applied patch to hand #1" in m for m in msgs)

    def test_confirmation_no_does_not_apply(self, tmp_path: Path):
        """確認ダイアログで NO → apply_patch_proposal は呼ばれない。"""
        dash, _gs, _audio_q, _stop = _make_mock_dashboard(tmp_path)
        thread = MagicMock()
        thread.get_reconstruction_result.return_value = _stub_result(
            patch_proposal=self._make_proposal_with_whitelist_field(),
        )
        dash._integration_thread = thread
        dash._latest_advisory_hand_id = 1
        dash._append_log = MagicMock()
        # 確認ダイアログ hook を override (False を返す = キャンセル)
        dash._ask_apply_patch_confirmation = MagicMock(return_value=False)

        dash._cmd_apply_patch()

        thread.apply_patch_proposal.assert_not_called()
        # cancel log
        msgs = [c[0][0] for c in dash._append_log.call_args_list]
        assert any("キャンセル" in m for m in msgs)

    def test_no_latest_advisory_warns(self, tmp_path: Path):
        """_latest_advisory_hand_id=None なら警告ログ + apply 呼ばない。"""
        dash, _gs, _audio_q, _stop = _make_mock_dashboard(tmp_path)
        thread = MagicMock()
        dash._integration_thread = thread
        dash._latest_advisory_hand_id = None
        dash._append_log = MagicMock()

        dash._cmd_apply_patch()

        thread.apply_patch_proposal.assert_not_called()
        msg = dash._append_log.call_args_list[-1][0][0]
        assert "適用対象" in msg

    def test_no_integration_thread_warns(self, tmp_path: Path):
        """_integration_thread=None なら警告ログ + apply 呼ばない。"""
        dash, _gs, _audio_q, _stop = _make_mock_dashboard(tmp_path)
        dash._integration_thread = None
        dash._latest_advisory_hand_id = 1
        dash._append_log = MagicMock()

        dash._cmd_apply_patch()

        msg = dash._append_log.call_args_list[-1][0][0]
        assert "integration thread" in msg

    def test_no_proposal_warns(self, tmp_path: Path):
        """対象 hand の result.patch_proposal が None なら警告 + apply しない。"""
        dash, _gs, _audio_q, _stop = _make_mock_dashboard(tmp_path)
        thread = MagicMock()
        thread.get_reconstruction_result.return_value = _stub_result(
            patch_proposal=None,
        )
        dash._integration_thread = thread
        dash._latest_advisory_hand_id = 1
        dash._append_log = MagicMock()

        dash._cmd_apply_patch()

        thread.apply_patch_proposal.assert_not_called()
        msg = dash._append_log.call_args_list[-1][0][0]
        assert "patch proposal" in msg

    def test_apply_returns_false_warns(self, tmp_path: Path):
        """confirmation OK でも apply が False を返したら警告ログ (= whitelist field
        が無いケース)。"""
        dash, _gs, _audio_q, _stop = _make_mock_dashboard(tmp_path)
        thread = MagicMock()
        thread.get_reconstruction_result.return_value = _stub_result(
            patch_proposal=self._make_proposal_with_whitelist_field(),
        )
        thread.apply_patch_proposal.return_value = False
        dash._integration_thread = thread
        dash._latest_advisory_hand_id = 1
        dash._append_log = MagicMock()
        dash._ask_apply_patch_confirmation = MagicMock(return_value=True)

        dash._cmd_apply_patch()

        msgs = [c[0][0] for c in dash._append_log.call_args_list]
        # 失敗ログ
        assert any("適用可能" in m and "whitelist" in m for m in msgs)

    def test_apply_exception_does_not_crash(self, tmp_path: Path):
        """apply で例外 → ログに warning だが GUI はクラッシュしない。"""
        dash, _gs, _audio_q, _stop = _make_mock_dashboard(tmp_path)
        thread = MagicMock()
        thread.get_reconstruction_result.return_value = _stub_result(
            patch_proposal=self._make_proposal_with_whitelist_field(),
        )
        thread.apply_patch_proposal.side_effect = RuntimeError("boom")
        dash._integration_thread = thread
        dash._latest_advisory_hand_id = 1
        dash._append_log = MagicMock()
        dash._ask_apply_patch_confirmation = MagicMock(return_value=True)

        dash._cmd_apply_patch()
        # クラッシュせず、warning が出る
        msg = dash._append_log.call_args_list[-1][0][0]
        assert "Apply patch failed" in msg

    def test_messagebox_unavailable_aborts_safely(self, tmp_path: Path):
        """確認ダイアログ hook が None を返す (= dialog 表示不可) 場合は apply しない。"""
        dash, _gs, _audio_q, _stop = _make_mock_dashboard(tmp_path)
        thread = MagicMock()
        thread.get_reconstruction_result.return_value = _stub_result(
            patch_proposal=self._make_proposal_with_whitelist_field(),
        )
        dash._integration_thread = thread
        dash._latest_advisory_hand_id = 1
        dash._append_log = MagicMock()
        # dialog 不可シミュレーション: None を返す
        dash._ask_apply_patch_confirmation = MagicMock(return_value=None)

        dash._cmd_apply_patch()

        # apply は呼ばれない (safe default)
        thread.apply_patch_proposal.assert_not_called()
        msg = dash._append_log.call_args_list[-1][0][0]
        assert "確認ダイアログ" in msg

    def test_ask_confirmation_returns_none_when_tkinter_missing(
        self, tmp_path: Path,
    ):
        """``_ask_apply_patch_confirmation`` 本体: tkinter が import できない
        環境では ``None`` を返す (= safe default、apply を中止する印)。
        """
        dash, _gs, _audio_q, _stop = _make_mock_dashboard(tmp_path)
        # test 環境では tkinter が無いか、display が無いので None になる
        ret = dash._ask_apply_patch_confirmation("title", "msg")
        # 戻り値は None (dialog 表示不可) or False (確認 NO 想定) のいずれか。
        # True (= 自動的に OK) になっていないことが安全側の証拠。
        assert ret is not True


class TestPhase5HPatchDetailView:
    """Phase 5-H: ``_cmd_show_patch_details`` と ``_create_patch_detail_window``
    の連携。Toplevel 実体は MagicMock で差し替える。"""

    def _make_proposal(self):
        from core.patch_proposal import FieldPatch, HandPatchProposal
        return HandPatchProposal(
            hand_id=1, can_patch_automatically=False,
            fields=[
                FieldPatch(field="resolution_type",
                            online="fold_win", offline="showdown",
                            note="resolution_type differs"),
                FieldPatch(field="winner_seat", online=1, offline=2),
                FieldPatch(field="seat_payouts",
                            online={1: 300}, offline={2: 600}),
            ],
        )

    def test_no_latest_advisory_warns(self, tmp_path: Path) -> None:
        """``_latest_advisory_hand_id=None`` → warning ログ + window 生成しない。"""
        dash, _gs, _audio_q, _stop = _make_mock_dashboard(tmp_path)
        thread = MagicMock()
        dash._integration_thread = thread
        dash._latest_advisory_hand_id = None
        dash._append_log = MagicMock()
        dash._create_patch_detail_window = MagicMock()

        dash._cmd_show_patch_details()

        dash._create_patch_detail_window.assert_not_called()
        msg = dash._append_log.call_args_list[-1][0][0]
        assert "表示できる" in msg or "advisory hand" in msg

    def test_no_integration_thread_warns(self, tmp_path: Path) -> None:
        dash, _gs, _audio_q, _stop = _make_mock_dashboard(tmp_path)
        dash._integration_thread = None
        dash._latest_advisory_hand_id = 1
        dash._append_log = MagicMock()
        dash._create_patch_detail_window = MagicMock()

        dash._cmd_show_patch_details()

        dash._create_patch_detail_window.assert_not_called()
        msg = dash._append_log.call_args_list[-1][0][0]
        assert "integration thread" in msg

    def test_no_proposal_warns(self, tmp_path: Path) -> None:
        """result.patch_proposal が None なら window 生成しない。"""
        dash, _gs, _audio_q, _stop = _make_mock_dashboard(tmp_path)
        thread = MagicMock()
        thread.get_reconstruction_result.return_value = _stub_result(
            patch_proposal=None,
        )
        dash._integration_thread = thread
        dash._latest_advisory_hand_id = 1
        dash._append_log = MagicMock()
        dash._create_patch_detail_window = MagicMock()

        dash._cmd_show_patch_details()

        dash._create_patch_detail_window.assert_not_called()
        msg = dash._append_log.call_args_list[-1][0][0]
        assert "patch proposal" in msg

    def test_show_details_calls_window_creator_with_lines(
        self, tmp_path: Path,
    ) -> None:
        """正常系: ``_create_patch_detail_window`` が title + lines list で呼ばれる。"""
        dash, _gs, _audio_q, _stop = _make_mock_dashboard(tmp_path)
        thread = MagicMock()
        thread.get_last_summary.return_value = _stub_summary(hand_id=1)
        thread.get_reconstruction_result.return_value = _stub_result(
            patch_proposal=self._make_proposal(),
        )
        dash._integration_thread = thread
        dash._latest_advisory_hand_id = 1
        dash._create_patch_detail_window = MagicMock()

        dash._cmd_show_patch_details()

        dash._create_patch_detail_window.assert_called_once()
        args, _kwargs = dash._create_patch_detail_window.call_args
        title, lines = args
        assert "hand #1" in title.lower() or "#1" in title
        # lines は list[str] で、各 field のヘッダー行を含む
        assert isinstance(lines, list)
        combined = "\n".join(lines)
        # whitelist field: applies マーカー
        assert "[applies] resolution_type" in combined
        assert "[applies] seat_payouts" in combined
        # 非 whitelist: skip マーカー
        assert "[skip]" in combined
        assert "winner_seat" in combined
        # online / offline 値が表示される
        assert "fold_win" in combined
        assert "showdown" in combined
        # note も表示される
        assert "differs" in combined

    def test_show_details_calls_summarize_once(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """``summarize_patch_proposal_for_view`` が 1 回だけ呼ばれる
        (= unintended な double-call が無い)。"""
        from core import patch_apply as patch_apply_mod
        from core.patch_apply import FieldDiffView

        dash, _gs, _audio_q, _stop = _make_mock_dashboard(tmp_path)
        thread = MagicMock()
        thread.get_last_summary.return_value = _stub_summary()
        thread.get_reconstruction_result.return_value = _stub_result(
            patch_proposal=self._make_proposal(),
        )
        dash._integration_thread = thread
        dash._latest_advisory_hand_id = 1
        dash._create_patch_detail_window = MagicMock()

        sentinel = [
            FieldDiffView(field="resolution_type", online_repr="a",
                          offline_repr="b", is_applicable=True),
        ]
        spy = MagicMock(return_value=sentinel)
        monkeypatch.setattr(patch_apply_mod, "summarize_patch_proposal_for_view", spy)

        dash._cmd_show_patch_details()

        assert spy.call_count == 1

    def test_empty_views_still_opens_window_with_placeholder(
        self, tmp_path: Path,
    ) -> None:
        """proposal はあるが view が空 (= 全 entry が malformed) → "差分なし"
        メッセージを乗せた window を生成する。"""
        from core.patch_proposal import HandPatchProposal

        dash, _gs, _audio_q, _stop = _make_mock_dashboard(tmp_path)
        thread = MagicMock()
        thread.get_last_summary.return_value = _stub_summary()
        # fields 空 → summarize は [] を返す
        empty_proposal = HandPatchProposal(
            hand_id=1, can_patch_automatically=False, fields=[],
        )
        thread.get_reconstruction_result.return_value = _stub_result(
            patch_proposal=empty_proposal,
        )
        dash._integration_thread = thread
        dash._latest_advisory_hand_id = 1
        dash._create_patch_detail_window = MagicMock()

        dash._cmd_show_patch_details()

        dash._create_patch_detail_window.assert_called_once()
        args, _kwargs = dash._create_patch_detail_window.call_args
        title, lines = args
        # placeholder 行: "表示可能な diff がありません" 等
        combined = "\n".join(lines)
        assert "diff" in combined.lower() or "差分" in combined or "ありません" in combined

    def test_show_details_does_not_call_apply(self, tmp_path: Path) -> None:
        """Show details は Apply と独立: ``apply_patch_proposal`` は呼ばれない。"""
        dash, _gs, _audio_q, _stop = _make_mock_dashboard(tmp_path)
        thread = MagicMock()
        thread.get_last_summary.return_value = _stub_summary()
        thread.get_reconstruction_result.return_value = _stub_result(
            patch_proposal=self._make_proposal(),
        )
        dash._integration_thread = thread
        dash._latest_advisory_hand_id = 1
        dash._create_patch_detail_window = MagicMock()

        dash._cmd_show_patch_details()

        # Show details は read-only (= apply は呼ばない)
        thread.apply_patch_proposal.assert_not_called()

    def test_show_details_after_apply_includes_patch_applied_header(
        self, tmp_path: Path,
    ) -> None:
        """apply 済み hand (result.patch_applied=True) で detail を開くと、
        header に ``(patch applied — applied_fields=...)`` が付く。
        """
        dash, _gs, _audio_q, _stop = _make_mock_dashboard(tmp_path)
        thread = MagicMock()
        thread.get_last_summary.return_value = _stub_summary()
        thread.get_reconstruction_result.return_value = _stub_result(
            patch_proposal=self._make_proposal(),
            patch_applied=True,
            applied_fields=["resolution_type", "seat_payouts"],
        )
        dash._integration_thread = thread
        dash._latest_advisory_hand_id = 1
        dash._create_patch_detail_window = MagicMock()

        dash._cmd_show_patch_details()

        args, _kwargs = dash._create_patch_detail_window.call_args
        _title, lines = args
        # 最初の行 (= header) に patch applied マーカーが含まれる
        header = lines[0]
        assert "patch applied" in header
        assert "resolution_type" in header
        assert "seat_payouts" in header


class TestPhase5GAdvisoryLabelDisplay:
    """Phase 5-G: ``patch_applied`` / ``applied_fields`` が Latest advisory に表示。"""

    def test_label_shows_patch_applied_yes(self, tmp_path: Path):
        """result.patch_applied=True + applied_fields=[...] → label に
        ``patch_applied=yes`` と ``applied_fields=...`` が含まれる。
        """
        dash, _gs, _audio_q, _stop = _make_mock_dashboard(tmp_path)
        thread = MagicMock()
        thread.get_last_summary.return_value = _stub_summary(
            hand_id=1, winner_seat=2, pot_total=600,
        )
        thread.get_reconstruction_result.return_value = _stub_result(
            needs_review=True, reason="reconstructed_with_diff",
            bootstrap_source="raw",
            patch_applied=True,
            applied_fields=["resolution_type", "seat_payouts"],
        )
        dash._integration_thread = thread

        dash._apply_hand_finalized(1)
        last_text = dash._lbl_latest_advisory.configure.call_args_list[-1][1]["text"]
        assert "patch_applied=yes" in last_text
        assert "applied_fields=resolution_type,seat_payouts" in last_text

    def test_label_omits_patch_applied_when_false(self, tmp_path: Path):
        """patch_applied=False (default) → label に ``patch_applied`` が出ない。"""
        dash, _gs, _audio_q, _stop = _make_mock_dashboard(tmp_path)
        thread = MagicMock()
        thread.get_last_summary.return_value = _stub_summary()
        thread.get_reconstruction_result.return_value = _stub_result(
            patch_applied=False,
        )
        dash._integration_thread = thread

        dash._apply_hand_finalized(1)
        last_text = dash._lbl_latest_advisory.configure.call_args_list[-1][1]["text"]
        assert "patch_applied" not in last_text
        assert "applied_fields" not in last_text

    def test_apply_hand_finalized_tracks_latest_advisory_id(
        self, tmp_path: Path,
    ):
        """_apply_hand_finalized 後 _latest_advisory_hand_id が更新される
        (Apply ボタンが正しい hand を指すように)。"""
        dash, _gs, _audio_q, _stop = _make_mock_dashboard(tmp_path)
        thread = MagicMock()
        thread.get_last_summary.return_value = _stub_summary()
        thread.get_reconstruction_result.return_value = _stub_result()
        dash._integration_thread = thread

        dash._apply_hand_finalized(42)
        assert dash._latest_advisory_hand_id == 42

    def test_apply_hand_finalized_append_to_history_false_skips_insert(
        self, tmp_path: Path,
    ):
        """``append_to_history=False`` で呼ばれた場合は history.insert を呼ばない
        (= refresh モード)。Latest advisory ラベルは更新する。
        """
        dash, _gs, _audio_q, _stop = _make_mock_dashboard(tmp_path)
        thread = MagicMock()
        thread.get_last_summary.return_value = _stub_summary()
        thread.get_reconstruction_result.return_value = _stub_result(
            patch_applied=True,
            applied_fields=["resolution_type"],
        )
        dash._integration_thread = thread

        dash._apply_hand_finalized(7, append_to_history=False)

        # history.insert は呼ばれない
        dash._history_box.insert.assert_not_called()
        # Latest advisory ラベルは更新される
        last_text = dash._lbl_latest_advisory.configure.call_args_list[-1][1]["text"]
        assert "patch_applied=yes" in last_text


# ――― Phase 5-F: history Textbox の bounded retention + evicted hand degrade ―――


class TestPhase5FHistoryRetention:
    """history Textbox に行数上限を入れ、evicted hand でも GUI が degrade する。"""

    def test_trim_deletes_when_over_limit(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """``_trim_history_lines``: index 5.0 + MAX=3 → 先頭 2 行を削除する。"""
        from gui import dashboard as dash_mod
        monkeypatch.setattr(dash_mod, "MAX_HISTORY_LINES", 3)

        dash, _gs, _audio_q, _stop = _make_mock_dashboard(tmp_path)
        dash._history_box.index = MagicMock(return_value="5.0")
        dash._history_box.delete = MagicMock()

        dash._trim_history_lines()
        # excess = 5 - 3 = 2 → "1.0" から "3.0" まで削除
        dash._history_box.delete.assert_called_once_with("1.0", "3.0")

    def test_trim_noop_when_under_limit(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """line count が MAX_HISTORY_LINES 以下なら delete は呼ばれない。"""
        from gui import dashboard as dash_mod
        monkeypatch.setattr(dash_mod, "MAX_HISTORY_LINES", 1000)

        dash, _gs, _audio_q, _stop = _make_mock_dashboard(tmp_path)
        dash._history_box.index = MagicMock(return_value="500.0")
        dash._history_box.delete = MagicMock()

        dash._trim_history_lines()
        dash._history_box.delete.assert_not_called()

    def test_trim_noop_when_exactly_at_limit(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """line count == MAX_HISTORY_LINES なら境界上で削除しない。"""
        from gui import dashboard as dash_mod
        monkeypatch.setattr(dash_mod, "MAX_HISTORY_LINES", 3)

        dash, _gs, _audio_q, _stop = _make_mock_dashboard(tmp_path)
        dash._history_box.index = MagicMock(return_value="3.0")
        dash._history_box.delete = MagicMock()

        dash._trim_history_lines()
        dash._history_box.delete.assert_not_called()

    def test_trim_safe_on_bad_index(self, tmp_path: Path) -> None:
        """``index()`` が想定外の値を返しても例外を出さない。"""
        dash, _gs, _audio_q, _stop = _make_mock_dashboard(tmp_path)
        dash._history_box.index = MagicMock(return_value="not-a-line.index")
        dash._history_box.delete = MagicMock()

        # 例外を出さない
        dash._trim_history_lines()
        dash._history_box.delete.assert_not_called()

    def test_trim_safe_on_attribute_error(self, tmp_path: Path) -> None:
        """``index`` 呼び出し自体が AttributeError でも safe degrade。"""
        dash, _gs, _audio_q, _stop = _make_mock_dashboard(tmp_path)
        dash._history_box.index = MagicMock(
            side_effect=AttributeError("no such method"),
        )
        dash._history_box.delete = MagicMock()

        dash._trim_history_lines()
        dash._history_box.delete.assert_not_called()

    def test_apply_hand_finalized_invokes_trim(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """``_apply_hand_finalized`` は insert 直後に ``_trim_history_lines`` を呼ぶ
        (= 上限を超えた状態が長く残らない)。
        """
        from gui import dashboard as dash_mod
        monkeypatch.setattr(dash_mod, "MAX_HISTORY_LINES", 3)

        dash, _gs, _audio_q, _stop = _make_mock_dashboard(tmp_path)
        thread = MagicMock()
        thread.get_last_summary.return_value = _stub_summary()
        thread.get_reconstruction_result.return_value = _stub_result()
        dash._integration_thread = thread
        # 5 行ある状態をシミュレート
        dash._history_box.index = MagicMock(return_value="5.0")
        dash._history_box.delete = MagicMock()

        dash._apply_hand_finalized(1)
        # insert 後に trim が走り、超過分が削除される
        dash._history_box.delete.assert_called_once_with("1.0", "3.0")


class TestPhase5FEvictedHandDegrade:
    """evicted hand_id (= accessor が None を返す) でも GUI が安全 degrade する。"""

    def test_apply_hand_finalized_handles_evicted_hand(
        self, tmp_path: Path,
    ) -> None:
        """両 accessor が None を返す hand_id でも、``_apply_hand_finalized`` は
        例外を投げず Phase 4-C2 / 5-E の degrade 経路 ([SKIPPED] + blinds=?/?
        source=unknown) で動く。
        """
        dash, _gs, _audio_q, _stop = _make_mock_dashboard(tmp_path)
        thread = MagicMock()
        # evicted hand_id: 両 accessor が None を返す
        thread.get_last_summary.return_value = None
        thread.get_reconstruction_result.return_value = None
        dash._integration_thread = thread

        # 例外を出さない
        dash._apply_hand_finalized(999)

        # 履歴行は [SKIPPED] tag で書かれる (Phase 4-C2 の summary=None 経路)
        insert_calls = dash._history_box.insert.call_args_list
        assert len(insert_calls) == 1
        args, _ = insert_calls[0]
        assert "#999" in args[1]
        assert "[SKIPPED]" in args[1]
        assert args[2] == "skipped"

        # Latest advisory には Phase 5-E の degrade 表示
        last_text = dash._lbl_latest_advisory.configure.call_args_list[-1][1]["text"]
        assert "Latest advisory hand #999" in last_text
        assert "blinds=?/?" in last_text
        assert "source=unknown" in last_text
        # mismatch なし (= patch_proposal が無いので)
        assert "blind_mismatch" not in last_text
