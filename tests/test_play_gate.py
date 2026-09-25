"""tests/test_play_gate.py

店舗の 2 回目の通しテスト（2026-09-25）への対応:

- シャッフル・ウォッシュでリーダーを通った札でハンドが始まっていた → RFID の在否で、2 席以上に手札 2 枚ずつが
  載り続け、ボードのリーダーが空のときだけ配布とする。
- ボードは手札が配られてから読む。ショーダウン待ちのボードは次のハンドのウォッシュで差し替えない。
- プレー中でないときの音声は認識に回さない（認識待ちが数分たまっていた）。
- Whisper の雑音への幻聴（プロンプトの繰り返し）を捨てる。温度を上げたやり直しをしない。
- 「なべとなっております」の「ベト」をベットと読まない。
"""
from __future__ import annotations

import queue
import struct
import threading
from pathlib import Path

import pytest

from audio.recognizer import WhisperTranscriber, is_prompt_echo, parse_actions
from audio.recorder import AudioThread
from core.event_queue import make_audio_queue
from core.events import RFIDEvent
from core.game_state import PlayerState
from integration.engine import DEAL_STABLE_SEC, TABLE_CLEAR_SEC, IntegrationThread
from output.json_writer import JsonWriter

pytest.importorskip("pokerkit")

from core.poker_engine import PokerkitGameState  # noqa: E402


class _Table:
    """RFID の在否（席ごとの札・ボードの枚数）を手で動かす卓。"""

    def __init__(self, tmp_path: Path, seats=(2, 5, 8)):
        self.gs = PokerkitGameState(
            [PlayerState(seat=s, name=f"P{s}", stack=1000) for s in seats], sb=5, bb=10)
        self.now = 0.0
        self.cards: dict[int, list[str]] = {}
        self.board_count = 0
        self.gate = threading.Event()
        self.hands: list = []
        self.resets = 0
        self.t = IntegrationThread(
            audio_queue=make_audio_queue(), game_state=self.gs,
            json_writer=JsonWriter(tmp_path, "gate"), on_hand=self.hands.append,
            stop_event=threading.Event(), clock=lambda: self.now,
            on_new_hand=self._reset, auto_new_hand=True, auto_winner=True,
            seat_presence=self._presence, board_presence=self._board, listen_gate=self.gate,
        )

    def _reset(self) -> None:
        self.resets += 1

    def _presence(self) -> dict:
        return {s: {"present": bool(c), "cards": list(c), "uid_count": len(c)}
                for s, c in self.cards.items()}

    def _board(self) -> dict:
        return {"present_count": self.board_count}

    def tick(self, until: float, step: float = 0.1) -> None:
        """run() のループ（在否の確認）を `until` まで回す。"""
        while self.now < until:
            self.now = round(self.now + step, 3)
            self.t._check_deal_presence()   # noqa: SLF001
            self.t._check_table_cleared()   # noqa: SLF001

    def say(self, text: str) -> None:
        for event in parse_actions(text, confidence=0.9, utterance_start_ts=self.now):
            event.timestamp = self.now
            self.t._handle_audio_event(event)   # noqa: SLF001

    def board(self, index: int, card: str, replaces=None) -> None:
        self.t._process_rfid_event(RFIDEvent(   # noqa: SLF001
            tag_id=card, card=card, reader_id="b", role="board", seat=None, timestamp=self.now,
            raw_tag_id=card, board_index=index, replaces=replaces))


class TestDealDetection:
    def test_two_seats_holding_two_cards_start_a_hand(self, tmp_path):
        tb = _Table(tmp_path)
        tb.cards = {2: ["As", "Ad"], 5: ["Kd", "Kc"]}
        tb.tick(1.0)
        assert tb.gs.hand_id == 0                  # まだ載り続けていない
        tb.tick(1.0 + DEAL_STABLE_SEC)
        assert tb.gs.hand_id == 1 and tb.gate.is_set() and tb.resets == 1
        assert tb.t._hole_cards == {2: ["As", "Ad"], 5: ["Kd", "Kc"]}   # noqa: SLF001

    def test_cards_passing_during_a_shuffle_do_not_start_a_hand(self, tmp_path):
        tb = _Table(tmp_path)
        for start in (0.0, 3.0, 6.0):              # 一瞬ずつ席のリーダーを通る
            tb.cards = {2: ["As", "Ad"], 5: ["Kd", "Kc"]}
            tb.tick(start + 1.0)
            tb.cards = {}
            tb.tick(start + 3.0)
        assert tb.gs.hand_id == 0

    def test_no_hand_while_cards_are_on_the_board_readers(self, tmp_path):
        tb = _Table(tmp_path)
        tb.board_count = 3                         # ウォッシュ中
        tb.cards = {2: ["As", "Ad"], 5: ["Kd", "Kc"]}
        tb.tick(5.0)
        assert tb.gs.hand_id == 0
        tb.board_count = 0
        tb.tick(5.2)
        assert tb.gs.hand_id == 1

    def test_one_seat_is_not_a_deal(self, tmp_path):
        tb = _Table(tmp_path)
        tb.cards = {2: ["As", "Ad"], 5: ["Kd"]}
        tb.tick(5.0)
        assert tb.gs.hand_id == 0

    def test_cards_left_from_the_last_hand_do_not_start_the_next(self, tmp_path):
        tb = _Table(tmp_path)
        tb.cards = {2: ["As", "Ad"], 5: ["Kd", "Kc"], 8: ["Qh", "Qc"]}
        tb.tick(2.0)
        tb.say("フォールド")                        # BTN 8
        tb.say("フォールド")                        # SB 2 → BB 5 の勝ち
        assert len(tb.hands) == 1 and not tb.gate.is_set()
        tb.tick(10.0)                               # 札はまだ席に残っている
        assert tb.gs.hand_id == 1
        tb.cards = {2: ["7s", "7d"], 5: ["8s", "8d"], 8: ["9s", "9d"]}
        tb.tick(12.0)
        assert tb.gs.hand_id == 2


class TestBoardOnlyWhilePlaying:
    def test_board_before_the_deal_is_ignored(self, tmp_path):
        tb = _Table(tmp_path)
        tb.board(1, "7s")                          # シャッフル中に読めた
        tb.cards = {2: ["As", "Ad"], 5: ["Kd", "Kc"]}
        tb.tick(2.0)
        tb.board(1, "2c")
        assert tb.t._board_cards == ["2c"]         # noqa: SLF001

    def test_the_board_at_showdown_is_not_replaced_by_the_next_wash(self, tmp_path):
        tb = _Table(tmp_path)
        tb.cards = {2: ["As", "Ad"], 5: ["Kd", "Kc"], 8: ["Qh", "Qc"]}
        tb.tick(2.0)
        tb.say("コール コール チェック")
        for i, card in enumerate(["2c", "7d", "9s", "Th", "3h"], start=1):
            tb.board(i, card)
        for _ in range(3):
            tb.say("チェック チェック チェック")
        tb.board(1, "5s", replaces="2c")           # 次のハンドのウォッシュ
        tb.board(4, "8s", replaces="Th")
        assert tb.t._board_cards == ["2c", "7d", "9s", "Th", "3h"]   # noqa: SLF001
        tb.say("ハンド終了")
        assert tb.hands[0].winner_seat == 2


class TestListenGate:
    def test_listening_follows_play(self, tmp_path):
        tb = _Table(tmp_path)
        assert not tb.gate.is_set()                # 最初のハンドの前
        tb.cards = {2: ["As", "Ad"], 5: ["Kd", "Kc"], 8: ["Qh", "Qc"]}
        tb.tick(2.0)
        assert tb.gate.is_set()
        tb.say("フォールド フォールド")
        assert not tb.gate.is_set()                # 確定したら次の配布まで聞き流す

    def test_a_cleared_table_stops_listening_at_showdown(self, tmp_path):
        tb = _Table(tmp_path)
        tb.cards = {2: ["As", "Ad"], 5: ["Kd", "Kc"], 8: ["Qh", "Qc"]}
        tb.tick(2.0)
        tb.say("コール コール チェック")
        for i, card in enumerate(["2c", "7d", "9s", "Th", "3h"], start=1):
            tb.board(i, card)
        for _ in range(3):
            tb.say("チェック チェック チェック")
        tb.cards = {}                              # 札を片付けた（ハンド終了は言わなかった）
        tb.tick(2.0 + TABLE_CLEAR_SEC + 1.0)
        assert not tb.gate.is_set() and tb.t._hand_open   # noqa: SLF001

    def _capture(self, thread: AudioThread, gate: threading.Event, open_at: int) -> None:
        loud = struct.pack("1024h", *([5000] * 1024))
        quiet = b"\x00" * 2048
        chunks = [loud] * 6 + [quiet] * 9 + [b""]

        def read():
            if len(chunks) == 16 - open_at:
                gate.set()
            return chunks.pop(0)

        thread._capture_loop(read, 1024)   # noqa: SLF001

    def test_speech_outside_play_is_not_transcribed(self):
        gate = threading.Event()
        thread = AudioThread(audio_queue=queue.Queue(), stop_event=threading.Event(),
                             transcriber=_Fake(), listen_gate=gate)
        self._capture(thread, gate, open_at=99)
        assert thread.backlog() == 0 and thread.skipped == 1

    def test_speech_that_runs_into_play_is_kept(self):
        gate = threading.Event()
        thread = AudioThread(audio_queue=queue.Queue(), stop_event=threading.Event(),
                             transcriber=_Fake(), listen_gate=gate)
        self._capture(thread, gate, open_at=3)
        assert thread.backlog() == 1


class _Fake:
    ready = True

    def __init__(self, text: str = "コール") -> None:
        self.text = text

    def transcribe_with_confidence(self, audio_bytes: bytes):
        return self.text, 0.15


class TestWhisperNoise:
    @pytest.mark.parametrize("text", [
        "シート3 レイズ 2千、コール、チョップ などの言葉が含まれます",
        "シート3 レイズ 2千、コール、チェック、フォールド、オールイン、ショーダウン、ウィナー、ハンド開始、"
        "チョップ などの言葉が含まれます。",
        "ポーカーのディーラーがアクションを読み上げています。",
    ])
    def test_prompt_echoes_are_noise(self, text):
        assert is_prompt_echo(text)

    @pytest.mark.parametrize("text", ["コール", "レイズ 2000", "フォールド、フォールド、コール"])
    def test_real_announcements_are_not(self, text):
        assert not is_prompt_echo(text)

    def test_an_echo_produces_no_actions(self):
        seen = []
        q: queue.Queue = queue.Queue()
        thread = AudioThread(audio_queue=q, stop_event=threading.Event(), on_transcript=seen.append,
                             transcriber=_Fake("シート3 レイズ 2千、コール、チョップ などの言葉が含まれます"))
        thread._process_chunk(b"\x00\x00" * 1600, utterance_start_ts=1.0)   # noqa: SLF001
        assert q.empty() and seen[0].noise and seen[0].events == ()

    def test_no_temperature_fallback_by_default(self):
        calls = {}

        class Model:
            def transcribe(self, audio, **kwargs):
                calls.update(kwargs)
                return [], None

        t = WhisperTranscriber.__new__(WhisperTranscriber)
        t._model, t._language, t._beam_size, t._temperature = Model(), "ja", 5, 0.0
        t.transcribe_with_confidence(b"\x00\x00" * 160)
        assert calls["temperature"] == 0.0 and calls["beam_size"] == 5
        assert calls["condition_on_previous_text"] is False


class TestShortAliases:
    @pytest.mark.parametrize("text, expected", [
        ("なべとなっております。", []),             # 店舗の実測（「ベト」を含む）
        ("ベトナム", []),
        ("ゴールドのチップ", []),
        ("ホールドする", []),
        ("ベトナナ", [("bet", 7)]),
        ("レイズゴール", [("raise", 0), ("call", 0)]),
        ("ベッド1200", [("bet", 1200)]),
        ("フォールド、ホールド", [("fold", 0), ("fold", 0)]),
    ])
    def test_aliases_need_word_boundaries(self, text, expected):
        assert [(e.action, e.amount) for e in parse_actions(text)] == expected
