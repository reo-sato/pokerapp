# tests/test_rfid_bridge.py
"""rfid/bridge.py RFIDThread のユニットテスト。

サーバーは起動せず _on_rfid_event() を直接呼び出してロジックを検証する。
"""
from __future__ import annotations

import queue

import pytest

from rfid.bridge import RFIDThread
from rfid.card_master import CardMaster

# ── テスト用リーダー設定 ─────────────────────────────────────────────────────

READERS_CONFIG = {
    "seat_1":  {"role": "seat",  "seat": 1},
    "seat_2":  {"role": "seat",  "seat": 2},
    "board_1": {"role": "board", "index": 1},
    "board_2": {"role": "board", "index": 2},
    "board_3": {"role": "board", "index": 3},
    "board_4": {"role": "board", "index": 4},
    "board_5": {"role": "board", "index": 5},
}

TAG_SEAT_1  = "04:11:22:33:44:55"
TAG_SEAT_2  = "04:11:22:33:44:56"
TAG_BOARD_1 = "04:AA:BB:CC:DD:01"
TAG_BOARD_2 = "04:AA:BB:CC:DD:02"
TAG_BOARD_3 = "04:AA:BB:CC:DD:03"
TAG_BOARD_4 = "04:AA:BB:CC:DD:04"
TAG_BOARD_5 = "04:AA:BB:CC:DD:05"
TAG_UNKNOWN = "04:FF:FF:FF:FF:FF"
TS          = "2026-04-10T00:00:00"


# ── ヘルパー ─────────────────────────────────────────────────────────────────

def make_bridge(tmp_path, threshold: int = 3) -> tuple[RFIDThread, queue.Queue]:
    """CardMaster にタグを登録して RFIDThread（未起動）と Queue を返す。"""
    cm = CardMaster(tmp_path / "cards.json")
    cm.register(TAG_SEAT_1,  "Ah")
    cm.register(TAG_SEAT_2,  "Kd")
    cm.register(TAG_BOARD_1, "2c")
    cm.register(TAG_BOARD_2, "3h")
    cm.register(TAG_BOARD_3, "4s")
    cm.register(TAG_BOARD_4, "5d")
    cm.register(TAG_BOARD_5, "6c")

    q = queue.Queue()
    bridge = RFIDThread(
        event_queue=q,
        card_master=cm,
        host="127.0.0.1",
        port=19999,   # 起動しないので使用されない
        fold_absent_threshold=threshold,
        readers_config=READERS_CONFIG,
    )
    return bridge, q


def send(bridge: RFIDThread, reader_id: str, tag_id: str, event_type: str) -> None:
    """_on_rfid_event を直接呼び出す。"""
    bridge._on_rfid_event({
        "reader_id":  reader_id,
        "tag_id":     tag_id,
        "timestamp":  TS,
        "event_type": event_type,
    })


def drain(q: queue.Queue) -> list[dict]:
    """Queue の全アイテムをリストで返す。"""
    items = []
    while not q.empty():
        items.append(q.get_nowait())
    return items


# ── テスト: present → hole_card ──────────────────────────────────────────────

class TestHoleCard:
    def test_present_emits_hole_card(self, tmp_path):
        bridge, q = make_bridge(tmp_path)
        send(bridge, "seat_1", TAG_SEAT_1, "present")

        events = drain(q)
        assert len(events) == 1
        ev = events[0]
        assert ev["type"]       == "hole_card"
        assert ev["seat"]       == 1
        assert ev["card_code"]  == "Ah"
        assert ev["event_type"] == "present"
        assert ev["reader_id"]  == "seat_1"
        assert ev["tag_id"]     == TAG_SEAT_1

    def test_two_seats_independent(self, tmp_path):
        bridge, q = make_bridge(tmp_path)
        send(bridge, "seat_1", TAG_SEAT_1, "present")
        send(bridge, "seat_2", TAG_SEAT_2, "present")

        events = drain(q)
        seats = {ev["seat"] for ev in events}
        assert seats == {1, 2}


# ── テスト: absent → fold ────────────────────────────────────────────────────

class TestFold:
    def test_below_threshold_no_fold(self, tmp_path):
        bridge, q = make_bridge(tmp_path, threshold=3)
        send(bridge, "seat_1", "", "absent")
        send(bridge, "seat_1", "", "absent")
        assert q.empty()

    def test_at_threshold_emits_fold(self, tmp_path):
        bridge, q = make_bridge(tmp_path, threshold=3)
        send(bridge, "seat_1", "", "absent")
        send(bridge, "seat_1", "", "absent")
        send(bridge, "seat_1", "", "absent")

        events = drain(q)
        assert len(events) == 1
        ev = events[0]
        assert ev["type"]      == "fold"
        assert ev["seat"]      == 1
        assert ev["reader_id"] == "seat_1"

    def test_threshold_1(self, tmp_path):
        bridge, q = make_bridge(tmp_path, threshold=1)
        send(bridge, "seat_2", "", "absent")

        events = drain(q)
        assert len(events) == 1
        assert events[0]["seat"] == 2

    def test_fold_counter_resets_after_fire(self, tmp_path):
        """閾値超過後にカウンタがリセットされ、続く absent×threshold-1 では発火しない。"""
        bridge, q = make_bridge(tmp_path, threshold=3)
        # 閾値到達 → fold
        for _ in range(3):
            send(bridge, "seat_1", "", "absent")
        drain(q)  # fold を消費
        # リセット後 2回だけ → fold なし
        send(bridge, "seat_1", "", "absent")
        send(bridge, "seat_1", "", "absent")
        assert q.empty()

    def test_present_resets_absent_counter(self, tmp_path):
        """present が absent カウンタをリセットして誤フォールドを防ぐ（FR-12）。"""
        bridge, q = make_bridge(tmp_path, threshold=3)
        send(bridge, "seat_1", "", "absent")
        send(bridge, "seat_1", "", "absent")
        # present でリセット
        send(bridge, "seat_1", TAG_SEAT_1, "present")
        # present 後 2回 absent → 閾値未達
        send(bridge, "seat_1", "", "absent")
        send(bridge, "seat_1", "", "absent")

        events = drain(q)
        assert any(e["type"] == "hole_card" for e in events)
        assert not any(e["type"] == "fold" for e in events)


# ── テスト: ショーダウンガード ───────────────────────────────────────────────

class TestShowdownGuard:
    def test_showdown_absent_no_fold(self, tmp_path):
        """ショーダウン中は absent でフォールドを発火しない（FR-11）。"""
        bridge, q = make_bridge(tmp_path, threshold=1)
        bridge.set_street("showdown")
        send(bridge, "seat_1", "", "absent")
        assert q.empty()

    def test_non_showdown_fold_fires(self, tmp_path):
        """通常フェーズでは threshold=1 で fold が発火する。"""
        bridge, q = make_bridge(tmp_path, threshold=1)
        bridge.set_street("preflop")
        send(bridge, "seat_1", "", "absent")
        events = drain(q)
        assert any(e["type"] == "fold" for e in events)


# ── テスト: ボードカード → ストリート遷移 ───────────────────────────────────

class TestStreetTransition:
    def test_board_3_cards_emits_flop(self, tmp_path):
        bridge, q = make_bridge(tmp_path)
        send(bridge, "board_1", TAG_BOARD_1, "present")
        send(bridge, "board_2", TAG_BOARD_2, "present")
        send(bridge, "board_3", TAG_BOARD_3, "present")

        events = drain(q)
        board_events  = [e for e in events if e["type"] == "board_card"]
        street_events = [e for e in events if e["type"] == "street"]
        assert len(board_events) == 3
        assert len(street_events) == 1
        assert street_events[0]["street"] == "flop"

    def test_board_4_cards_emits_turn(self, tmp_path):
        bridge, q = make_bridge(tmp_path)
        for tag, rid in [
            (TAG_BOARD_1, "board_1"),
            (TAG_BOARD_2, "board_2"),
            (TAG_BOARD_3, "board_3"),
            (TAG_BOARD_4, "board_4"),
        ]:
            send(bridge, rid, tag, "present")

        street_events = [e for e in drain(q) if e["type"] == "street"]
        streets = [e["street"] for e in street_events]
        assert "flop" in streets
        assert "turn" in streets

    def test_board_5_cards_emits_river(self, tmp_path):
        bridge, q = make_bridge(tmp_path)
        tags = [TAG_BOARD_1, TAG_BOARD_2, TAG_BOARD_3, TAG_BOARD_4, TAG_BOARD_5]
        rids = ["board_1", "board_2", "board_3", "board_4", "board_5"]
        for tag, rid in zip(tags, rids):
            send(bridge, rid, tag, "present")

        street_events = [e for e in drain(q) if e["type"] == "street"]
        streets = [e["street"] for e in street_events]
        assert streets == ["flop", "turn", "river"]

    def test_no_duplicate_street_event(self, tmp_path):
        """同一リーダーを再検出しても重複ストリートイベントは発火しない。"""
        bridge, q = make_bridge(tmp_path)
        send(bridge, "board_1", TAG_BOARD_1, "present")
        send(bridge, "board_2", TAG_BOARD_2, "present")
        send(bridge, "board_3", TAG_BOARD_3, "present")
        drain(q)  # 最初の 3枚
        # board_3 を再検出
        send(bridge, "board_3", TAG_BOARD_3, "present")

        events = drain(q)
        street_events = [e for e in events if e["type"] == "street"]
        assert len(street_events) == 0  # flop は既に発火済みなので再発火なし

    def test_reset_board_clears_state(self, tmp_path):
        """reset_board() 後に同じ3枚を置くと flop が再度発火する。"""
        bridge, q = make_bridge(tmp_path)
        for tag, rid in [(TAG_BOARD_1, "board_1"),
                         (TAG_BOARD_2, "board_2"),
                         (TAG_BOARD_3, "board_3")]:
            send(bridge, rid, tag, "present")
        drain(q)

        bridge.reset_board()
        for tag, rid in [(TAG_BOARD_1, "board_1"),
                         (TAG_BOARD_2, "board_2"),
                         (TAG_BOARD_3, "board_3")]:
            send(bridge, rid, tag, "present")

        events = drain(q)
        street_events = [e for e in events if e["type"] == "street"]
        assert len(street_events) == 1
        assert street_events[0]["street"] == "flop"


# ── テスト: 未登録タグ → needs_review ───────────────────────────────────────

class TestNeedsReview:
    def test_unregistered_tag_not_in_queue(self, tmp_path):
        bridge, q = make_bridge(tmp_path)
        send(bridge, "seat_1", TAG_UNKNOWN, "present")
        assert q.empty()

    def test_unregistered_tag_added_to_needs_review(self, tmp_path):
        bridge, q = make_bridge(tmp_path)
        send(bridge, "seat_1", TAG_UNKNOWN, "present")

        assert len(bridge.needs_review_items) == 1
        item = bridge.needs_review_items[0]
        assert item["tag_id"]    == TAG_UNKNOWN
        assert item["reader_id"] == "seat_1"

    def test_multiple_unregistered_tags_accumulate(self, tmp_path):
        bridge, q = make_bridge(tmp_path)
        send(bridge, "seat_1", TAG_UNKNOWN, "present")
        send(bridge, "seat_2", TAG_UNKNOWN, "present")

        assert len(bridge.needs_review_items) == 2
        assert q.empty()
