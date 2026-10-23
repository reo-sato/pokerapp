"""tests/test_cloud_storage.py

ADR-0067 D2/D3/D7: クラウドの保存（メモリ版と Postgres 版が同じ約束を守る）。

- 店舗ごとに分かれる（同じキーでも別の店舗なら別のレコード）。
- 種類ごとの merge: ハンドは送り手の番号が新しい方 / 会計の明細は append-only /
  session・settlement・注文は `core/sync.py` の解決関数（店舗 PC から届いた方を優先する項目は店舗 PC の値）。
- Postgres 版は `POKERAPP_TEST_DATABASE_URL` があるときだけ（CI ではサービスの Postgres を使う）。
"""
from __future__ import annotations

import pytest

from cloud.storage import ADDED, CHANGED, SAME, STALE, StoreNotFound

from tests.cloud_helpers import make_storage


@pytest.fixture(params=["memory", "postgres"])
def storage(request):
    st = make_storage(request.param)
    yield st
    if request.param == "postgres":
        st.drop_schema()
        st.close()


def _hand(sid="s1", hand_id=1, winner=1):
    return {"session_id": sid, "hand_id": hand_id, "winner_seat": winner, "actions": []}


def test_stores_are_kept_apart(storage):
    a, b = storage.create_store("A 店"), storage.create_store("B 店")
    storage.apply(a["store_id"], [("hands", "s1:1", _hand(winner=1))], seq=1)
    storage.apply(b["store_id"], [("hands", "s1:1", _hand(winner=2))], seq=1)
    assert storage.get_record(a["store_id"], "hands", "s1:1")["winner_seat"] == 1
    assert storage.get_record(b["store_id"], "hands", "s1:1")["winner_seat"] == 2
    assert storage.counts(a["store_id"]) == {"hands": 1}
    assert storage.list_records(b["store_id"], "players") == []


def test_hands_take_the_newer_sender_number(storage):
    sid = storage.create_store("店")["store_id"]
    assert storage.apply(sid, [("hands", "s1:1", _hand(winner=1))], seq=10) == [ADDED]
    assert storage.apply(sid, [("hands", "s1:1", _hand(winner=1))], seq=11) == [SAME]
    assert storage.apply(sid, [("hands", "s1:1", _hand(winner=2))], seq=12) == [CHANGED]
    # 遅れて届いた古い push は上書きしない
    assert storage.apply(sid, [("hands", "s1:1", _hand(winner=3))], seq=5) == [STALE]
    assert storage.get_record(sid, "hands", "s1:1")["winner_seat"] == 2
    assert storage.max_seq(sid) == 12


def test_ledger_entries_are_append_only(storage):
    sid = storage.create_store("店")["store_id"]
    entry = {"entry_id": "e1", "session_id": "s1", "player_id": "p1", "kind": "buy_in",
             "occurred_at": "2026-10-11T10:00:00", "cash_amount": 5000, "point_amount": 0}
    assert storage.apply(sid, [("ledger_entries", "e1", entry)], seq=1) == [ADDED]
    changed = {**entry, "cash_amount": 1}
    assert storage.apply(sid, [("ledger_entries", "e1", changed)], seq=2) == [SAME]
    assert storage.get_record(sid, "ledger_entries", "e1")["cash_amount"] == 5000


def test_sessions_take_the_store_pcs_latest_whole_state(storage):
    """店舗 PC はセッション（席の割り当て込み）の全体をいつも送る → 新しい番号の方がそのまま正しい（追記 4）。"""
    sid = storage.create_store("店")["store_id"]
    base = {"session_id": "s1", "started_at": "2026-10-11T10:00:00", "status": "open", "label": "旧",
            "hole_card_visibility": "own_and_shown",
            "hands": {"1": {"started_at": "2026-10-11T10:01:00", "seats": [{"seat_no": 1, "player_id": "p1"}]}}}
    storage.apply(sid, [("sessions", "s1", base)], seq=1)
    closed = {k: v for k, v in base.items() if k != "hole_card_visibility"}   # 「店の設定に従う」に戻した
    closed.update(status="closed", ended_at="2026-10-11T12:00:00", label="新")
    closed["hands"] = {**base["hands"],
                       "2": {"started_at": "2026-10-11T10:05:00", "seats": [{"seat_no": 2, "player_id": "p2"}]}}
    assert storage.apply(sid, [("sessions", "s1", closed)], seq=2) == [CHANGED]
    got = storage.get_record(sid, "sessions", "s1")
    assert got["label"] == "新" and got["status"] == "closed" and set(got["hands"]) == {"1", "2"}
    assert "hole_card_visibility" not in got          # 項目が無いことも写しに伝わる
    # 遅れて届いた古い push（「開いている」）では戻らない
    assert storage.apply(sid, [("sessions", "s1", base)], seq=1) == [STALE]
    assert storage.get_record(sid, "sessions", "s1")["status"] == "closed"


def test_settlement_paid_amount_only_grows(storage):
    sid = storage.create_store("店")["store_id"]
    s = {"session_id": "s1", "player_id": "p1", "cash_in_total": 5000, "point_spent_total": 0,
         "order_total": 0, "entry_fee": 0, "point_credited_total": 0, "net_due_to_store": 5000,
         "payment_status": "partial", "settled_at": "2026-10-11T12:00:00", "paid_amount": 3000}
    storage.apply(sid, [("settlements", "s1:p1", s)], seq=1)
    storage.apply(sid, [("settlements", "s1:p1", {**s, "paid_amount": 1000, "payment_status": "partial"})], seq=2)
    assert storage.get_record(sid, "settlements", "s1:p1")["paid_amount"] == 3000


def test_the_same_key_twice_in_one_push_is_merged_in_order(storage):
    sid = storage.create_store("店")["store_id"]
    out = storage.apply(sid, [("hands", "s1:1", _hand(winner=1)), ("hands", "s1:1", _hand(winner=2))], seq=1)
    assert out == [ADDED, CHANGED]
    assert storage.get_record(sid, "hands", "s1:1")["winner_seat"] == 2


def test_list_records_filters_and_orders(storage):
    sid = storage.create_store("店")["store_id"]
    items = [("hands", f"{s}:{h}", _hand(sid=s, hand_id=h)) for s in ("sB", "sA") for h in (2, 10, 1)]
    items += [("order_requests", "r1", {"request_id": "r1", "session_id": "sA", "player_id": "p1"}),
              ("order_requests", "r2", {"request_id": "r2", "session_id": "sA", "player_id": "p2"})]
    storage.apply(sid, items, seq=1)
    keys = [f"{h['session_id']}:{h['hand_id']}" for h in storage.list_records(sid, "hands")]
    assert keys == sorted(keys)   # 両方の保存で同じ順（キーの文字列順）
    assert len(storage.list_records(sid, "hands", session_id="sA")) == 3
    assert [r["request_id"] for r in storage.list_records(sid, "order_requests", player_id="p2")] == ["r2"]


def test_devices(storage):
    sid = storage.create_store("店")["store_id"]
    d1 = storage.add_device(sid, "h1", "店舗 PC")
    d2 = storage.add_device(sid, "h2", "予備")
    assert "token_hash" not in d1
    assert storage.find_device("h1")["store_id"] == sid and storage.find_device("nope") is None
    storage.touch_device(d1["device_id"])
    assert storage.find_device("h1")["last_used_at"] is not None
    assert storage.revoke_devices(sid, keep_device_id=d2["device_id"]) == 1
    assert storage.find_device("h1")["revoked_at"] is not None
    assert storage.find_device("h2")["revoked_at"] is None
    assert storage.revoke_devices(sid) == 1
    with pytest.raises(StoreNotFound):
        storage.add_device("missing", "h3")
    with pytest.raises(StoreNotFound):
        storage.apply("missing", [("hands", "s1:1", _hand())], seq=1)


def test_stores_listing(storage):
    a = storage.create_store("A 店")
    assert storage.get_store(a["store_id"])["status"] == "active"
    assert storage.get_store("missing") is None
    assert [s["name"] for s in storage.list_stores()] == ["A 店"]


def test_postgres_migrations_are_idempotent_and_enable_rls():
    st = make_storage("postgres")
    try:
        assert st.migrate() == []          # 2 回目は何もしない
        with st._pool.connection() as conn:
            rows = conn.execute(
                "select c.relname, c.relrowsecurity from pg_class c join pg_namespace n on n.oid = c.relnamespace "
                "where n.nspname = %s and c.relkind = 'r' and c.relname <> 'schema_migrations'",
                (st.schema,)).fetchall()
            conn.rollback()
        assert dict(rows) == {"stores": True, "store_devices": True, "records": True, "viewer_links": True}
        assert st.ping()
    finally:
        st.drop_schema()
        st.close()


def test_viewer_links(storage):
    sid = storage.create_store("店")["store_id"]
    first = storage.create_link(sid, "p1", "h1")
    assert "token_hash" not in first and storage.find_link("h1")["player_id"] == "p1"
    second = storage.create_link(sid, "p1", "h2")            # 1 人 1 本: 前のものは取り消し
    assert storage.find_link("h1")["revoked_at"] is not None
    assert storage.get_link(second["link_id"])["revoked_at"] is None
    storage.create_link(sid, "p2", "h3")
    storage.touch_link(second["link_id"])
    assert storage.get_link(second["link_id"])["last_used_at"] is not None
    assert storage.revoke_links(sid, "p1") == 1
    assert storage.find_link("h3")["revoked_at"] is None       # 別の人のリンクはそのまま
    assert storage.find_link("nope") is None and storage.get_link("nope") is None
    with pytest.raises(StoreNotFound):
        storage.create_link("missing", "p1", "h4")


def test_store_last_seq_moves_only_on_changes(storage):
    sid = storage.create_store("店")["store_id"]
    assert storage.get_store(sid)["last_seq"] is None
    storage.apply(sid, [("hands", "s1:1", _hand())], seq=5)
    assert storage.get_store(sid)["last_seq"] == 5
    storage.apply(sid, [("hands", "s1:1", _hand())], seq=9)      # 変わらなければ動かない
    assert storage.get_store(sid)["last_seq"] == 5
