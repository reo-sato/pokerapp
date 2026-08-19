"""output/event_recorder.py

R1 (rules-aware reconstruction, ADR-0010): 生センサーイベントを append-only の
JSON Lines sidecar (logs/{session_id}.events.jsonl) に記録する。

- **解釈は行わない**（挙動不変）。`logs/*.json` / PHH には一切触れない別 sidecar。
- camera frame は除外する（重く非決定的）。
- 記録失敗で再構築を止めない（warning に留める）。

schema: docs/contracts/schemas/reconstruction_event.schema.json
設計: docs/contracts/event-replay.md §2-3 / ADR-0010。
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Union

from core.events import AudioEvent, CameraEvent, RFIDEvent

logger = logging.getLogger(__name__)

RecordableEvent = Union[AudioEvent, RFIDEvent, CameraEvent]


def event_to_envelope(event: RecordableEvent) -> dict:
    """AudioEvent / RFIDEvent / CameraEvent を reconstruction_event envelope (dict) に変換する。

    `CameraEvent.frame` は除外する。`type` で判別する discriminated union
    (docs/contracts/schemas/reconstruction_event.schema.json)。
    """
    if isinstance(event, AudioEvent):
        envelope = {
            "type": "audio",
            "timestamp": event.timestamp,
            "action": event.action,
            "amount": event.amount,
            "raw_text": event.raw_text,
            "seat": event.seat,              # additive (R3/R4)。未設定なら null。
            "confidence": event.confidence,  # additive (Whisper 信頼度)。未設定なら null。
        }
        # additive (ADR-A/B): 既定値のときは省略し、旧 replay 実装でもそのまま読める形を保つ。
        if event.utterance_start_ts is not None:
            envelope["utterance_start_ts"] = event.utterance_start_ts
        if event.parse_flags:
            envelope["parse_flags"] = list(event.parse_flags)
        return envelope
    if isinstance(event, RFIDEvent):
        return {
            "type": "rfid",
            "timestamp": event.timestamp,
            "tag_id": event.tag_id,
            "card": event.card,
            "reader_id": event.reader_id,
            "role": event.role,
            "seat": event.seat,
            "board_index": event.board_index,
            "raw_tag_id": event.raw_tag_id,
        }
    if isinstance(event, CameraEvent):
        return {
            "type": "camera",
            "timestamp": event.timestamp,
            "seat": event.seat,
        }
    raise TypeError(f"Unsupported event type for recording: {type(event)!r}")


class EventRecorder:
    """生センサーイベントを JSON Lines で追記する (append-only, 非破壊, スレッド安全)。

    1 イベント = 1 行。ハンド単位でバッファせず即追記する
    (CLAUDE.md エラーハンドリング方針: バッファせずディスクへ)。
    """

    def __init__(self, path: Union[str, Path]) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def record(self, event: RecordableEvent) -> None:
        """1 イベントを 1 行追記する。未対応型・I/O 失敗は warning に留め例外を投げない。"""
        try:
            envelope = event_to_envelope(event)
        except TypeError:
            logger.warning("Skip recording unsupported event type: %r", type(event))
            return
        line = json.dumps(envelope, ensure_ascii=False)
        try:
            with self._lock:
                with self._path.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except OSError:
            logger.exception("Failed to record event to %s", self._path)
