"""cloud/records.py — 受け取ったレコードの検査と、種類ごとの merge の規則（ADR-0067 D2）。

- 検査は契約の schema（`docs/contracts/schemas/`）。合わないレコードは断る（理由を返す）。
- merge は `core/sync.py` の解決関数を使い回す。**店舗 PC から届いたもの（incoming）を先に渡す**
  （label のように「先に渡した方を優先」する項目で、店舗 PC の変更が写しに反映されるように）。
- 店舗 PC だけが書き、中身が変わり得るもの（ハンド・店舗の設定）は**送り手の番号（seq）が新しい方**を採る。
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Callable, Optional

from core import sync
from core.cloud_payload import (
    HOLE_CARD_VISIBILITY,
    KINDS,
    PRIVATE_ACTION_FIELDS,
    STORE_SETTING_KEYS,
    RecordKeyError,
    record_key,
    strip_hand,
)

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "docs" / "contracts" / "schemas"

# 種類 → merge の方法
#   resolve: core/sync の解決関数（incoming, existing）
#   union:   append-only（既にあれば既存のまま）
#   seq:     送り手の番号が新しい（同じも含む）なら置き換える
# sessions は seq: 店舗 PC が席の割り当て込みの全体をいつも送るので、新しい方がそのまま正しい（設定を
# 「店の設定に従う」に戻したときに、項目が無いことも写しに伝わる。ADR-0067 追記 4）。
MERGE_MODE: dict[str, str] = {
    "players": "resolve",
    "sessions": "seq",
    "ledger_entries": "union",
    "point_entries": "union",
    "settlements": "resolve",
    "order_requests": "resolve",
    "hands": "seq",
    "corrections": "union",
    "store_settings": "seq",
}

_RESOLVERS: dict[str, Callable[[dict, dict], dict]] = {
    "players": sync._resolve_player,
    "settlements": sync._resolve_settlement,
    "order_requests": sync._resolve_order,
}

_SCHEMA_FOR: dict[str, str] = {
    "players": "player",
    "ledger_entries": "ledger_entry",
    "point_entries": "point_ledger_entry",
    "settlements": "session_settlement",
    "order_requests": "order_request",
    "hands": "hand",
}


@lru_cache(maxsize=None)
def _validator(name: str):
    import jsonschema

    schema = json.loads((SCHEMA_DIR / f"{name}.schema.json").read_text(encoding="utf-8"))
    return jsonschema.Draft202012Validator(schema)


def _schema_error(name: str, rec: dict) -> Optional[str]:
    err = next(iter(_validator(name).iter_errors(rec)), None)
    if err is None:
        return None
    where = "/".join(str(p) for p in err.absolute_path) or "(全体)"
    return f"{name} の形に合いません（{where}: {err.message[:200]}）"


def _validate_session(rec: dict) -> Optional[str]:
    hands = rec.get("hands", {})
    top = {k: v for k, v in rec.items() if k != "hands"}
    err = _schema_error("session", top)
    if err:
        return err
    if not isinstance(hands, dict):
        return "hands が辞書ではありません"
    for hand_key, hand in hands.items():
        try:
            hand_id = int(hand_key)
        except (TypeError, ValueError):
            return f"hands のキーが整数ではありません: {hand_key!r}"
        if not isinstance(hand, dict) or not isinstance(hand.get("seats", []), list):
            return f"hands[{hand_key}] の形が違います"
        for seat in hand.get("seats", []):
            if not isinstance(seat, dict):
                return f"hands[{hand_key}].seats の形が違います"
            full = {"session_id": rec["session_id"], "hand_id": hand_id, **seat}
            err = _schema_error("seat_assignment", full)
            if err:
                return err
    return None


def _validate_correction(rec: dict) -> Optional[str]:
    from core.hand_correction import HandCorrection

    try:
        HandCorrection.from_dict(rec)
    except (KeyError, TypeError, ValueError) as exc:
        return f"訂正の形が違います: {exc}"
    if not isinstance(rec.get("hand_id"), int) or not isinstance(rec.get("session_id"), str):
        return "訂正の session_id / hand_id の形が違います"
    return None


def validate(kind: str, rec: object) -> tuple[Optional[str], Optional[str]]:
    """(キー, 断る理由)。理由が None なら受け付ける。"""
    if kind not in KINDS:
        return None, f"知らない種類です: {kind}"
    if not isinstance(rec, dict):
        return None, "レコードが辞書ではありません"
    try:
        key = record_key(kind, rec)
    except RecordKeyError as exc:
        return None, str(exc)
    if kind == "sessions":
        return key, _validate_session(rec)
    if kind == "corrections":
        return key, _validate_correction(rec)
    return key, _schema_error(_SCHEMA_FOR[kind], rec)


def prepare(kind: str, rec: dict) -> dict:
    """保存する前の整え（念のため受け手でもハンドの聞き取った文・監査項目を落とす, D1）。"""
    if kind == "hands":
        return strip_hand(rec)
    return rec


def validate_settings(settings: object) -> Optional[str]:
    if not isinstance(settings, dict):
        return "store_settings が辞書ではありません"
    unknown = set(settings) - set(STORE_SETTING_KEYS)
    if unknown:
        return f"知らない設定です: {sorted(unknown)}"
    if "hole_cards" in settings and settings["hole_cards"] not in HOLE_CARD_VISIBILITY:
        return f"hole_cards は {list(HOLE_CARD_VISIBILITY)} のどれかです"
    return None


def merge(kind: str, existing: Optional[dict], existing_seq: Optional[int],
          incoming: dict, seq: int) -> Optional[dict]:
    """保存する本文。None なら何もしない（古い・append-only で既にある）。"""
    if existing is None:
        return incoming
    mode = MERGE_MODE[kind]
    if mode == "union":
        return None
    if mode == "seq":
        return incoming if existing_seq is None or seq >= existing_seq else None
    return _RESOLVERS[kind](incoming, existing)


def index_columns(kind: str, rec: dict) -> tuple[Optional[str], Optional[str]]:
    """検索用の列 (player_id, session_id)。"""
    player_id = rec.get("player_id") if kind != "hands" else None
    session_id = rec.get("session_id")
    return (str(player_id) if player_id else None, str(session_id) if session_id else None)


__all__ = ["MERGE_MODE", "PRIVATE_ACTION_FIELDS", "validate", "validate_settings", "prepare", "merge",
           "index_columns"]
