"""cloud/ingest.py — 店舗 PC からの push を受け取って保存する（ADR-0067 D2）。

本文:
    {"protocol": 1, "seq": <送り手の番号>, "store_pc": {版の情報}, "records": {種類: [レコード…]},
     "store_settings": {...}}   # store_settings は任意

- 種類・形の合わないレコードは断る（理由を返す）。ほかのレコードは受け付ける（1 件の不備で全部を止めない）。
- 1 回の件数の上限 `MAX_RECORDS`（本文の大きさの上限は受け口の側, `cloud/app.py`）。
"""
from __future__ import annotations

from typing import Optional

from cloud import records
from cloud.storage import ADDED, CHANGED, SAME, STALE, Item, Storage
from core.cloud_payload import KINDS, PROTOCOL

MAX_RECORDS = 5000
SETTINGS_KEY = "settings"


class PushError(Exception):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def _check_envelope(payload: object) -> tuple[int, dict, Optional[dict]]:
    if not isinstance(payload, dict):
        raise PushError("invalid_payload", "本文が JSON のオブジェクトではありません")
    protocol = payload.get("protocol")
    if protocol != PROTOCOL:
        # 新しい店舗 PC が先に来たとき（クラウドがまだ古い）に分かるようにする
        raise PushError("unsupported_protocol",
                        f"protocol {protocol!r} は受け付けられません（クラウドは {PROTOCOL}）", status=409)
    seq = payload.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq <= 0 or seq >= 2**63:
        raise PushError("invalid_payload", "seq は正の整数です")
    recs = payload.get("records", {})
    if not isinstance(recs, dict):
        raise PushError("invalid_payload", "records が辞書ではありません")
    unknown = set(recs) - set(KINDS)
    if unknown:
        raise PushError("invalid_payload", f"知らない種類です: {sorted(unknown)}")
    total = 0
    for kind, items in recs.items():
        if not isinstance(items, list):
            raise PushError("invalid_payload", f"records.{kind} が配列ではありません")
        total += len(items)
    if total > MAX_RECORDS:
        raise PushError("too_many_records", f"1 回に送れるのは {MAX_RECORDS} 件までです（{total} 件）", status=413)
    settings = payload.get("store_settings")
    if settings is not None:
        err = records.validate_settings(settings)
        if err:
            raise PushError("invalid_payload", err)
    return seq, recs, settings


def ingest_push(storage: Storage, store_id: str, payload: object) -> dict:
    """push を保存し、種類ごとの件数・断ったレコード・この店舗の最大の seq を返す。"""
    seq, recs, settings = _check_envelope(payload)
    items: list[Item] = []
    rejected: list[dict] = []
    for kind in KINDS:                      # 種類の順番を固定（結果を決定的に）
        for rec in recs.get(kind, []):
            key, reason = records.validate(kind, rec)
            if reason is not None:
                rejected.append({"kind": kind, "key": key, "reason": reason})
                continue
            items.append((kind, key, records.prepare(kind, rec)))
    if settings is not None:
        items.append(("store_settings", SETTINGS_KEY, dict(settings)))
    outcomes = storage.apply(store_id, items, seq)
    results: dict[str, dict[str, int]] = {}
    for (kind, _, _), outcome in zip(items, outcomes):
        counts = results.setdefault(kind, {ADDED: 0, CHANGED: 0, SAME: 0, STALE: 0})
        counts[outcome] += 1
    return {"seq": seq, "results": results, "rejected": rejected, "max_seq": storage.max_seq(store_id)}
