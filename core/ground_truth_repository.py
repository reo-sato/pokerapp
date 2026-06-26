"""core/ground_truth_repository.py

ADR-0043: Phase A 計測のための ground truth ストア（LWW、session ファイル単位）。

各 session の GT は `logs/{session_id}.ground_truth.json` に保存される。
`(session_id, hand_id)` キーで上書き保存（LWW）。`tools/measure_capture_accuracy.py` が
直接読む形式と互換（measurement-plan §2.2）。

write 所有プロセス（`--ledger` / `viewer_api.enabled`）でのみ書き込む前提。
atomic + fsync 書き込み（B2）。
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

from core.atomic_io import atomic_write_json, read_json_file
from core.ground_truth import GroundTruthHand

logger = logging.getLogger(__name__)


def _now_iso_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class GroundTruthRepository:
    """LWW な per-session ground truth ストア。"""

    def __init__(self, log_dir: str | Path) -> None:
        self._lock = threading.RLock()
        self._log_dir = Path(log_dir)
        self._cache: dict[str, list[GroundTruthHand]] = {}

    @property
    def log_dir(self) -> Path:
        return self._log_dir

    def path_for(self, session_id: str) -> Path:
        return self._log_dir / f"{session_id}.ground_truth.json"

    def reload(self) -> None:
        with self._lock:
            self._cache.clear()

    def _load_session(self, session_id: str) -> list[GroundTruthHand]:
        if session_id in self._cache:
            return self._cache[session_id]
        path = self.path_for(session_id)
        data = read_json_file(path)
        hands: list[GroundTruthHand] = []
        if isinstance(data, dict):
            for raw in data.get("hands") or []:
                if not isinstance(raw, dict):
                    continue
                try:
                    hands.append(GroundTruthHand.from_dict(raw))
                except (KeyError, TypeError, ValueError):
                    logger.warning(
                        "Skipping malformed ground truth hand in %s: %r", path, raw
                    )
        self._cache[session_id] = hands
        return hands

    def _flush_session(self, session_id: str) -> None:
        hands = self._cache.get(session_id, [])
        if not hands:
            return
        latest = max(hands, key=lambda h: h.annotated_at)
        payload = {
            "session_id": session_id,
            "annotator": latest.annotator,
            "annotated_at": latest.annotated_at,
            "source": "manual",
            "hands": [h.to_dict() for h in sorted(hands, key=lambda h: h.hand_id)],
        }
        self._log_dir.mkdir(parents=True, exist_ok=True)
        try:
            atomic_write_json(self.path_for(session_id), payload)
        except OSError:
            logger.exception(
                "Failed to write ground truth: %s", self.path_for(session_id)
            )

    def upsert(
        self,
        session_id: str,
        hand_id: int,
        hand: dict,
        *,
        annotator: str,
        source: str,
    ) -> GroundTruthHand:
        """ground truth を 1 件 upsert する（LWW）。

        `hand` は GT の本体（board / actions / players / winner_seat / notes 等）。
        `(session_id, hand_id)` 既存があれば上書きする。
        """
        with self._lock:
            hands = self._load_session(session_id)
            entry = GroundTruthHand(
                hand_id=int(hand_id),
                annotator=annotator or "staff",
                annotated_at=_now_iso_utc(),
                source=source,
                hand=hand,
            )
            self._cache[session_id] = [
                h for h in hands if h.hand_id != int(hand_id)
            ] + [entry]
            self._flush_session(session_id)
            logger.info(
                "Ground truth upsert: session=%s hand=%s source=%s by=%s",
                session_id, hand_id, source, entry.annotator,
            )
            return entry

    def get(self, session_id: str, hand_id: int) -> GroundTruthHand | None:
        with self._lock:
            for h in self._load_session(session_id):
                if h.hand_id == int(hand_id):
                    return h
            return None

    def list_for_session(self, session_id: str) -> list[GroundTruthHand]:
        with self._lock:
            return list(self._load_session(session_id))
