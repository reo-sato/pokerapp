"""rfid/event_builder.py

RFID 受信 transport（HTTP / serial）が共有する、受信ペイロード → RFIDEvent 変換の
単一ロジック。tag 正規化・reader_id 検証・card lookup・timestamp 解析・RFIDEvent 生成を
ここに集約し、各 transport（front-end）に validation を複製しない。

受信ペイロード契約（transport 非依存, ADR-0006）:
    {"reader_id": "seat_3", "tag_id": "04A1B2C3D4E5F6", "timestamp": "2026-..."}
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Optional

from core.events import RFIDEvent
from rfid.card_master import CardMaster, normalize_tag_id

logger = logging.getLogger(__name__)


def build_rfid_event(
    data: dict,
    reader_configs: dict[str, dict],
    card_master: CardMaster,
) -> tuple[Optional[RFIDEvent], str]:
    """受信済み dict ペイロードから RFIDEvent を生成する。

    JSON のパースは呼び出し側（transport）の責務。ここは「dict が来た後」を担う。

    Returns:
        (event, status):
          - 正常: (RFIDEvent, "ok")
          - 未知の reader_id: (None, "unknown reader_id")  ← event は生成しない
        未登録 tag は event を生成する（card="" で needs_review 相当）。
    """
    reader_id: str = data.get("reader_id", "")
    tag_id_raw: str = data.get("tag_id", "")
    timestamp_str: str = data.get("timestamp", "")

    # タグ ID 正規化（UID 長は仮定しない: ISO14443A 4/7 byte・ISO15693 8 byte 両対応）
    try:
        tag_id = normalize_tag_id(tag_id_raw)
    except Exception:
        tag_id = tag_id_raw.upper()

    # reader_id 検証
    reader_cfg = reader_configs.get(reader_id)
    if reader_cfg is None:
        logger.warning(
            "build_rfid_event: unknown reader_id=%r (tag=%s)", reader_id, tag_id
        )
        return None, "unknown reader_id"

    # カードルックアップ
    card = card_master.lookup(tag_id)
    if not card:
        logger.warning(
            "build_rfid_event: unregistered tag %s (reader=%s) — needs_review",
            tag_id, reader_id,
        )

    ts = _parse_timestamp(timestamp_str)

    role: str = reader_cfg.get("role", "seat")
    seat: Optional[int] = reader_cfg.get("seat") if role == "seat" else None
    board_index: Optional[int] = reader_cfg.get("index") if role == "board" else None

    event = RFIDEvent(
        tag_id=tag_id,
        card=card,
        reader_id=reader_id,
        role=role,
        seat=seat,
        timestamp=ts,
        raw_tag_id=tag_id_raw,
        board_index=board_index,
    )
    return event, "ok"


def _parse_timestamp(ts_str: str) -> float:
    """ISO 8601 文字列を UNIX タイムスタンプに変換する。失敗時は現在時刻。"""
    if not ts_str:
        return time.time()
    try:
        dt = datetime.fromisoformat(ts_str)
        # タイムゾーン情報がなければローカル時刻として扱う
        if dt.tzinfo is None:
            return dt.timestamp()
        return dt.astimezone(timezone.utc).timestamp()
    except ValueError:
        logger.debug("Could not parse timestamp %r, using current time", ts_str)
        return time.time()
