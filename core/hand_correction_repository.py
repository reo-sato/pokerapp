"""core/hand_correction_repository.py

ADR-0036 (B4): ハンド訂正の append-only ストア（`hand_corrections.json`, node-local）。

元 hand log を mutate せず、訂正レコードをここに追記する。read 側（viewer API）は
`apply_hand_corrections` で元ハンドに重ねて返す。atomic + fsync 書き込み（B2）。
"""
from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime
from pathlib import Path

from core.atomic_io import atomic_write_json, read_json_file
from core.hand_correction import (
    ACTION_FIELDS,
    HAND_FIELDS,
    HandCorrection,
)

logger = logging.getLogger(__name__)

_DEFAULT_DB = Path(__file__).parent.parent / "hand_corrections.json"


class HandCorrectionError(Exception):
    """訂正の不正（code=invalid_correction）。"""


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class HandCorrectionRepository:
    """append-only な訂正ストア。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self._lock = threading.RLock()
        self._path = Path(path) if path is not None else _DEFAULT_DB
        self._corrections: list[HandCorrection] = []
        self._load()

    @property
    def path(self) -> Path:
        return self._path

    def _load(self) -> None:
        data = read_json_file(self._path)  # 破損は退避（B7）
        if data is None:
            return
        for raw in data.get("corrections", []):
            try:
                self._corrections.append(HandCorrection.from_dict(raw))
            except (KeyError, TypeError):
                logger.warning("Skipping malformed hand correction: %r", raw)

    def _flush(self) -> None:
        data = {"corrections": [c.to_dict() for c in self._corrections]}
        try:
            atomic_write_json(self._path, data)
        except OSError:
            logger.exception("Failed to write hand corrections: %s", self._path)

    def reload(self) -> None:
        with self._lock:
            self._corrections.clear()
            self._load()

    def add_correction(
        self,
        session_id: str,
        hand_id: int,
        field: str,
        new_value: object,
        *,
        action_index: int | None = None,
        corrected_by: str = "staff",
        note: str | None = None,
    ) -> HandCorrection:
        """訂正を 1 件追記する（append-only）。field と value の基本 validation を行う。

        action_index の hand 内範囲チェックは hand を読める API 層が行う（ここは hand を持たない）。
        """
        with self._lock:
            self._validate(field, new_value, action_index)
            c = HandCorrection(
                correction_id=uuid.uuid4().hex,
                session_id=session_id,
                hand_id=int(hand_id),
                action_index=action_index,
                field=field,
                new_value=new_value,
                corrected_by=corrected_by or "staff",
                corrected_at=_now_iso(),
                note=note,
            )
            self._corrections.append(c)
            self._flush()
            logger.info(
                "Hand correction %s: session=%s hand=%s idx=%s %s=%r by=%s",
                c.correction_id, session_id, hand_id, action_index, field, new_value, c.corrected_by,
            )
            return c

    @staticmethod
    def _validate(field: str, new_value: object, action_index: int | None) -> None:
        if action_index is None:
            if field not in HAND_FIELDS:
                raise HandCorrectionError(
                    f"hand レベル訂正の field は {HAND_FIELDS} のいずれかです: {field!r}"
                )
            if field == "winner_seat" and (
                not isinstance(new_value, int) or isinstance(new_value, bool)
            ):
                raise HandCorrectionError("winner_seat は整数である必要があります。")
            return
        if not isinstance(action_index, int) or isinstance(action_index, bool) or action_index < 0:
            raise HandCorrectionError("action_index は 0 以上の整数である必要があります。")
        if field not in ACTION_FIELDS:
            raise HandCorrectionError(
                f"アクション訂正の field は {ACTION_FIELDS} のいずれかです: {field!r}"
            )
        if field == "amount" and (
            not isinstance(new_value, int) or isinstance(new_value, bool) or new_value < 0
        ):
            raise HandCorrectionError("amount は 0 以上の整数である必要があります。")
        if field == "action" and (not isinstance(new_value, str) or not new_value.strip()):
            raise HandCorrectionError("action は空でない文字列である必要があります。")

    def list_for_hand(self, session_id: str, hand_id: int) -> list[HandCorrection]:
        with self._lock:
            return [
                c for c in self._corrections
                if c.session_id == session_id and c.hand_id == int(hand_id)
            ]

    def list_for_session(self, session_id: str) -> list[HandCorrection]:
        with self._lock:
            return [c for c in self._corrections if c.session_id == session_id]
