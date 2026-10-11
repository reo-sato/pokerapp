"""cloud/postgres.py — Postgres（Supabase の東京）に保存する `Storage`（ADR-0067 D7）。

- 接続は Supavisor の transaction モード（ポート 6543）を前提に、prepared statement を使わない
  （`prepare_threshold=None`）。セッションをまたぐ状態（search_path・セッションの advisory lock）も使わず、
  テーブル名は常にスキーマ付きで書き、lock はトランザクションの中だけ（`pg_advisory_xact_lock`）。
- テーブルの作成・変更は `cloud/migrations/NNNN_*.sql` を起動時に順に当てる（`migrate`）。
- 同じ店舗の push は 1 つずつ（店舗ごとの lock）。新しいレコードの同時の insert で merge を飛ばさないため。
"""
from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any, Optional

from psycopg import sql
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from cloud import records
from cloud.storage import Item, StoreNotFound, decide

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_SCHEMA_RE = re.compile(r"^[a-z_][a-z0-9_]{0,40}$")
_MIGRATION_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


def _iso(value: Any) -> Optional[str]:
    return value.isoformat(timespec="seconds") if value is not None else None


class PostgresStorage:
    def __init__(self, dsn: str, *, schema: str = "pokerapp", min_size: int = 0, max_size: int = 4) -> None:
        if not _SCHEMA_RE.match(schema):
            raise ValueError(f"スキーマ名に使えない文字があります: {schema!r}")
        self.schema = schema
        self._pool = ConnectionPool(
            dsn, min_size=min_size, max_size=max_size, open=True, timeout=15,
            kwargs={"prepare_threshold": None, "autocommit": False},
        )

    def close(self) -> None:
        self._pool.close()

    # ――― 補助 ―――

    def _t(self, table: str) -> sql.Composed:
        return sql.SQL("{}.{}").format(sql.Identifier(self.schema), sql.Identifier(table))

    def _q(self, text: str, **tables: str) -> sql.Composed:
        return sql.SQL(text).format(**{name: self._t(table) for name, table in tables.items()})

    # ――― 作成・変更 ―――

    def migrate(self) -> list[str]:
        """未適用の migration を当てる。当てた版の一覧を返す。"""
        files = sorted(p for p in MIGRATIONS_DIR.iterdir() if _MIGRATION_RE.match(p.name))
        applied_now: list[str] = []
        with self._pool.connection() as conn, conn.transaction():
            conn.execute(sql.SQL("create schema if not exists {}").format(sql.Identifier(self.schema)))
            conn.execute(self._q("create table if not exists {m} (version text primary key, "
                                 "applied_at timestamptz not null default now())", m="schema_migrations"))
            conn.execute("select pg_advisory_xact_lock(hashtext(%s))", (f"{self.schema}.migrations",))
            done = {row[0] for row in conn.execute(self._q("select version from {m}", m="schema_migrations"))}
            for path in files:
                version = path.name[:4]
                if version in done:
                    continue
                text = path.read_text(encoding="utf-8").replace("{schema}", '"' + self.schema + '"')
                conn.execute(text)
                conn.execute(self._q("insert into {m} (version) values (%s)", m="schema_migrations"), (version,))
                applied_now.append(version)
        return applied_now

    def drop_schema(self) -> None:
        """テスト用: スキーマごと消す。"""
        with self._pool.connection() as conn, conn.transaction():
            conn.execute(sql.SQL("drop schema if exists {} cascade").format(sql.Identifier(self.schema)))

    def ping(self) -> bool:
        try:
            with self._pool.connection(timeout=5) as conn:
                conn.execute("select 1")
                conn.rollback()
            return True
        except Exception:  # noqa: BLE001 — 生きているかの確認だけ
            return False

    # ――― 店舗と端末 ―――

    def create_store(self, name: str) -> dict:
        store_id = uuid.uuid4().hex
        with self._pool.connection() as conn, conn.transaction():
            row = conn.execute(self._q("insert into {s} (store_id, name) values (%s, %s) "
                                       "returning store_id, name, status, created_at", s="stores"),
                               (store_id, name)).fetchone()
        return self._store(row)

    @staticmethod
    def _store(row) -> dict:
        return {"store_id": row[0], "name": row[1], "status": row[2], "created_at": _iso(row[3])}

    def get_store(self, store_id: str) -> Optional[dict]:
        with self._pool.connection() as conn:
            row = conn.execute(self._q("select store_id, name, status, created_at from {s} where store_id = %s",
                                       s="stores"), (store_id,)).fetchone()
            conn.rollback()
        return self._store(row) if row else None

    def list_stores(self) -> list[dict]:
        with self._pool.connection() as conn:
            rows = conn.execute(self._q("select store_id, name, status, created_at from {s} "
                                        "order by created_at, store_id", s="stores")).fetchall()
            conn.rollback()
        return [self._store(r) for r in rows]

    _DEVICE_COLS = "device_id, store_id, label, created_at, last_used_at, revoked_at"

    @staticmethod
    def _device(row) -> dict:
        return {"device_id": row[0], "store_id": row[1], "label": row[2], "created_at": _iso(row[3]),
                "last_used_at": _iso(row[4]), "revoked_at": _iso(row[5])}

    def add_device(self, store_id: str, token_hash: str, label: str = "") -> dict:
        if self.get_store(store_id) is None:
            raise StoreNotFound(store_id)
        with self._pool.connection() as conn, conn.transaction():
            row = conn.execute(self._q("insert into {d} (device_id, store_id, token_hash, label) "
                                       f"values (%s, %s, %s, %s) returning {self._DEVICE_COLS}", d="store_devices"),
                               (uuid.uuid4().hex, store_id, token_hash, label)).fetchone()
        return self._device(row)

    def find_device(self, token_hash: str) -> Optional[dict]:
        with self._pool.connection() as conn:
            row = conn.execute(self._q(f"select {self._DEVICE_COLS} from {{d}} where token_hash = %s",
                                       d="store_devices"), (token_hash,)).fetchone()
            conn.rollback()
        return self._device(row) if row else None

    def touch_device(self, device_id: str) -> None:
        with self._pool.connection() as conn, conn.transaction():
            conn.execute(self._q("update {d} set last_used_at = now() where device_id = %s", d="store_devices"),
                         (device_id,))

    def revoke_devices(self, store_id: str, keep_device_id: Optional[str] = None) -> int:
        with self._pool.connection() as conn, conn.transaction():
            cur = conn.execute(self._q("update {d} set revoked_at = now() where store_id = %s "
                                       "and revoked_at is null and device_id is distinct from %s", d="store_devices"),
                               (store_id, keep_device_id))
            return cur.rowcount

    # ――― レコード ―――

    def apply(self, store_id: str, items: list[Item], seq: int) -> list[str]:
        if not items:
            return []
        with self._pool.connection() as conn, conn.transaction():
            if conn.execute(self._q("select 1 from {s} where store_id = %s", s="stores"), (store_id,)).fetchone() is None:
                raise StoreNotFound(store_id)
            # 同じ店舗の push は 1 つずつ（新しいレコードを同時に insert して merge を飛ばさないため）
            conn.execute("select pg_advisory_xact_lock(hashtext(%s))", (f"{self.schema}.push.{store_id}",))
            existing: dict[tuple[str, str], tuple[dict, int]] = {}
            by_kind: dict[str, list[str]] = {}
            for kind, key, _ in items:
                by_kind.setdefault(kind, []).append(key)
            for kind, keys in by_kind.items():
                for key, body, src_seq in conn.execute(
                        self._q("select key, body, src_seq from {r} where store_id = %s and kind = %s "
                                "and key = any(%s) for update", r="records"), (store_id, kind, keys)):
                    existing[(kind, key)] = (body, src_seq)
            outcomes: list[str] = []
            writes: list[tuple] = []
            for kind, key, body in items:
                cur_body, cur_seq = existing.get((kind, key), (None, None))
                outcome, new_body = decide(kind, cur_body, cur_seq, body, seq)
                outcomes.append(outcome)
                if new_body is not None:
                    existing[(kind, key)] = (new_body, seq)   # 同じ push の中で同じキーが 2 回来ても続けて merge
                    player_id, session_id = records.index_columns(kind, new_body)
                    writes.append((store_id, kind, key, Jsonb(new_body), seq, player_id, session_id))
            if writes:
                with conn.cursor() as cur:
                    cur.executemany(self._q(
                        "insert into {r} (store_id, kind, key, body, src_seq, player_id, session_id) "
                        "values (%s, %s, %s, %s, %s, %s, %s) on conflict (store_id, kind, key) do update set "
                        "body = excluded.body, src_seq = excluded.src_seq, player_id = excluded.player_id, "
                        "session_id = excluded.session_id, updated_at = now()", r="records"), writes)
            return outcomes

    def get_record(self, store_id: str, kind: str, key: str) -> Optional[dict]:
        with self._pool.connection() as conn:
            row = conn.execute(self._q("select body from {r} where store_id = %s and kind = %s and key = %s",
                                       r="records"), (store_id, kind, key)).fetchone()
            conn.rollback()
        return row[0] if row else None

    def list_records(self, store_id: str, kind: str, *, session_id: Optional[str] = None,
                     player_id: Optional[str] = None) -> list[dict]:
        query = "select body from {r} where store_id = %s and kind = %s"
        params: list[Any] = [store_id, kind]
        if session_id is not None:
            query += " and session_id = %s"
            params.append(session_id)
        if player_id is not None:
            query += " and player_id = %s"
            params.append(player_id)
        query += ' order by key collate "C"'
        with self._pool.connection() as conn:
            rows = conn.execute(self._q(query, r="records"), params).fetchall()
            conn.rollback()
        return [r[0] for r in rows]

    def max_seq(self, store_id: str) -> Optional[int]:
        with self._pool.connection() as conn:
            row = conn.execute(self._q("select max(src_seq) from {r} where store_id = %s", r="records"),
                               (store_id,)).fetchone()
            conn.rollback()
        return row[0]

    def counts(self, store_id: str) -> dict[str, int]:
        with self._pool.connection() as conn:
            rows = conn.execute(self._q("select kind, count(*) from {r} where store_id = %s group by kind",
                                        r="records"), (store_id,)).fetchall()
            conn.rollback()
        return {k: int(n) for k, n in rows}
