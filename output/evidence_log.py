"""output/evidence_log.py

セッション中の raw 観測イベント (AudioEvent / RFIDEvent / CameraEvent) を
append-only な JSONL に書き出すロガー。

ベイズ推定レイヤ (v6.0+) の「観測ログ収集 (Phase 0 / B0)」用に整備された。
将来のオフライン学習 (B2/B3/B6) の入力資料として使う。

ハンドサマリーを書く ``output/json_writer.py`` とは完全に分離する:
  - ``logs/<session_id>.json``         : 確定 ActionRecord (PHH/GUI が読む)
  - ``logs/evidence_<session_id>.jsonl``: raw 観測 (PHH/GUI は読まない)
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

from core.events import AudioEvent, CameraEvent, RFIDEvent

logger = logging.getLogger(__name__)


def _audio_payload(event: AudioEvent) -> dict[str, Any]:
    return {
        "action": event.action,
        "amount": event.amount,
        "raw_text": event.raw_text,
        "t_end": event.t_end,
        "alternatives": [
            {
                "text": a.text,
                "confidence": a.confidence,
                "words": [
                    {"word": w.word, "start": w.start, "end": w.end, "conf": w.confidence}
                    for w in a.words
                ],
            }
            for a in event.alternatives
        ],
        "word_timestamps": [
            {"word": w.word, "start": w.start, "end": w.end, "conf": w.confidence}
            for w in event.word_timestamps
        ],
    }


def _rfid_payload(event: RFIDEvent) -> dict[str, Any]:
    return {
        "tag_id": event.tag_id,
        "card": event.card,
        "reader_id": event.reader_id,
        "role": event.role,
        "seat": event.seat,
        "raw_tag_id": event.raw_tag_id,
        "board_index": event.board_index,
        "t_end": event.t_end,
    }


def _camera_payload(event: CameraEvent) -> dict[str, Any]:
    return {"seat": event.seat}


class EvidenceLogWriter:
    """raw 観測イベントを ``logs/evidence_<session>.jsonl`` に append-only で記録する。

    line-buffered で書き込むので、プロセスが SIGKILL されてもハンド進行中の観測は失われない。
    書き込み失敗時は warning ログを出して継続する (IntegrationThread を巻き込まない)。
    """

    def __init__(self, log_dir: str | Path, session_id: str) -> None:
        self._session_id = session_id
        self._path = Path(log_dir) / f"evidence_{session_id}.jsonl"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            # buffering=1 = line-buffered (テキストモード時の唯一の正しい指定)
            self._fp = open(self._path, "a", buffering=1, encoding="utf-8")
            logger.info("EvidenceLogWriter opened: %s", self._path)
        except OSError:
            logger.exception("Could not open evidence log: %s", self._path)
            self._fp = None

    @property
    def path(self) -> Path:
        return self._path

    def _write_line(self, record: dict[str, Any]) -> None:
        if self._fp is None:
            return
        try:
            self._fp.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except (OSError, TypeError):
            logger.exception("Failed to write evidence log line")

    def write_audio(self, event: AudioEvent, extra: Optional[dict[str, Any]] = None) -> None:
        record: dict[str, Any] = {
            "ts": event.timestamp,
            "kind": "audio",
            **_audio_payload(event),
        }
        if extra:
            record.update(extra)
        self._write_line(record)

    def write_rfid(self, event: RFIDEvent, extra: Optional[dict[str, Any]] = None) -> None:
        record: dict[str, Any] = {
            "ts": event.timestamp,
            "kind": "rfid",
            **_rfid_payload(event),
        }
        if extra:
            record.update(extra)
        self._write_line(record)

    def write_camera(self, event: CameraEvent, extra: Optional[dict[str, Any]] = None) -> None:
        record: dict[str, Any] = {
            "ts": event.timestamp,
            "kind": "camera",
            **_camera_payload(event),
        }
        if extra:
            record.update(extra)
        self._write_line(record)

    def close(self) -> None:
        if self._fp is None:
            return
        try:
            self._fp.close()
        finally:
            self._fp = None
