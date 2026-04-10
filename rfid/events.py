from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class RFIDEventType(str, Enum):
    """ESP32 から送信される RFID イベント種別（spec.md 6.3）。"""
    PRESENT = "present"   # カード検知
    ABSENT  = "absent"    # カード消失


@dataclass
class RFIDEvent:
    """ESP32 → Python HTTP POST で受信する生 RFID イベント（spec.md 6.3）。

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


@dataclass
class RFIDCardEvent:
    """CardMaster 解決済みの RFID カードイベント。RFIDThread が event_queue へ送出する。

    seat リーダー  → seat が席番号、board_index は None
    board リーダー → board_index が 1–5 の位置番号、seat は None
    """
    seat:        Optional[int]   # role="seat" の席番号（board の場合は None）
    board_index: Optional[int]   # role="board" のボード位置 1–5（seat の場合は None）
    card_code:   str             # "Ah", "Kd" 等（CardMaster 解決済み）
    event_type:  str             # "present" | "absent"
    timestamp:   str             # ISO 8601
    reader_id:   str = ""        # 発信元リーダー ID
    tag_id:      str = ""        # 生タグ ID
