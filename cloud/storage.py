"""cloud/storage.py — クラウドの保存の約束（`Storage`）と、メモリ版（テスト・手元の確認用）。

本番は `cloud/postgres.py` の `PostgresStorage`（Supabase の Postgres, ADR-0067 D7）。どちらも同じテストを通す
（`tests/test_cloud_storage.py`）。

レコードはすべて「店舗 ID + 種類 + キー」で持つ（D3。別の店舗が同じ ID を送っても混ざらない）。
"""
from __future__ import annotations

import copy
import threading
import uuid
from datetime import datetime, timezone
from typing import Optional, Protocol

from cloud import records

# apply の結果
ADDED, CHANGED, SAME, STALE = "added", "changed", "same", "stale"

Item = tuple[str, str, dict]   # (種類, キー, 本文)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StoreNotFound(Exception):
    pass


class Storage(Protocol):
    def migrate(self) -> None: ...
    def ping(self) -> bool: ...
    def create_store(self, name: str) -> dict: ...
    def get_store(self, store_id: str) -> Optional[dict]: ...
    def list_stores(self) -> list[dict]: ...
    def add_device(self, store_id: str, token_hash: str, label: str = "") -> dict: ...
    def find_device(self, token_hash: str) -> Optional[dict]: ...
    def touch_device(self, device_id: str) -> None: ...
    def revoke_devices(self, store_id: str, keep_device_id: Optional[str] = None) -> int: ...
    def apply(self, store_id: str, items: list[Item], seq: int) -> list[str]: ...
    def get_record(self, store_id: str, kind: str, key: str) -> Optional[dict]: ...
    def list_records(self, store_id: str, kind: str, *, session_id: Optional[str] = None,
                     player_id: Optional[str] = None) -> list[dict]: ...
    def max_seq(self, store_id: str) -> Optional[int]: ...
    def counts(self, store_id: str) -> dict[str, int]: ...
    def create_link(self, store_id: str, player_id: str, token_hash: str) -> dict: ...
    def find_link(self, token_hash: str) -> Optional[dict]: ...
    def get_link(self, link_id: str) -> Optional[dict]: ...
    def touch_link(self, link_id: str) -> None: ...
    def revoke_links(self, store_id: str, player_id: str) -> int: ...


def decide(kind: str, existing: Optional[dict], existing_seq: Optional[int], incoming: dict,
           seq: int) -> tuple[str, Optional[dict]]:
    """(結果, 保存する本文 or None)。両方の保存の実装が使う。"""
    if existing is None:
        return ADDED, incoming
    merged = records.merge(kind, existing, existing_seq, incoming, seq)
    if merged is None:
        return (SAME if records.MERGE_MODE[kind] == "union" else STALE), None
    if merged == existing:
        return SAME, None
    return CHANGED, merged


class MemoryStorage:
    """プロセスの中だけの保存（再起動で消える）。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._stores: dict[str, dict] = {}
        self._devices: dict[str, dict] = {}
        self._records: dict[tuple[str, str, str], tuple[dict, int]] = {}
        self._links: dict[str, dict] = {}

    def migrate(self) -> None:
        return None

    def ping(self) -> bool:
        return True

    def create_store(self, name: str) -> dict:
        with self._lock:
            store = {"store_id": uuid.uuid4().hex, "name": name, "status": "active", "created_at": now_iso(),
                     "last_seq": None}
            self._stores[store["store_id"]] = store
            return dict(store)

    def get_store(self, store_id: str) -> Optional[dict]:
        with self._lock:
            store = self._stores.get(store_id)
            return dict(store) if store else None

    def list_stores(self) -> list[dict]:
        with self._lock:
            return sorted((dict(s) for s in self._stores.values()), key=lambda s: s["created_at"])

    def add_device(self, store_id: str, token_hash: str, label: str = "") -> dict:
        with self._lock:
            if store_id not in self._stores:
                raise StoreNotFound(store_id)
            device = {"device_id": uuid.uuid4().hex, "store_id": store_id, "token_hash": token_hash,
                      "label": label, "created_at": now_iso(), "last_used_at": None, "revoked_at": None}
            self._devices[device["device_id"]] = device
            return {k: v for k, v in device.items() if k != "token_hash"}

    def find_device(self, token_hash: str) -> Optional[dict]:
        with self._lock:
            for device in self._devices.values():
                if device["token_hash"] == token_hash:
                    return {k: v for k, v in device.items() if k != "token_hash"}
            return None

    def touch_device(self, device_id: str) -> None:
        with self._lock:
            if device_id in self._devices:
                self._devices[device_id]["last_used_at"] = now_iso()

    def revoke_devices(self, store_id: str, keep_device_id: Optional[str] = None) -> int:
        with self._lock:
            n = 0
            for device in self._devices.values():
                if (device["store_id"] == store_id and device["device_id"] != keep_device_id
                        and device["revoked_at"] is None):
                    device["revoked_at"] = now_iso()
                    n += 1
            return n

    def apply(self, store_id: str, items: list[Item], seq: int) -> list[str]:
        with self._lock:
            if store_id not in self._stores:
                raise StoreNotFound(store_id)
            outcomes = []
            for kind, key, body in items:
                current = self._records.get((store_id, kind, key))
                existing, existing_seq = (current if current else (None, None))
                outcome, new_body = decide(kind, existing, existing_seq, body, seq)
                if new_body is not None:
                    self._records[(store_id, kind, key)] = (copy.deepcopy(new_body), seq)
                outcomes.append(outcome)
            store = self._stores[store_id]
            if any(o in (ADDED, CHANGED) for o in outcomes):
                store["last_seq"] = max(store["last_seq"] or 0, seq)
            return outcomes

    def get_record(self, store_id: str, kind: str, key: str) -> Optional[dict]:
        with self._lock:
            current = self._records.get((store_id, kind, key))
            return copy.deepcopy(current[0]) if current else None

    def list_records(self, store_id: str, kind: str, *, session_id: Optional[str] = None,
                     player_id: Optional[str] = None) -> list[dict]:
        with self._lock:
            out = []
            for (sid, k, key), (body, _) in sorted(self._records.items()):
                if sid != store_id or k != kind:
                    continue
                pid_col, sess_col = records.index_columns(kind, body)
                if session_id is not None and sess_col != session_id:
                    continue
                if player_id is not None and pid_col != player_id:
                    continue
                out.append(copy.deepcopy(body))
            return out

    def max_seq(self, store_id: str) -> Optional[int]:
        with self._lock:
            seqs = [s for (sid, _, _), (_, s) in self._records.items() if sid == store_id]
            return max(seqs) if seqs else None

    def counts(self, store_id: str) -> dict[str, int]:
        with self._lock:
            out: dict[str, int] = {}
            for (sid, kind, _) in self._records:
                if sid == store_id:
                    out[kind] = out.get(kind, 0) + 1
            return out

    # ――― 閲覧リンク（ADR-0067 D5）―――

    def create_link(self, store_id: str, player_id: str, token_hash: str) -> dict:
        with self._lock:
            if store_id not in self._stores:
                raise StoreNotFound(store_id)
            self.revoke_links(store_id, player_id)          # 1 人 1 本
            link = {"link_id": uuid.uuid4().hex, "store_id": store_id, "player_id": player_id,
                    "token_hash": token_hash, "created_at": now_iso(), "last_used_at": None, "revoked_at": None}
            self._links[link["link_id"]] = link
            return {k: v for k, v in link.items() if k != "token_hash"}

    def find_link(self, token_hash: str) -> Optional[dict]:
        with self._lock:
            for link in self._links.values():
                if link["token_hash"] == token_hash:
                    return {k: v for k, v in link.items() if k != "token_hash"}
            return None

    def get_link(self, link_id: str) -> Optional[dict]:
        with self._lock:
            link = self._links.get(link_id)
            return {k: v for k, v in link.items() if k != "token_hash"} if link else None

    def touch_link(self, link_id: str) -> None:
        with self._lock:
            if link_id in self._links:
                self._links[link_id]["last_used_at"] = now_iso()

    def revoke_links(self, store_id: str, player_id: str) -> int:
        with self._lock:
            n = 0
            for link in self._links.values():
                if link["store_id"] == store_id and link["player_id"] == player_id and link["revoked_at"] is None:
                    link["revoked_at"] = now_iso()
                    n += 1
            return n
