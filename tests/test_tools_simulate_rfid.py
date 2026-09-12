"""tests/test_tools_simulate_rfid.py

tools/simulate_rfid.py（実機なし RFID injector）のテスト。

- demo_tag_for_card: 決定的・カードごとに一意・正規化で有効な CardMaster キーになる
- register_demo_deck: 合成デッキを書き、CardMaster が解決できる
- post_event / get_status: 実 RFIDHTTPReceiver を起動して end-to-end（POST→rfid_queue）
"""
from __future__ import annotations

import queue
import socket
import threading
import time

import pytest

from core.event_queue import make_rfid_queue
from core.events import RFIDEvent
from rfid.card_master import CardMaster, normalize_tag_id
from rfid.http_receiver import RFIDHTTPReceiver
from tools.simulate_rfid import (
    demo_tag_for_card,
    get_status,
    post_event,
    register_demo_deck,
)

READER_CONFIGS = {
    "seat_1":  {"role": "seat",  "seat": 1},
    "board_1": {"role": "board", "index": 1},
    "board_2": {"role": "board", "index": 2},
}

_ALL_CARDS = [r + s for r in "23456789TJQKA" for s in "cdhs"]


# ――― demo_tag_for_card ―――

class TestDemoTag:
    def test_deterministic(self):
        assert demo_tag_for_card("Ah") == demo_tag_for_card("Ah")

    def test_distinct_for_full_deck(self):
        tags = {demo_tag_for_card(c) for c in _ALL_CARDS}
        assert len(tags) == 52

    def test_normalizes_to_valid_key(self):
        # A=index12 (0C), h=index2 (02) → "DEAD0C02" → "DE:AD:0C:02"
        assert normalize_tag_id(demo_tag_for_card("Ah")) == "DE:AD:0C:02"
        assert normalize_tag_id(demo_tag_for_card("2c")) == "DE:AD:00:00"

    @pytest.mark.parametrize("bad", ["ZZ", "ah", "Ahh", "A", "10h", ""])
    def test_invalid_card_raises(self, bad):
        with pytest.raises(ValueError):
            demo_tag_for_card(bad)


# ――― register_demo_deck ―――

class TestRegisterDemoDeck:
    def test_writes_resolvable_deck(self, tmp_path):
        path = tmp_path / "cards.json"
        count = register_demo_deck(path)
        assert count == 52

        cm = CardMaster(path)
        assert len(cm) == 52
        assert cm.lookup(demo_tag_for_card("Ah")) == "Ah"
        assert cm.lookup(demo_tag_for_card("2c")) == "2c"
        assert cm.lookup(demo_tag_for_card("Td")) == "Td"

    def test_merges_with_existing(self, tmp_path):
        path = tmp_path / "cards.json"
        cm = CardMaster(path)
        cm.register("04AABBCC", "Ah")  # 既存の自前タグ
        register_demo_deck(path)
        cm2 = CardMaster(path)
        assert cm2.lookup("04AABBCC") == "Ah"        # 既存は保持
        assert cm2.lookup(demo_tag_for_card("Ks")) == "Ks"  # 合成も追加


# ――― end-to-end（実 receiver 起動） ―――

def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def live_receiver(tmp_path):
    """合成デッキを読んだ実 CardMaster で RFIDHTTPReceiver を起動して yield する。"""
    cards_file = tmp_path / "cards.json"
    register_demo_deck(cards_file)
    card_master = CardMaster(cards_file)

    port = _free_port()
    rfid_q = make_rfid_queue()
    stop = threading.Event()
    receiver = RFIDHTTPReceiver(
        rfid_queue=rfid_q,
        card_master=card_master,
        reader_configs=READER_CONFIGS,
        bind_host="127.0.0.1",
        bind_port=port,
        stop_event=stop,
    )
    receiver.start()
    for _ in range(20):
        if receiver._server is not None:
            break
        time.sleep(0.05)

    yield rfid_q, port

    stop.set()
    receiver.stop()
    receiver.join(timeout=3)


class TestPostEvent:
    def test_board_card_resolves_and_enqueues(self, live_receiver):
        rfid_q, port = live_receiver
        status, body = post_event("127.0.0.1", port, "board_1", demo_tag_for_card("Ah"))
        assert status == 200
        assert body["status"] == "ok"

        ev: RFIDEvent = rfid_q.get(timeout=1.0)
        assert ev.role == "board"
        assert ev.board_index == 1
        assert ev.card == "Ah"
        assert ev.tag_id == "DE:AD:0C:02"

    def test_seat_card_resolves(self, live_receiver):
        rfid_q, port = live_receiver
        post_event("127.0.0.1", port, "seat_1", demo_tag_for_card("Ks"))
        ev: RFIDEvent = rfid_q.get(timeout=1.0)
        assert ev.role == "seat"
        assert ev.seat == 1
        assert ev.card == "Ks"

    def test_raw_tag_unregistered_is_empty_card(self, live_receiver):
        rfid_q, port = live_receiver
        status, _ = post_event("127.0.0.1", port, "board_2", "0099AABB")
        assert status == 200
        ev: RFIDEvent = rfid_q.get(timeout=1.0)
        assert ev.card == ""        # 未登録 → 空（needs_review 経路）

    def test_unknown_reader_not_enqueued(self, live_receiver):
        rfid_q, port = live_receiver
        status, body = post_event("127.0.0.1", port, "seat_9", demo_tag_for_card("Ah"))
        assert status == 200
        assert body["status"] == "unknown reader_id"
        with pytest.raises(queue.Empty):
            rfid_q.get(timeout=0.3)

    def test_status(self, live_receiver):
        _, port = live_receiver
        status, body = get_status("127.0.0.1", port)
        assert status == 200
        assert body["running"] is True
        assert body["bind_port"] == port
