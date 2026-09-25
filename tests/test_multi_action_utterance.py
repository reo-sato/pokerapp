"""tests/test_multi_action_utterance.py

ADR-0061: 店舗の音声テスト（2026-09-25）で分かったことへの対応。実運用では席番号を言わず、アクターは
手番の順で決まるので、1 つのアクションを落とすと以降がすべてずれる。

- 間を空けずに続けて言った複数のアクションは、言った順に分ける（`parse_actions`）。
- 「ベッド」はベット（Whisper の書き起こしゆれ）。「チェックレイズ」は 1 つのレイズ。
- 認識が遅れても発話を捨てない。打った入力は、先に言われた発話を追い越さない。
- 「チェック」のような短い言葉を、短すぎる音として捨てない（0.15 秒から認識に回す）。
"""
from __future__ import annotations

import queue
import struct
import threading
from types import SimpleNamespace

import pytest

import main
from audio.recognizer import parse_action, parse_actions
from audio.recorder import AudioThread


def _read(text: str) -> list[tuple]:
    return [(e.action, e.amount, e.seat) for e in parse_actions(text, confidence=0.8)]


class TestSplit:
    @pytest.mark.parametrize("text, expected", [
        # 店舗で実際に 1 つの発話になったもの
        ("シート4 コール、シート1 フォールド", [("call", 0, 4), ("fold", 0, 1)]),
        ("シート2 ベッド1200、シート5 レイズ2500", [("bet", 1200, 2), ("raise", 2500, 5)]),
        ("フォールド、シート3、ウィナー", [("fold", 0, None), ("winner", 0, 3)]),
        ("ハンド開始、シート3 レイズ 600", [("new_hand", 0, None), ("raise", 600, 3)]),
        # 席番号を言わない運用（手番の順でアクターを決める）
        ("フォールド、フォールド、コール", [("fold", 0, None), ("fold", 0, None), ("call", 0, None)]),
        ("フォールド、ホールド", [("fold", 0, None), ("fold", 0, None)]),   # 店舗の実測（2 回目）
        ("フォールドフォールドコール", [("fold", 0, None), ("fold", 0, None), ("call", 0, None)]),
        ("チェック チェック チェック", [("check", 0, None)] * 3),
        ("レイズ 600、コール", [("raise", 600, None), ("call", 0, None)]),
        ("レイズ、600、コール", [("raise", 600, None), ("call", 0, None)]),
        ("コール、600にレイズ", [("call", 0, None), ("raise", 600, None)]),
        ("オールイン コール", [("allin", 0, None), ("call", 0, None)]),
    ])
    def test_actions_said_in_one_breath_are_separated(self, text, expected):
        assert _read(text) == expected

    @pytest.mark.parametrize("text, expected", [
        ("スリーベット 900", [("raise", 900, None)]),          # 「ベット」を含むが 1 アクション
        ("チェックレイズ 1200", [("raise", 1200, None)]),       # 1 人のレイズ
        ("シート3 シート5 チョップ", [("winner", 0, 3)]),       # 席が複数でもアクションは 1 つ
        ("ベッド 1200", [("bet", 1200, None)]),
        ("ホールド", [("fold", 0, None)]),
        ("テキサスホールデム", []),                            # 「ホールド」を含まない
        ("えーと", []),
    ])
    def test_single_actions_stay_single(self, text, expected):
        assert _read(text) == expected

    def test_positions_mark_the_next_action(self):
        events = parse_actions("BTN コール、SB フォールド")
        assert [(e.action, e.position) for e in events] == [("call", "BTN"), ("fold", "SB")]

    def test_each_part_keeps_its_own_words_and_no_review_flag(self):
        events = parse_actions("ちぇっく、こーる", confidence=0.7, utterance_start_ts=5.0)
        assert [(e.action, e.raw_text) for e in events] == [("check", "ちぇっく"), ("call", "こーる")]
        assert all(e.parse_flags == () and e.confidence == 0.7 and e.utterance_start_ts == 5.0
                   for e in events)

    def test_a_repetition_loop_is_not_split(self):
        # Whisper の繰り返し（9 回以上）は分けずに 1 件 + 要レビュー
        events = parse_actions("フォールド、" * 9)
        assert len(events) == 1 and "too_many_actions" in events[0].parse_flags

    def test_parse_action_still_returns_one(self):
        ev = parse_action("シート4 コール、シート1 フォールド")
        assert ev.action == "call" and "multi_action_keywords" in ev.parse_flags


class _FakeTranscriber:
    ready = True

    def __init__(self, text: str) -> None:
        self.text = text

    def transcribe_with_confidence(self, audio_bytes: bytes):
        return self.text, 0.8


def _thread(text: str = "x", **kwargs) -> tuple[queue.Queue, AudioThread]:
    q: queue.Queue = queue.Queue()
    return q, AudioThread(audio_queue=q, stop_event=threading.Event(),
                          transcriber=_FakeTranscriber(text), **kwargs)


class TestAudioThread:
    def test_one_utterance_can_become_several_actions_in_order(self):
        seen = []
        q, t = _thread("フォールド、フォールド、コール", on_transcript=seen.append)
        t._process_chunk(b"\x00\x00" * 1600, utterance_start_ts=1.0)   # noqa: SLF001
        assert [q.get_nowait().action for _ in range(3)] == ["fold", "fold", "call"]
        assert [e.action for e in seen[0].events] == ["fold", "fold", "call"]

    def _capture(self, t: AudioThread, voiced_chunks: int) -> None:
        loud = struct.pack("1024h", *([5000] * 1024))
        quiet = b"\x00" * 2048
        chunks = iter([quiet] * 2 + [loud] * voiced_chunks + [quiet] * 9 + [b""])
        t._capture_loop(lambda: next(chunks), 1024)   # noqa: SLF001

    def test_a_short_word_like_check_is_kept(self):
        # 「チェック」単独の有音は 0.2 秒前後（1024 サンプル = 64 ms のチャンク 3 つ）
        _, t = _thread()
        self._capture(t, 3)
        assert t.backlog() == 1

    def test_a_click_is_dropped_and_reported(self):
        dropped: list[float] = []
        _, t = _thread(on_dropped=dropped.append)
        self._capture(t, 2)      # 0.128 秒 < 0.15 秒
        assert t.backlog() == 0 and dropped == [pytest.approx(0.128)]

    def test_the_minimum_is_configurable(self):
        _, t = _thread(min_speech_sec=0.3)
        self._capture(t, 3)
        assert t.backlog() == 0

    def test_config_reaches_the_thread(self):
        thread = main._make_audio_thread(   # noqa: SLF001
            {"audio": {"min_speech_sec": 0.25}}, queue.Queue(), threading.Event())
        assert thread._min_speech_sec == 0.25   # noqa: SLF001


class TestTypedInputWaitsForSpeech:
    def test_waits_until_the_backlog_is_empty(self, capsys):
        remaining = iter([2, 2, 1, 0])
        last = {"n": 2}

        def backlog():
            last["n"] = next(remaining, 0)
            return last["n"]

        main._wait_for_backlog(SimpleNamespace(backlog=backlog))   # noqa: SLF001
        assert last["n"] == 0
        assert "聞き取った発話を先に反映しています" in capsys.readouterr().out

    def test_no_wait_without_audio(self, capsys):
        main._wait_for_backlog(None)   # noqa: SLF001
        main._wait_for_backlog(SimpleNamespace(backlog=lambda: 0))   # noqa: SLF001
        assert capsys.readouterr().out == ""

    def test_gives_up_after_the_timeout(self, capsys):
        main._wait_for_backlog(SimpleNamespace(backlog=lambda: 3), timeout_sec=0.2)   # noqa: SLF001
        assert "入力を先に反映します" in capsys.readouterr().out


def test_a_typed_line_with_several_actions_is_split(tmp_path, monkeypatch, capsys):
    cfg = {"audio": {"enabled": False}, "rfid": {"enabled": False},
           "engine": {"backend": "legacy"}, "table_state": {"enabled": False}}
    answers = iter(["3", "", "1000", "", "1000", "", "1000", "5", "10", "",
                    str(tmp_path / "logs"), "フォールド フォールド コール", "q"])
    monkeypatch.setattr("core.config.load_config", lambda path=None: cfg)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    main.run_cli()
    out = capsys.readouterr().out
    assert out.count("  → fold") == 2 and out.count("  → call") == 1
