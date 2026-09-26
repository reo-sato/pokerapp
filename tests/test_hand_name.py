"""tests/test_hand_name.py

ショーダウンでディーラーが言う **勝った役の名前**（オーナーの回答 2026-09-26: 「ウィナー」は言わず、役名を言う。
見せるべきときにマックしたら「フォールド」）:

- 役名（「ツーペア」「フラッシュ」…）はハンドの終わり（「ハンド終了」と同じ）。`AudioEvent.hand_name` に
  pokerkit の役名を載せ、events.jsonl にも残す（reconstruction_event 0.6）。
- 手札とボードの判定と突き合わせる: 一致すれば確定、違えば要確認（役名に合う席が 1 つだけならその席の勝ち =
  `winner_source="announced"`）。手札が読めていない席があるときも、役名から勝者を決める（要確認）。
- 閉じていないベッティングは聞き取れなかったとみて閉じる。確定したあとの役名は判定と違えば知らせる。
"""
from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from audio.recognizer import parse_action, parse_actions
from audio.recorder import describe_event
from core.events import RFIDEvent
from integration.replay import event_from_envelope
from output.event_recorder import event_to_envelope

pytest.importorskip("pokerkit")

from tests.test_rfid_folds import _Table  # noqa: E402

_SCHEMA = Path(__file__).resolve().parent.parent / "docs" / "contracts" / "schemas" / "reconstruction_event.schema.json"


class TestParsing:
    @pytest.mark.parametrize("text, name", [
        ("ツーペア", "Two pair"),
        ("ツーペア、エースとテン", "Two pair"),
        ("2ペアです", "Two pair"),
        ("ワンペア", "One pair"),
        ("スリーカード", "Three of a kind"),
        ("トリップス", "Three of a kind"),
        ("ストレート", "Straight"),
        ("フラッシュ", "Flush"),
        ("フルハウス", "Full house"),
        ("フォーカード", "Four of a kind"),
        ("ストレートフラッシュ", "Straight flush"),
        ("ロイヤルフラッシュ", "Straight flush"),
        ("ハイカード、エースハイ", "High card"),
        ("ふらっしゅ", "Flush"),
    ])
    def test_a_hand_name_ends_the_hand(self, text, name):
        (event,) = parse_actions(text)
        assert (event.action, event.hand_name, event.amount) == ("end_hand", name, 0)

    def test_plain_end_of_hand_has_no_name(self):
        assert parse_action("ハンド終了").hand_name is None

    @pytest.mark.parametrize("text", ["セット", "リセットします", "フル"])
    def test_loose_words_are_not_hand_names(self, text):
        assert parse_actions(text) == []

    def test_described_for_the_cli(self):
        assert describe_event(parse_action("ツーペア")) == "ハンド終了（ツーペア）"

    def test_recorded_and_replayed(self):
        event = parse_action("フラッシュ", confidence=0.7, utterance_start_ts=5.0)
        envelope = event_to_envelope(event)
        assert envelope["hand_name"] == "Flush"
        jsonschema.Draft202012Validator(json.loads(_SCHEMA.read_text(encoding="utf-8"))).validate(envelope)
        assert event_from_envelope(envelope).hand_name == "Flush"
        assert "hand_name" not in event_to_envelope(parse_action("ハンド終了"))


# ボタン 席6: プリフロップは 6 → 4 → 5、フロップ以降は 4 → 5 → 6 の順。
FLUSH_BOARD = ["Jd", "9d", "3d", "5h", "2c"]
HOLES = {4: ["6s", "Qs"], 5: ["Jh", "9c"], 6: ["Kd", "Qd"]}   # 席4 ハイカード / 席5 ツーペア / 席6 フラッシュ


def _board(tb: _Table, cards: list[str]) -> None:
    for index, card in enumerate(cards, start=len(tb.t._board_positions) + 1):   # noqa: SLF001
        ev = RFIDEvent(tag_id=card, card=card, reader_id="b", role="board", seat=None,
                       timestamp=tb.now, raw_tag_id=card, board_index=index)
        tb.recorder.record(ev)
        tb.t._process_rfid_event(ev)         # noqa: SLF001
    tb.tick(tb.now + 1.0)


def _to_river(tb: _Table, holes=HOLES, checks_on_river: bool = True) -> None:
    tb.deal(holes)
    for text in ("コール", "コール", "チェック"):
        tb.say(text)
        tb.tick(tb.now + 1.0)
    _board(tb, FLUSH_BOARD[:3])
    for _ in range(3):
        tb.say("チェック")
        tb.tick(tb.now + 1.0)
    _board(tb, FLUSH_BOARD[3:4])
    for _ in range(3):
        tb.say("チェック")
        tb.tick(tb.now + 1.0)
    _board(tb, FLUSH_BOARD[4:])
    if checks_on_river:
        for _ in range(3):
            tb.say("チェック")
            tb.tick(tb.now + 1.0)


class TestShowdown:
    def test_a_matching_hand_name_confirms_the_winner(self, tmp_path):
        tb = _Table(tmp_path)
        _to_river(tb)
        tb.say("フラッシュ")
        (hand,) = tb.hands
        assert (hand.winner_seat, hand.winner_source, hand.announced_hand) == (6, "cards", "Flush")
        assert not hand.review_required
        assert hand.to_dict()["announced_hand"] == "Flush"

    def test_a_hand_name_nobody_has_flags_the_hand(self, tmp_path):
        tb = _Table(tmp_path)
        _to_river(tb)
        tb.say("フルハウス")
        (hand,) = tb.hands
        assert (hand.winner_seat, hand.winner_source) == (6, "cards")
        assert hand.review_required and hand.announced_hand == "Full house"
        assert any("合いません" in n for n in tb.notices)

    def test_the_seat_with_the_announced_hand_wins(self, tmp_path):
        # 判定は席6 のフラッシュだが、ディーラーは「ツーペア」= 席5 の手（席6 の札の読み違いの疑い）
        tb = _Table(tmp_path)
        _to_river(tb)
        tb.say("ツーペア")
        (hand,) = tb.hands
        assert (hand.winner_seat, hand.winner_source) == (5, "announced")
        assert hand.review_required and hand.pot_total == 600
        assert any("席5 の勝ちにします" in n for n in tb.notices)

    def test_an_unreadable_seat_is_resolved_by_the_hand_name(self, tmp_path):
        tb = _Table(tmp_path)
        _to_river(tb, holes={4: HOLES[4], 5: HOLES[5], 6: ["Kd"]})    # 席6 の札は 1 枚しか読めていない
        tb.say("ツーペア")
        (hand,) = tb.hands
        assert (hand.winner_seat, hand.winner_source) == (5, "announced") and hand.review_required

    def test_an_unreadable_seat_wins_when_no_readable_hand_matches(self, tmp_path):
        tb = _Table(tmp_path)
        _to_river(tb, holes={4: HOLES[4], 5: HOLES[5], 6: ["Kd"]})
        tb.say("フラッシュ")
        (hand,) = tb.hands
        assert (hand.winner_seat, hand.winner_source) == (6, "announced") and hand.review_required

    def test_a_hand_name_closes_the_betting(self, tmp_path):
        # リバーのチェックが聞き取れないまま役名が言われた = ハンドは終わっている
        tb = _Table(tmp_path)
        _to_river(tb, checks_on_river=False)
        tb.say("フラッシュ")
        (hand,) = tb.hands
        assert (hand.winner_seat, hand.winner_source) == (6, "cards") and hand.review_required
        assert [a.seat for a in hand.actions if a.street == "river"] == [4, 5, 6]
        assert all(a.action == "check" and a.reason == "implied_before_showdown"
                   for a in hand.actions if a.street == "river")

    def test_a_hand_name_before_the_river_does_not_end_the_hand(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal(HOLES)
        tb.say("フラッシュ")
        assert tb.hands == [] and any("ベッティングの途中" in n for n in tb.notices)

    def test_a_late_hand_name_that_differs_is_reported(self, tmp_path):
        tb = _Table(tmp_path)
        _to_river(tb)
        tb.say("フラッシュ")
        tb.say("ツーペア")
        assert len(tb.hands) == 1
        assert any("役名「ツーペア」が判定（フラッシュ）と違います" in n for n in tb.notices)

    def test_a_late_hand_name_that_matches_is_quiet(self, tmp_path):
        tb = _Table(tmp_path)
        _to_river(tb)
        tb.say("フラッシュ")
        before = len(tb.notices)
        tb.say("フラッシュ")
        assert len(tb.notices) == before
