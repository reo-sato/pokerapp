from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class RFIDEventType(str, Enum):
    """ESP32 から送信される RFID イベント種別（spec.md 6.3）。"""
    PRESENT = "present"   # カード検知
    ABSENT  = "absent"    # カード消失


@dataclass
class RFIDEvent:
    """ESP32 → Python HTTP POST で受信する RFID イベント（spec.md 6.3）。

    JSON 形式:
        {
          "reader_id": "seat_3",
          "tag_id": "04A1B2C3D4E5F6",
          "timestamp": "2026-04-09T19:23:04.980",
          "event_type": "present"
        }
    """
    reader_id:  str   # "seat_1"–"seat_9" / "board_1"–"board_5"
    tag_id:     str   # 14桁HEX文字列（例: "04A1B2C3D4E5F6"）
    timestamp:  str   # ISO 8601 形式
    event_type: str   # "present" | "absent"
