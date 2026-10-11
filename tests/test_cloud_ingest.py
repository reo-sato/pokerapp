"""tests/test_cloud_ingest.py

ADR-0067 D1/D2: 店舗 PC の本物の保存から作った push を、クラウドが受け取って保存する。

- 契約の形に合うレコードはすべて受け付ける。同じものをもう一度送っても変わらない。
- 形の合わないレコードは理由を付けて断り、ほかは受け付ける（1 件の不備で全部を止めない）。
- ハンドの聞き取った文・監査用の項目は保存しない（送り手が落とし忘れても受け手で落とす）。
"""
from __future__ import annotations

import copy

import pytest

pytest.importorskip("jsonschema")

from cloud.ingest import MAX_RECORDS, PushError, ingest_push  # noqa: E402
from cloud.storage import MemoryStorage  # noqa: E402
from core.cloud_payload import (  # noqa: E402
    KINDS,
    PRIVATE_ACTION_FIELDS,
    fingerprint,
    record_key,
    strip_hand,
)

from tests.cloud_helpers import push_payload, store_snapshot  # noqa: E402


@pytest.fixture()
def snap(tmp_path):
    return store_snapshot(tmp_path)


@pytest.fixture()
def store():
    st = MemoryStorage()
    return st, st.create_store("テスト店")["store_id"]


def test_a_real_store_snapshot_is_accepted(snap, store):
    st, sid = store
    result = ingest_push(st, sid, push_payload(snap, store_settings={"hole_cards": "own_and_shown"}))
    assert result["rejected"] == []
    added = {kind: c["added"] for kind, c in result["results"].items()}
    assert added == {"players": 2, "sessions": 1, "ledger_entries": 3, "point_entries": 1, "settlements": 2,
                     "order_requests": 2, "hands": 2, "store_settings": 1}
    assert result["max_seq"] == 1
    assert st.get_record(sid, "store_settings", "settings") == {"hole_cards": "own_and_shown"}
    # 同じものをもう一度（次の番号で）送っても何も変わらない
    again = ingest_push(st, sid, push_payload(snap, seq=2))
    assert all(c["added"] == 0 and c["changed"] == 0 for c in again["results"].values())


def test_private_fields_are_not_kept(snap, store):
    st, sid = store
    ingest_push(st, sid, push_payload(snap))
    for hand in st.list_records(sid, "hands"):
        for action in hand["actions"]:
            assert not set(PRIVATE_ACTION_FIELDS) & set(action)
            assert action["action"] == "call"     # ほかの項目は残る


def test_bad_records_are_rejected_one_by_one(snap, store):
    st, sid = store
    payload = push_payload(snap)
    recs = payload["records"]
    recs["players"].append({"player_id": "x", "display_name": ""})                 # created_at が無い
    recs["ledger_entries"].append({**recs["ledger_entries"][0], "entry_id": "e-bad", "kind": "prize"})
    recs["hands"].append({"session_id": "s", "hand_id": "1"})                      # hand_id が整数でない
    bad_session = copy.deepcopy(recs["sessions"][0])
    bad_session["session_id"] = "s-bad"
    bad_session["hands"]["1"]["seats"][0]["seat_no"] = 12                           # 席番号が範囲外
    recs["sessions"].append(bad_session)
    recs["corrections"].append({"correction_id": "c1"})
    result = ingest_push(st, sid, payload)
    reasons = {(r["kind"], r["key"]) for r in result["rejected"]}
    assert reasons == {("players", "x"), ("ledger_entries", "e-bad"), ("hands", None), ("sessions", "s-bad"),
                       ("corrections", "c1")}
    assert all(r["reason"] for r in result["rejected"])
    assert result["results"]["players"]["added"] == 2       # 正しいものは受け付けた
    assert st.get_record(sid, "sessions", "s-bad") is None


@pytest.mark.parametrize("payload, code, status", [
    ([], "invalid_payload", 400),
    ({"protocol": 2, "seq": 1}, "unsupported_protocol", 409),
    ({"protocol": 1, "seq": 0}, "invalid_payload", 400),
    ({"protocol": 1, "seq": True}, "invalid_payload", 400),
    ({"protocol": 1, "seq": 1, "records": {"ledger": []}}, "invalid_payload", 400),
    ({"protocol": 1, "seq": 1, "records": {"hands": {}}}, "invalid_payload", 400),
    ({"protocol": 1, "seq": 1, "store_settings": {"hole_cards": "none"}}, "invalid_payload", 400),
    ({"protocol": 1, "seq": 1, "store_settings": {"color": "red"}}, "invalid_payload", 400),
    ({"protocol": 1, "seq": 1, "records": {"hands": [{}] * (MAX_RECORDS + 1)}}, "too_many_records", 413),
])
def test_envelope_errors(store, payload, code, status):
    st, sid = store
    with pytest.raises(PushError) as exc:
        ingest_push(st, sid, payload)
    assert (exc.value.code, exc.value.status) == (code, status)
    assert st.counts(sid) == {}


class TestPayloadHelpers:
    def test_keys(self, snap):
        payload = push_payload(snap)
        for kind in KINDS:
            for rec in payload["records"][kind]:
                assert record_key(kind, rec)
        s = snap["settlements"][0]
        assert record_key("settlements", s) == f"{s['session_id']}:{s['player_id']}"

    def test_fingerprint_ignores_key_order_and_strip_copies(self):
        assert fingerprint({"a": 1, "b": [1, 2]}) == fingerprint({"b": [1, 2], "a": 1})
        hand = {"actions": [{"action": "call", "raw_text": "コール"}]}
        stripped = strip_hand(hand)
        assert stripped["actions"][0] == {"action": "call"}
        assert hand["actions"][0]["raw_text"] == "コール"    # 元は変えない
