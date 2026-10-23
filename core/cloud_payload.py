"""core/cloud_payload.py — 店舗 PC → クラウドの同期で送るレコードの形（ADR-0067）。

送り手（店舗 PC, 2b）と受け手（クラウド `cloud/`, 2a）の両方が使う。

- 送る種類と、種類ごとのキー（クラウドの中では「店舗 ID + このキー」が主キー, D3）
- 送る前に落とす項目（聞き取った文と監査用の項目, D1）
- レコードの指紋（変わったものだけを送るため, D2）
"""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

PROTOCOL = 1

# 送る種類（`store_settings` は種類ではなく本文の別の項目）
KINDS: tuple[str, ...] = (
    "players",
    "sessions",
    "ledger_entries",
    "point_entries",
    "settlements",
    "order_requests",
    "hands",
    "corrections",
)

# ハンドの各アクションから、送る前に落とす項目。クラウドの画面では使わない（持たないのが一番安全, D1）。
PRIVATE_ACTION_FIELDS: tuple[str, ...] = (
    "raw_text",        # 聞き取った文（ADR-0060）
    "corrected_from",  # 以下は監査用（ADR-0047 G2）
    "asr_confidence",
    "apply_ok",
    "actor_source",
    "reason",
)

# ホールカードの公開範囲（D6）
HOLE_CARD_VISIBILITY: tuple[str, ...] = ("all", "own_and_shown")
STORE_SETTING_KEYS: tuple[str, ...] = ("hole_cards",)


class RecordKeyError(ValueError):
    """キーが取れないレコード。"""


def record_key(kind: str, rec: dict) -> str:
    """種類ごとのキー（文字列）。"""
    try:
        if kind == "players":
            return str(rec["player_id"])
        if kind == "sessions":
            return str(rec["session_id"])
        if kind in ("ledger_entries", "point_entries"):
            return str(rec["entry_id"])
        if kind == "settlements":
            return f"{rec['session_id']}:{rec['player_id']}"
        if kind == "order_requests":
            return str(rec["request_id"])
        if kind == "hands":
            hand_id = rec["hand_id"]
            if isinstance(hand_id, bool) or not isinstance(hand_id, int):
                raise RecordKeyError("hand_id が整数ではありません")
            return f"{rec['session_id']}:{hand_id}"
        if kind == "corrections":
            return str(rec["correction_id"])
    except (KeyError, TypeError) as exc:
        raise RecordKeyError(f"キーの項目がありません: {exc}") from exc
    raise RecordKeyError(f"知らない種類です: {kind}")


def strip_hand(hand: dict) -> dict:
    """送る前にハンドから聞き取った文と監査用の項目を落とした写し。"""
    out = copy.deepcopy(hand)
    for action in out.get("actions") or []:
        if isinstance(action, dict):
            for field in PRIVATE_ACTION_FIELDS:
                action.pop(field, None)
    return out


def canonical_json(rec: Any) -> str:
    return json.dumps(rec, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def fingerprint(rec: Any) -> str:
    """レコードの指紋（sha256）。キーの順番に左右されない。"""
    return hashlib.sha256(canonical_json(rec).encode("utf-8")).hexdigest()
