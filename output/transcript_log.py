"""output/transcript_log.py

聞き取った発話を 1 行ずつ JSON で残す（`logs/{session_id}.transcripts.jsonl`）。

アクションとして読めなかった発話・雑音として捨てた発話も含む。`events.jsonl` はアクションになった発話
しか残さないので、聞き違いの原因（どう聞こえたか・どのくらい遅れたか・どの音声か）を店舗のデータで
追えるようにする（設計監査 2026-09-25）。書けなくても聞き取りは止めない。
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

logger = logging.getLogger(__name__)


class TranscriptLog:
    """`audio.recorder.Transcript` を JSON Lines で追記する（スレッド安全）。"""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def write(self, transcript) -> None:
        line = {
            "utterance_start_ts": transcript.utterance_start_ts,
            "heard_at": transcript.heard_at,
            "audio_sec": round(transcript.audio_sec, 3),
            "infer_sec": round(transcript.infer_sec, 3),
            "text": transcript.text,
            "confidence": transcript.confidence,
            "noise": transcript.noise,
            "no_speech": getattr(transcript, "no_speech", False),
            "question": getattr(transcript, "question", False),
            "audio_file": getattr(transcript, "audio_file", None),
            "events": [
                {
                    "action": e.action, "amount": e.amount, "seat": e.seat,
                    "position": e.position, "parse_flags": list(e.parse_flags),
                }
                for e in transcript.events
            ],
        }
        try:
            with self._lock:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(line, ensure_ascii=False) + "\n")
        except OSError:
            logger.exception("聞き取りの記録を書けませんでした: %s", self._path)
