"""tests/test_reconstructor_live_hook.py

Phase 4-A: IntegrationThread から HandReconstructor を **advisory** として
呼び出す経路をテストする。

検証範囲:
  1. live hook 経由で reconstruct が走り、`_last_summary_by_hand_id` と
     `_last_reconstruction_by_hand_id` が hand_id 別に埋まること
  2. online HandSummary (JSON / GameStateManager の stack) が advisory 経路で
     mutate されないこと
  3. 複数 hand が独立に tracked されること
  4. reconstruct 失敗 (例外) しても online path が壊れないこと
  5. online_summary 無しで invoke した場合 ``"reconstruction_skipped"`` が返ること

`tests/test_e2e.py` / `tests/test_bayesian_e2e.py` / `tests/test_hand_boundary.py`
等の既存 e2e テストが green を維持していることは別途確認する。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Optional

import pytest

from core.event_queue import EventQueue
from core.events import AudioEvent
from core.game_state import GameStateManager, PlayerState
from core.hand_log import HandSummary
from core.hand_reconstructor import HandReconstructionResult, HandReconstructor
from integration.engine import IntegrationThread
from output.json_writer import JsonWriter


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────


def _build_thread(
    tmp_path: Path,
    session_id: str = "phase4a",
    on_hand_finalized=None,
):
    """SB=100 / BB=200 / 2 players (10k each) で IntegrationThread を組む。"""
    players = [
        PlayerState(seat=1, name="A", stack=10000),
        PlayerState(seat=2, name="B", stack=10000),
    ]
    gs = GameStateManager(players=players, sb=100, bb=200)
    audio_q = EventQueue()
    writer = JsonWriter(log_dir=tmp_path, session_id=session_id)
    stop = threading.Event()
    thread = IntegrationThread(
        audio_queue=audio_q,
        game_state=gs,
        json_writer=writer,
        stop_event=stop,
        initial_button_seat=2,
        auto_post_blinds=True,
        on_hand_finalized=on_hand_finalized,
    )
    return thread, audio_q, stop, writer, gs


def _drive_fold_win_hand(audio_q: EventQueue, t0: float, winner_seat: int = 1) -> None:
    """new_hand → fold (seat 2) → winner (seat 1) の 3 events を流す。

    SB=seat 1, BB=seat 2 (HU, BTN=SB=seat 1) なので、preflop で seat 2 (BB) が
    fold すれば seat 1 (BTN/SB) が勝つ fold_win 経路。
    """
    audio_q.put(AudioEvent("new_hand", 0, t0, ""))
    # SB/BB auto-post の後、最初 actor は HU preflop なので BTN=seat 1
    # ここでは BTN=seat 2 で start_hand しているので seat 2 = SB, seat 1 = BB
    # → HU preflop の first actor は SB = seat 2
    audio_q.put(AudioEvent("fold", 0, t0 + 0.1, "フォールド"))
    audio_q.put(AudioEvent("winner", 0, t0 + 0.2, f"シート{winner_seat} ウィナー"))


# ────────────────────────────────────────────────────────────────────────────
# 1. live hook 経由で reconstruct が走る
# ────────────────────────────────────────────────────────────────────────────


class TestReconstructorLiveHookInvoked:
    def test_live_hook_populates_per_hand_id_dicts(self, tmp_path: Path) -> None:
        """1 hand 完了で _last_summary_by_hand_id / _last_reconstruction_by_hand_id
        が hand_id をキーに正しく埋まる。
        """
        thread, audio_q, stop, _writer, _gs = _build_thread(tmp_path)
        _drive_fold_win_hand(audio_q, time.time())

        thread.start()
        time.sleep(0.8)
        stop.set()
        thread.join(timeout=2.0)

        assert 1 in thread._last_summary_by_hand_id
        assert 1 in thread._last_reconstruction_by_hand_id
        result = thread._last_reconstruction_by_hand_id[1]
        assert isinstance(result, HandReconstructionResult)
        assert result.summary is not None
        assert result.reason in {"reconstructed_no_diff", "reconstructed_with_diff"}

    def test_last_reconstruction_alias_points_to_most_recent(self, tmp_path: Path) -> None:
        """convenience pointer ``_last_reconstruction`` は dict 内の最新エントリと一致。"""
        thread, audio_q, stop, _writer, _gs = _build_thread(tmp_path)
        _drive_fold_win_hand(audio_q, time.time())

        thread.start()
        time.sleep(0.8)
        stop.set()
        thread.join(timeout=2.0)

        assert thread._last_reconstruction is not None
        assert thread._last_reconstruction is thread._last_reconstruction_by_hand_id[1]

    def test_summary_stored_matches_jsonwriter_record(self, tmp_path: Path) -> None:
        """_last_summary_by_hand_id[hand_id] は JsonWriter に渡された summary と一致。"""
        thread, audio_q, stop, _writer, _gs = _build_thread(tmp_path)
        _drive_fold_win_hand(audio_q, time.time())

        thread.start()
        time.sleep(0.8)
        stop.set()
        thread.join(timeout=2.0)

        stored = thread._last_summary_by_hand_id[1]
        assert isinstance(stored, HandSummary)
        # JsonWriter は logs/<session_id>.json に同 hand_id を書いている
        json_path = tmp_path / "phase4a.json"
        assert json_path.exists()
        data = json.loads(json_path.read_text(encoding="utf-8"))
        hands = data.get("hands", [])
        assert len(hands) == 1
        assert int(hands[0]["hand_id"]) == int(stored.hand_id)
        assert int(hands[0]["pot_total"]) == int(stored.pot_total)
        assert int(hands[0]["winner_seat"]) == int(stored.winner_seat)


# ────────────────────────────────────────────────────────────────────────────
# 2. online path が advisory 経路で mutate されない
# ────────────────────────────────────────────────────────────────────────────


class TestOnlinePathUntouched:
    def test_online_json_content_unchanged_by_advisory(self, tmp_path: Path) -> None:
        """advisory reconstruct が走った後でも logs/<session>.json は online 値そのまま。"""
        thread, audio_q, stop, _writer, _gs = _build_thread(tmp_path, "advisory_json")
        _drive_fold_win_hand(audio_q, time.time())

        thread.start()
        time.sleep(0.8)
        stop.set()
        thread.join(timeout=2.0)

        # _last_summary_by_hand_id[1] と JSON の hand 0 が一致 (advisory が上書きしていない)
        stored = thread._last_summary_by_hand_id[1]
        data = json.loads((tmp_path / "advisory_json.json").read_text(encoding="utf-8"))
        hand = data["hands"][0]
        assert hand["resolution_status"] == stored.resolution_status
        assert hand["resolution_type"] == stored.resolution_type
        # seat_payouts: JSON では key が str になる可能性があるので int 化して比較
        json_payouts = {int(k): int(v) for k, v in (hand.get("seat_payouts") or {}).items()}
        assert json_payouts == {int(k): int(v) for k, v in stored.seat_payouts.items()}

    def test_game_state_stacks_reflect_online_only(self, tmp_path: Path) -> None:
        """advisory が走っても stacks は online _apply_payouts_to_gamestate の結果と
        完全一致 (二重適用されていない)。
        """
        thread, audio_q, stop, _writer, gs = _build_thread(tmp_path)
        _drive_fold_win_hand(audio_q, time.time())

        thread.start()
        time.sleep(0.8)
        stop.set()
        thread.join(timeout=2.0)

        stacks = gs.get_stacks()
        # 開始時 seat1=10000, seat2=10000, SB=100 (seat 2 BTN=SB after HU rotation),
        # BB=200 (seat 1). seat 2 fold → seat 1 wins pot=300.
        # 終局後: seat 1 = 10000 + 100 (= pot - own BB), seat 2 = 10000 - 100 (= -SB)
        # 合計は invariant
        assert sum(stacks.values()) == 20000
        # winner_seat の stack が増えている
        winner = thread._last_summary_by_hand_id[1].winner_seat
        loser = 2 if winner == 1 else 1
        assert stacks[winner] > 10000
        assert stacks[loser] < 10000


# ────────────────────────────────────────────────────────────────────────────
# 3. 複数 hand が独立に tracked される
# ────────────────────────────────────────────────────────────────────────────


class TestUpdateBlinds:
    """Phase 5-C: ``IntegrationThread.update_blinds`` の挙動。"""

    def test_update_blinds_syncs_all_canonical_state(self, tmp_path: Path) -> None:
        """update_blinds で IntegrationThread / GameStateManager /
        HandReconstructor の blind state が同時更新される。
        """
        thread, _audio_q, _stop, _writer, gs = _build_thread(tmp_path)
        # 初期値
        assert thread._sb_amount == 100
        assert thread._bb_amount == 200
        assert gs._sb == 100
        assert gs._bb == 200
        assert thread._hand_reconstructor._default_sb == 100
        assert thread._hand_reconstructor._default_bb == 200
        assert thread._hand_reconstructor._blinds_updated_at_runtime is False

        thread.update_blinds(300, 600)

        # 3 ヶ所すべて同期
        assert thread._sb_amount == 300
        assert thread._bb_amount == 600
        assert gs._sb == 300
        assert gs._bb == 600
        assert thread._hand_reconstructor._default_sb == 300
        assert thread._hand_reconstructor._default_bb == 600
        # HandReconstructor は runtime updated フラグが立つ
        assert thread._hand_reconstructor._blinds_updated_at_runtime is True

    def test_update_blinds_raises_on_invalid(self, tmp_path: Path) -> None:
        """IntegrationThread.update_blinds は不正値で ValueError を投げる
        (HandReconstructor 側の黙殺と違い、GUI に明示的に伝える)。
        """
        thread, _audio_q, _stop, _writer, _gs = _build_thread(tmp_path)
        with pytest.raises(ValueError):
            thread.update_blinds(-5, 200)
        with pytest.raises(ValueError):
            thread.update_blinds(0, 0)
        with pytest.raises(ValueError):
            thread.update_blinds("bogus", 400)  # type: ignore[arg-type]
        # state は変わらない
        assert thread._sb_amount == 100
        assert thread._bb_amount == 200

    def test_update_blinds_does_not_affect_finished_hand_summary(
        self, tmp_path: Path,
    ) -> None:
        """進行中 hand を 1 件回した後 update_blinds しても、その hand の
        ``HandSummary.blinds`` は旧値のまま (= 過去 hand を書き換えない)。
        """
        thread, audio_q, stop, _writer, gs = _build_thread(tmp_path, "phase5c_no_retro")
        _drive_fold_win_hand(audio_q, time.time())

        thread.start()
        time.sleep(0.8)
        stop.set()
        thread.join(timeout=2.0)

        # hand 1 終局後に blinds を変更
        old_summary = thread.get_last_summary(1)
        assert old_summary is not None
        assert int(old_summary.blinds["sb"]) == 100
        assert int(old_summary.blinds["bb"]) == 200

        thread.update_blinds(500, 1000)
        # update 後でも summary は旧値を保持
        assert int(old_summary.blinds["sb"]) == 100
        assert int(old_summary.blinds["bb"]) == 200


class TestOnHandFinalizedCallback:
    """Phase 4-C2: ``on_hand_finalized`` callback の動作。"""

    def test_callback_invoked_on_hand_finalize(self, tmp_path: Path) -> None:
        """1 hand 完了 → callback が hand_id 引数で 1 回呼ばれる。"""
        received: list[int] = []
        thread, audio_q, stop, _writer, _gs = _build_thread(
            tmp_path, on_hand_finalized=received.append,
        )
        _drive_fold_win_hand(audio_q, time.time())

        thread.start()
        time.sleep(0.8)
        stop.set()
        thread.join(timeout=2.0)

        assert received == [1]

    def test_callback_fires_after_advisory_is_stored(self, tmp_path: Path) -> None:
        """callback 時点で ``get_reconstruction_result`` から advisory が読める
        (= advisory 計算後に発火している)。
        """
        captured: list[tuple[int, object, object]] = []

        # callback 内で accessor を呼んで snapshot を取る
        def cb(hand_id: int) -> None:
            res = thread.get_reconstruction_result(hand_id)
            summary = thread.get_last_summary(hand_id)
            captured.append((hand_id, res, summary))

        thread, audio_q, stop, _w, _gs = _build_thread(
            tmp_path, on_hand_finalized=cb,
        )
        _drive_fold_win_hand(audio_q, time.time())

        thread.start()
        time.sleep(0.8)
        stop.set()
        thread.join(timeout=2.0)

        assert len(captured) == 1
        hand_id, result, summary = captured[0]
        assert hand_id == 1
        # advisory が確定済み (None ではない)
        assert result is not None
        assert summary is not None

    def test_callback_exception_does_not_break_online_path(
        self, tmp_path: Path,
    ) -> None:
        """callback で例外を投げても JSON 書き込みと stacks 更新は完了する。"""
        def boom(hand_id: int) -> None:
            raise RuntimeError("simulated callback failure")

        thread, audio_q, stop, _w, gs = _build_thread(
            tmp_path, session_id="cb_fail", on_hand_finalized=boom,
        )
        _drive_fold_win_hand(audio_q, time.time())

        thread.start()
        time.sleep(0.8)
        stop.set()
        thread.join(timeout=2.0)

        # online path は完了している (JSON 書き込み + stacks 更新)
        json_path = tmp_path / "cb_fail.json"
        assert json_path.exists()
        stacks = gs.get_stacks()
        assert sum(stacks.values()) == 20000


class TestAdvisoryEviction:
    """Phase 5-F: ``_last_summary_by_hand_id`` / ``_last_reconstruction_by_hand_id``
    が ``MAX_ADVISORY_HANDS`` 件を超えたら古い hand から evict される。"""

    def test_evict_keeps_latest_n_hands_symmetric(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """両 dict 同じ hand_id を持つ場合: union 5 件 / MAX=3 → {3,4,5} だけ残る。"""
        monkeypatch.setattr("integration.engine.MAX_ADVISORY_HANDS", 3)
        thread, _aq, _stop, _w, _gs = _build_thread(tmp_path)

        # 両 dict に hand_id 1..5 を入れる (中身は適当な sentinel)
        for hid in [1, 2, 3, 4, 5]:
            thread._last_summary_by_hand_id[hid] = object()
            thread._last_reconstruction_by_hand_id[hid] = object()

        thread._evict_old_advisory_entries()

        # MAX=3 → excess=2 → 昇順 [1,2] を捨てる
        assert set(thread._last_summary_by_hand_id.keys()) == {3, 4, 5}
        assert set(thread._last_reconstruction_by_hand_id.keys()) == {3, 4, 5}

    def test_evict_handles_asymmetric_dicts(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """片方の dict にしか entry が無い hand_id も union 経由で eviction される。"""
        monkeypatch.setattr("integration.engine.MAX_ADVISORY_HANDS", 3)
        thread, _aq, _stop, _w, _gs = _build_thread(tmp_path)

        # summary は {1,2,3,4} (4 件)、reconstruction は {2,5} (2 件)。
        # union={1,2,3,4,5}=5 件。MAX=3 → excess=2 → {1,2} を捨てる。
        thread._last_summary_by_hand_id = {h: object() for h in [1, 2, 3, 4]}
        thread._last_reconstruction_by_hand_id = {h: object() for h in [2, 5]}

        thread._evict_old_advisory_entries()

        # summary: 4 件中 {1,2} が消えて {3,4} 残る
        assert set(thread._last_summary_by_hand_id.keys()) == {3, 4}
        # reconstruction: 2 件中 {2} が消えて {5} 残る
        assert set(thread._last_reconstruction_by_hand_id.keys()) == {5}

    def test_evict_noop_under_limit(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """size <= MAX_ADVISORY_HANDS の場合は eviction しない (no-op)。"""
        monkeypatch.setattr("integration.engine.MAX_ADVISORY_HANDS", 10)
        thread, _aq, _stop, _w, _gs = _build_thread(tmp_path)

        for hid in [1, 2, 3]:
            thread._last_summary_by_hand_id[hid] = object()
            thread._last_reconstruction_by_hand_id[hid] = object()

        thread._evict_old_advisory_entries()
        assert set(thread._last_summary_by_hand_id.keys()) == {1, 2, 3}
        assert set(thread._last_reconstruction_by_hand_id.keys()) == {1, 2, 3}

    def test_evict_via_invoke_reconstructor_hook(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """``_invoke_reconstructor_hook`` 経由で連続 5 hand 走らせると最新 3 だけ残る
        (= eviction が hook の中から自動的に走る)。
        """
        monkeypatch.setattr("integration.engine.MAX_ADVISORY_HANDS", 3)
        thread, _aq, _stop, _w, _gs = _build_thread(tmp_path)

        # 空 window で 5 hand 分の hook を invoke (online_summary=None → skipped 結果)。
        # 各回で reconstruction dict に entry が積まれていき、eviction が走る。
        for hid in [1, 2, 3, 4, 5]:
            thread._completed_hands[hid] = []
            thread._invoke_reconstructor_hook(hid, online_summary=None)

        remaining = set(thread._last_reconstruction_by_hand_id.keys())
        assert remaining == {3, 4, 5}

    def test_get_accessors_return_none_for_evicted(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """evicted hand_id への ``get_last_summary`` / ``get_reconstruction_result``
        は None を返す (= GUI の degrade パスへ送り込む)。
        """
        monkeypatch.setattr("integration.engine.MAX_ADVISORY_HANDS", 2)
        thread, _aq, _stop, _w, _gs = _build_thread(tmp_path)

        for hid in [1, 2, 3]:
            thread._last_summary_by_hand_id[hid] = object()
            thread._last_reconstruction_by_hand_id[hid] = object()
        thread._evict_old_advisory_entries()

        # hand 1 は evict されたので None
        assert thread.get_last_summary(1) is None
        assert thread.get_reconstruction_result(1) is None
        # hand 2, 3 は残っている
        assert thread.get_last_summary(2) is not None
        assert thread.get_reconstruction_result(2) is not None
        assert thread.get_last_summary(3) is not None
        assert thread.get_reconstruction_result(3) is not None

    def test_default_max_advisory_hands_is_500(self) -> None:
        """default value のサニティチェック (将来変えるときは意識的に)。"""
        from integration.engine import MAX_ADVISORY_HANDS
        assert MAX_ADVISORY_HANDS == 500


class TestMultipleHandsTracked:
    def test_two_hands_independently_tracked(self, tmp_path: Path) -> None:
        thread, audio_q, stop, _writer, _gs = _build_thread(tmp_path)
        t0 = time.time()
        # hand 1
        _drive_fold_win_hand(audio_q, t0)
        # hand 2 (1 秒後)
        _drive_fold_win_hand(audio_q, t0 + 1.0)

        thread.start()
        time.sleep(1.5)
        stop.set()
        thread.join(timeout=2.0)

        assert {1, 2} <= set(thread._last_summary_by_hand_id.keys())
        assert {1, 2} <= set(thread._last_reconstruction_by_hand_id.keys())
        # 各 hand の結果は別オブジェクト
        assert (
            thread._last_reconstruction_by_hand_id[1]
            is not thread._last_reconstruction_by_hand_id[2]
        )
        # convenience pointer は最新 (hand 2)
        assert thread._last_reconstruction is thread._last_reconstruction_by_hand_id[2]


# ────────────────────────────────────────────────────────────────────────────
# 4. reconstruct 失敗時の online 不変
# ────────────────────────────────────────────────────────────────────────────


class _RaisingReconstructor:
    """常に例外を投げる reconstructor。online path への影響を試す。"""

    def reconstruct_from_events(self, events, initial_state=None, online_summary=None):
        raise RuntimeError("simulated reconstruct failure")


class TestReconstructorFailureIsolated:
    def test_reconstructor_exception_does_not_break_online(self, tmp_path: Path) -> None:
        """reconstruct で例外が出ても online HandSummary 書き込みと stacks 更新は完了。"""
        thread, audio_q, stop, _writer, gs = _build_thread(tmp_path, "advisory_fail")
        # reconstructor を例外を投げる stub に差し替え
        thread._hand_reconstructor = _RaisingReconstructor()

        _drive_fold_win_hand(audio_q, time.time())

        thread.start()
        time.sleep(0.8)
        stop.set()
        thread.join(timeout=2.0)

        # online path はクラッシュせず完了している
        json_path = tmp_path / "advisory_fail.json"
        assert json_path.exists()
        data = json.loads(json_path.read_text(encoding="utf-8"))
        assert len(data.get("hands", [])) == 1
        # stacks も更新済み
        stacks = gs.get_stacks()
        assert sum(stacks.values()) == 20000
        # online summary は保存されている (例外は reconstruct 側のみ)
        assert 1 in thread._last_summary_by_hand_id
        # reconstruction 側は dict に入っていない (例外で抜けたため)
        assert 1 not in thread._last_reconstruction_by_hand_id
        assert thread._last_reconstruction is None


# ────────────────────────────────────────────────────────────────────────────
# 5. online_summary=None ケース (Phase 2-C と同じ挙動を維持)
# ────────────────────────────────────────────────────────────────────────────


class TestInvokeHookWithoutOnlineSummary:
    def test_invoke_hook_none_returns_skipped(self, tmp_path: Path) -> None:
        """_invoke_reconstructor_hook(hand_id, online_summary=None) は、events が
        空であれば raw / online_summary どちらの bootstrap も成立せず
        ``reason="reconstruction_skipped"`` を返す。
        """
        thread, _audio_q, _stop, _writer, _gs = _build_thread(tmp_path)
        # ダミーの空 window を _completed_hands に入れる
        thread._completed_hands[99] = []

        thread._invoke_reconstructor_hook(99, online_summary=None)

        assert 99 in thread._last_reconstruction_by_hand_id
        result = thread._last_reconstruction_by_hand_id[99]
        assert result.reason == "reconstruction_skipped"

    def test_invoke_hook_unknown_hand_id_handles_gracefully(self, tmp_path: Path) -> None:
        """_completed_hands に存在しない hand_id でも例外を出さず skipped で抜ける。"""
        thread, _audio_q, _stop, _writer, _gs = _build_thread(tmp_path)
        thread._invoke_reconstructor_hook(12345, online_summary=None)
        # 空 events から bootstrap できないので skipped が入る
        assert thread._last_reconstruction_by_hand_id[12345].reason == "reconstruction_skipped"

    def test_get_reconstruction_result_returns_none_for_unknown_hand_id(
        self, tmp_path: Path,
    ) -> None:
        """Phase 4-C2 accessor: 該当 hand 無しなら None を返す。"""
        thread, _aq, _stop, _w, _gs = _build_thread(tmp_path)
        assert thread.get_reconstruction_result(999) is None
        assert thread.get_last_summary(999) is None

    def test_get_reconstruction_result_returns_stored_entry(
        self, tmp_path: Path,
    ) -> None:
        """Phase 4-C2 accessor: dict に保存済みのエントリを返す。"""
        thread, _aq, _stop, _w, _gs = _build_thread(tmp_path)
        # ダミー entry を直接注入して accessor を検証
        thread._completed_hands[77] = []
        thread._invoke_reconstructor_hook(77, online_summary=None)
        result = thread.get_reconstruction_result(77)
        assert result is not None
        assert result.reason == "reconstruction_skipped"

    def test_invoke_hook_none_with_rfid_raw_bootstrap_succeeds(
        self, tmp_path: Path,
    ) -> None:
        """Phase 4-B: online_summary=None でも _completed_hands に RFID hole_cards が
        揃っていれば raw-only bootstrap が成立し ``reason != "reconstruction_skipped"``
        になる。これが Phase 4-A 時代との挙動差。

        IntegrationThread コンストラクタは GameStateManager から SB=100/BB=200 を
        引き継ぎ、HandReconstructor(default_sb=100, default_bb=200) を立てる。
        """
        from core.events import RFIDEvent
        from output.replay_hand import EvidenceRecord

        thread, _audio_q, _stop, _writer, _gs = _build_thread(tmp_path)
        # 1 hand 分の events を _completed_hands に直接注入
        thread._completed_hands[77] = [
            EvidenceRecord(
                timestamp=1.0, kind="audio",
                event=AudioEvent(action="new_hand", amount=0, timestamp=1.0, raw_text=""),
                payload={},
            ),
            EvidenceRecord(
                timestamp=1.1, kind="rfid",
                event=RFIDEvent(
                    tag_id="t1", card="Ah", reader_id="seat_1", role="seat",
                    seat=1, timestamp=1.1, raw_tag_id="t1",
                ),
                payload={},
            ),
            EvidenceRecord(
                timestamp=1.2, kind="rfid",
                event=RFIDEvent(
                    tag_id="t2", card="Kh", reader_id="seat_2", role="seat",
                    seat=2, timestamp=1.2, raw_tag_id="t2",
                ),
                payload={},
            ),
            EvidenceRecord(
                timestamp=1.3, kind="audio",
                event=AudioEvent(action="fold", amount=0, timestamp=1.3,
                                  raw_text="フォールド"),
                payload={},
            ),
            EvidenceRecord(
                timestamp=1.4, kind="audio",
                event=AudioEvent(action="winner", amount=0, timestamp=1.4,
                                  raw_text="シート2 ウィナー"),
                payload={},
            ),
        ]

        thread._invoke_reconstructor_hook(77, online_summary=None)

        result = thread._last_reconstruction_by_hand_id[77]
        assert result.bootstrap_source == "raw"
        # online_summary が無いので reason は "reconstructed" (diff 計算なし)
        assert result.reason == "reconstructed"
        assert result.summary is not None
