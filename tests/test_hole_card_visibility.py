"""tests/test_hole_card_visibility.py

ADR-0067 D6 / 2d: ホールカードの公開範囲（`all` / `own_and_shown`）。

- `own_and_shown` では、見ている人の席の手札と、ショーダウンで見せた手札だけを出す。見せずにマックした手札・
  フォールドした手札は出さない（`hole_cards: null` + `hole_cards_hidden: true`）。
- 本物のエンジンが作ったハンドで、**手札が入る項目をすべて**確かめる（新しい項目に手札が入ったら落ちる）。
- セッションの設定 > 店の設定 > all。店内の viewer API とクラウドの両方に効き、スタッフの読み取りには効かない。
"""
from __future__ import annotations

import copy
import re

import pytest

from api.read_models import hide_hole_cards, hole_card_mode, viewer_seats
from core.player_repository import PlayerRepository
from core.session_repository import InvalidHoleCardVisibilityError, SessionRepository

pytest.importorskip("pokerkit")

from tests.test_auto_hand import HANDS, _heads_up_to_river, _Table, _to_river  # noqa: E402

CARD = re.compile(r"^[2-9TJQKA][shdc]$")


def _cards_outside(hand: dict, own_seats: set[int]) -> list[str]:
    """ボード・見せた手札・自分の席の手札の外にある札（あれば漏れ）。"""
    found: list[str] = []
    allowed = set(own_seats) | {e["seat"] for e in hand.get("showdown") or []}

    def walk(value, path):
        if isinstance(value, str) and CARD.match(value):
            found.append("/".join(map(str, path)))
        elif isinstance(value, dict):
            for k, v in value.items():
                walk(v, path + [k])
        elif isinstance(value, list):
            for i, v in enumerate(value):
                walk(v, path + [i])

    for key, value in hand.items():
        if key in ("board", "board_timeline", "showdown"):
            continue
        if key == "players":
            for i, player in enumerate(value):
                if player.get("seat") not in allowed:
                    walk(player, ["players", i])
            continue
        walk(value, [key])
    return found


def _mucked_hand(tmp_path) -> dict:
    """席5 がプリフロップでフォールド、席2 と席8 がリバーまで → 席2 が見せずにマック（誰も見せない）。"""
    tb = _Table(tmp_path)
    tb.deal(HANDS, 1.0)
    _heads_up_to_river(tb)
    tb.say("フォールド", 45)
    (hand,) = tb.hands
    assert hand.winner_source == "fold" and not hand.showdown
    return hand.to_dict()


def _shown_hand(tmp_path) -> dict:
    tb = _Table(tmp_path)
    tb.deal(HANDS, 1.0)
    _to_river(tb)
    tb.say("ハンド終了", 50)
    (hand,) = tb.hands
    assert hand.winner_source == "cards"
    return hand.to_dict()


class TestHide:
    def test_all_changes_nothing(self, tmp_path):
        hand = _mucked_hand(tmp_path)
        assert hide_hole_cards(hand, set(), "all") is hand

    def test_own_cards_only_when_nobody_showed(self, tmp_path):
        hand = _mucked_hand(tmp_path)
        before = copy.deepcopy(hand)
        hidden = hide_hole_cards(hand, {5}, "own_and_shown")
        assert hand == before                                     # 元は変えない
        by_seat = {p["seat"]: p for p in hidden["players"]}
        assert by_seat[5]["hole_cards"] == ["Kd", "Kc"]             # 自分（フォールドした手札）は見える
        for seat in (2, 8):                                         # 見せずにマック・勝った人も見えない
            assert by_seat[seat]["hole_cards"] is None and by_seat[seat]["hole_cards_hidden"] is True
        assert _cards_outside(hidden, {5}) == []                    # ほかの項目にも手札は無い
        assert hidden["board"] == before["board"]                   # ボードはそのまま

    def test_shown_hands_are_visible_to_everyone(self, tmp_path):
        hand = _shown_hand(tmp_path)
        hidden = hide_hole_cards(hand, set(), "own_and_shown")       # 卓にいなかった人でも
        assert [p["hole_cards"] for p in hidden["players"]] == [p["hole_cards"] for p in hand["players"]]
        assert hidden["showdown"] == hand["showdown"]
        assert _cards_outside(hidden, set()) == []

    def test_the_raw_record_carries_cards_only_where_expected(self, tmp_path):
        """隠す前のハンドでも、手札は players と showdown にしか無い（ここが落ちたら hide_hole_cards を直す）。"""
        hand = _mucked_hand(tmp_path)
        assert sorted(_cards_outside(hand, set())) == sorted(
            f"players/{i}/hole_cards/{k}" for i in range(3) for k in range(2))


class TestMode:
    def test_session_then_store_then_all(self, tmp_path):
        players = PlayerRepository(tmp_path / "players.json")
        sessions = SessionRepository(tmp_path / "sessions.json", player_repo=players)
        s = sessions.create_session()
        assert hole_card_mode(sessions, s.session_id) == "all"
        assert hole_card_mode(sessions, s.session_id, "own_and_shown") == "own_and_shown"
        sessions.set_hole_card_visibility(s.session_id, "all")
        assert hole_card_mode(sessions, s.session_id, "own_and_shown") == "all"
        sessions.close_session(s.session_id)
        sessions.set_hole_card_visibility(s.session_id, None)       # 閉じたあとでも変えられる（表示の設定）
        again = SessionRepository(tmp_path / "sessions.json", player_repo=players)
        assert again.get_session(s.session_id).hole_card_visibility is None
        with pytest.raises(InvalidHoleCardVisibilityError):
            sessions.set_hole_card_visibility(s.session_id, "nobody")
        assert hole_card_mode(sessions, "missing", "own_and_shown") == "own_and_shown"
        assert hole_card_mode(sessions, s.session_id, "nonsense") == "all"

    def test_viewer_seats_follow_merges(self, tmp_path):
        players = PlayerRepository(tmp_path / "players.json")
        sessions = SessionRepository(tmp_path / "sessions.json", player_repo=players)
        a, b = players.create_player("A"), players.create_player("B")
        s = sessions.create_session()
        sessions.assign_seat(s.session_id, 1, 3, a.player_id)
        assert viewer_seats(sessions, s.session_id, 1, a.player_id) == {3}
        assert viewer_seats(sessions, s.session_id, 1, b.player_id) == set()
        players.merge_players(b.player_id, a.player_id)             # A は B に統合された
        assert viewer_seats(sessions, s.session_id, 1, b.player_id) == {3}
        assert viewer_seats(sessions, s.session_id, 1, None) == set()
        assert viewer_seats(sessions, s.session_id, 99, a.player_id) == set()


fastapi = pytest.importorskip("fastapi")


class TestStoreApi:
    """店内の viewer API（名前選び = 見ている人は選んだ人）とスタッフ API。"""

    @pytest.fixture()
    def store(self, tmp_path):
        import json

        from fastapi.testclient import TestClient

        from api.server import create_app
        from core.hand_correction_repository import HandCorrectionRepository
        from core.ledger_repository import LedgerRepository
        from core.order_request_repository import OrderRequestRepository

        players = PlayerRepository(tmp_path / "players.json")
        sessions = SessionRepository(tmp_path / "sessions.json", player_repo=players)
        ids = {s: players.create_player(f"P{s}").player_id for s in (2, 5, 8)}
        session = sessions.create_session()
        for seat, pid in ids.items():
            sessions.assign_seat(session.session_id, 1, seat, pid)
        hand = _mucked_hand(tmp_path)
        hand.update(session_id=session.session_id, hand_id=1)
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        (log_dir / f"{session.session_id}.json").write_text(
            json.dumps({"session_id": session.session_id, "hands": [hand]}), encoding="utf-8")

        def make(default="all"):
            return TestClient(create_app(
                players, sessions, log_dir,
                ledger_repo=LedgerRepository(tmp_path / "ledger.json", session_repo=sessions),
                order_repo=OrderRequestRepository(tmp_path / "orders.json", session_repo=sessions),
                correction_repo=HandCorrectionRepository(tmp_path / "corrections.json"),
                orders_writable=True, staff_token="staff", hole_cards_default=default))

        return {"make": make, "ids": ids, "sid": session.session_id, "sessions": sessions}

    @staticmethod
    def _cards(hand):
        return {p["seat"]: p["hole_cards"] for p in hand["players"]}

    def test_all_by_default(self, store):
        client = store["make"]()
        hand = client.get(f"/api/sessions/{store['sid']}/hands/1").json()
        assert all(self._cards(hand).values())

    def test_store_default_own_and_shown(self, store):
        client, sid, ids = store["make"]("own_and_shown"), store["sid"], store["ids"]
        mine = client.get(f"/api/players/{ids[5]}/sessions/{sid}/hands").json()["hands"][0]
        assert self._cards(mine) == {2: None, 5: ["Kd", "Kc"], 8: None}
        one = client.get(f"/api/sessions/{sid}/hands/1", params={"player_id": ids[8]}).json()
        assert self._cards(one) == {2: None, 5: None, 8: ["Qh", "Qc"]}
        anonymous = client.get(f"/api/sessions/{sid}/hands/1").json()
        assert set(self._cards(anonymous).values()) == {None}
        staff = client.get(f"/api/staff/sessions/{sid}/hands", headers={"Authorization": "Bearer staff"})
        assert all(self._cards(staff.json()["hands"][0]).values())   # スタッフはすべて見る

    def test_staff_sets_the_session_override(self, store):
        client, sid, ids = store["make"]("own_and_shown"), store["sid"], store["ids"]
        auth = {"Authorization": "Bearer staff"}
        assert client.get("/api/staff/hole-card-visibility", headers=auth).json() == {
            "store_default": "own_and_shown"}
        res = client.put(f"/api/staff/sessions/{sid}/hole-card-visibility", json={"value": "all"}, headers=auth)
        assert res.json()["effective"] == "all" and res.json()["session"]["hole_card_visibility"] == "all"
        one = client.get(f"/api/sessions/{sid}/hands/1", params={"player_id": ids[8]}).json()
        assert all(self._cards(one).values())
        res = client.put(f"/api/staff/sessions/{sid}/hole-card-visibility", json={"value": None}, headers=auth)
        assert res.json()["effective"] == "own_and_shown" and "hole_card_visibility" not in res.json()["session"]
        bad = client.put(f"/api/staff/sessions/{sid}/hole-card-visibility", json={"value": "x"}, headers=auth)
        assert bad.status_code == 400 and bad.json()["code"] == "invalid_visibility"
        assert client.put(f"/api/staff/sessions/{sid}/hole-card-visibility", json={}, headers=auth).status_code == 400
        assert client.put("/api/staff/sessions/missing/hole-card-visibility", json={"value": "all"},
                          headers=auth).status_code == 404
        assert client.put(f"/api/staff/sessions/{sid}/hole-card-visibility", json={"value": "all"}).status_code == 401


def test_the_config_value_reaches_the_app():
    from api.server import auth_kwargs_from_config

    assert auth_kwargs_from_config({})["hole_cards_default"] == "all"
    assert auth_kwargs_from_config({"hole_cards": "own_and_shown"})["hole_cards_default"] == "own_and_shown"
    assert auth_kwargs_from_config({"hole_cards": "typo"})["hole_cards_default"] == "all"
