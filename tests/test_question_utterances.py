"""tests/test_question_utterances.py

確認型の発話（仕様 FR-16/17, §7 のディーラー発話プロトコル）: 「コールですか？」「レイズ 2400 でよろしいですか？」は
ディーラーがプレイヤーに確かめている言葉で、アクションにしない。確認のあとに言う確定の発話（「コール」）だけを
読む（ADR-0056 が「未実装」と記録していた FR-15/16/17 の踏襲）。
"""
from __future__ import annotations

import json
import queue
import threading

import pytest

from audio.recognizer import is_question, parse_actions
from audio.recorder import QUESTION_NOTE, AudioThread, Transcript
from output.transcript_log import TranscriptLog


@pytest.mark.parametrize("text, question", [
    ("コールですか？", True),
    ("コールですか", True),
    ("レイズ 2400 でよろしいですか？", True),
    ("コール？", True),
    ("オールインでしょうか。", True),
    ("フォールドですよね", True),
    ("チェックしますか", True),
    ("コールです", False),
    ("コール。", False),
    ("コール", False),
    ("チェック、チェック", False),
    ("レイズ 2400", False),
    ("ハンド終了", False),
])
def test_question_form(text, question):
    assert is_question(text) is question
    assert (parse_actions(text) == []) is question or not question


def test_a_question_is_not_an_action_but_the_statement_is():
    assert parse_actions("コールですか？") == []
    (event,) = parse_actions("コール")
    assert event.action == "call"


class _Fake:
    ready = True

    def __init__(self, text: str) -> None:
        self.text = text

    def transcribe_with_confidence(self, audio_bytes: bytes):
        return self.text, 0.8


def test_the_recorder_reports_it_and_queues_nothing():
    q: queue.Queue = queue.Queue()
    seen: list[Transcript] = []
    thread = AudioThread(audio_queue=q, stop_event=threading.Event(), transcriber=_Fake("コールですか？"),
                         on_transcript=seen.append)
    thread._process_chunk(b"\x10\x00" * 16000, utterance_start_ts=5.0)   # noqa: SLF001
    (t,) = seen
    assert q.empty() and t.question and not t.noise and t.events == ()


def test_shown_and_logged(capsys, tmp_path):
    from main import _print_transcript

    t = Transcript(text="コールですか？", confidence=0.8, events=(), audio_sec=1.0, infer_sec=2.0,
                   utterance_start_ts=1.0, heard_at=4.0, question=True)
    _print_transcript(t)
    assert QUESTION_NOTE in capsys.readouterr().out
    log = TranscriptLog(tmp_path / "s.transcripts.jsonl")
    log.write(t)
    (line,) = [json.loads(x) for x in log.path.read_text(encoding="utf-8").splitlines()]
    assert line["question"] is True and line["events"] == []
