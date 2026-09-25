"""tests/test_spoken_amounts.py

店舗の 3 回目の通しテスト（2026-09-25）への対応:

- 読み取った札を CLI に出す（手札・フロップ / ターン / リバー・確定時のまとめ）。
- 数字だけの発話（「600点」「2千点」）はベットかレイズ。いまのベット以下・最小ベット未満の額は使わない。
- 「チェックアラウンド」= まだ動いていない全員がチェック（オーナーの説明）。
- 前のストリートで言われた「コール」の言い直しを、次のストリートのチェックにしない。
- プロンプトの語を順に並べる幻聴・字幕の定型句を雑音にする。
"""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from audio.recognizer import is_prompt_echo, parse_actions
from audio.recorder import describe_event
from core.event_queue import make_audio_queue
from core.events import RFIDEvent
from core.game_state import PlayerState
from integration.engine import IntegrationThread, _card_text
from output.json_writer import JsonWriter

pytest.importorskip("pokerkit")

from core.poker_engine import PokerkitGameState  # noqa: E402

# 店舗の 3 回目の通しテストの 1 ハンド目（events.jsonl）: ボタン 席6、SB 席4、BB 席5
HOLES = {4: ["6h", "Qd"], 5: ["6c", "7c"], 6: ["9h", "6s"]}


class _Table:
    def __init__(self, tmp_path: Path, seats=(4, 5, 6)):
        self.gs = PokerkitGameState(
            [PlayerState(seat=s, name=f"P{s}", stack=10000) for s in seats], sb=100, bb=200)
        self.now = 0.0
        self.cards: dict[int, list[str]] = {}
        self.hands: list = []
        self.actions: list = []
        self.notices: list[str] = []
        self.card_lines: list[str] = []
        self.t = IntegrationThread(
            audio_queue=make_audio_queue(), game_state=self.gs,
            json_writer=JsonWriter(tmp_path, "amounts"), on_hand=self.hands.append,
            on_action=self.actions.append, stop_event=threading.Event(), clock=lambda: self.now,
            auto_new_hand=True, auto_winner=True, on_notice=self.notices.append,
            on_cards=self.card_lines.append, seat_presence=self._presence,
            board_presence=lambda: {"present_count": 0}, listen_gate=threading.Event(),
        )

    def _presence(self) -> dict:
        return {s: {"present": bool(c), "cards": list(c)} for s, c in self.cards.items()}

    def deal(self, holes=HOLES) -> None:
        self.cards = {s: list(c) for s, c in holes.items()}
        while self.gs.hand_id == 0 or not self.t._hand_open:   # noqa: SLF001
            self.now = round(self.now + 0.1, 3)
            self.t._check_deal_presence()   # noqa: SLF001

    def say(self, text: str, spoken_at: float | None = None) -> None:
        start = self.now if spoken_at is None else spoken_at
        for event in parse_actions(text, confidence=0.9, utterance_start_ts=start):
            event.timestamp = self.now
            self.t._handle_audio_event(event)   # noqa: SLF001

    def board(self, index: int, card: str) -> None:
        self.t._process_rfid_event(RFIDEvent(   # noqa: SLF001
            tag_id=card, card=card, reader_id="b", role="board", seat=None, timestamp=self.now,
            raw_tag_id=card, board_index=index))

    def played(self) -> list[tuple]:
        return [(a.street, a.seat, a.action, a.amount) for a in self.actions
                if a.actor_source != "unresolved"]


class TestStoreHand:
    """events.jsonl のハンドを、そのまま流して正しく記録できること。"""

    def test_the_whole_hand(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal()
        assert tb.gs.button_seat == 6
        tb.now = 10.0
        for text in ("コール", "フォールド", "アクションです。レイズ1千点。", "コール"):
            tb.say(text)
        tb.now = 20.0
        for i, card in enumerate(["3d", "Js", "7h"], start=1):
            tb.board(i, card)
        tb.now = 25.0
        tb.say("ヒットアップです。")
        tb.say("600点")                                   # 語を言わないベット
        tb.say("コール")
        tb.now = 31.0
        tb.board(4, "5h")
        tb.now = 33.0
        tb.say("600点コールです。", spoken_at=28.0)          # ターンの札より前に話した言い直し
        tb.now = 35.0
        tb.say("チェックアランド、ラストカード")
        tb.now = 38.0
        tb.board(5, "Td")
        tb.now = 40.0
        tb.say("2千点")
        tb.say("コール、ショーダウン")
        tb.say("5 6 7 7 6 9")
        tb.say("7")
        tb.say("ハンド終了")

        assert tb.played() == [
            ("preflop", 6, "call", 200), ("preflop", 4, "fold", 0),
            ("preflop", 5, "raise", 1000), ("preflop", 6, "call", 800),
            ("flop", 5, "bet", 600), ("flop", 6, "call", 600),
            ("turn", 5, "check", 0), ("turn", 6, "check", 0),
            ("river", 5, "bet", 2000), ("river", 6, "call", 2000),
        ]
        (hand,) = tb.hands
        assert hand.winner_seat == 5 and hand.winner_source == "cards"
        assert hand.pot_total == 7300
        assert any("前のストリートのコール" in n for n in tb.notices)
        assert any("数字だけの「7」" in n and "ベッティングは終わっています" in n for n in tb.notices)
        assert tb.card_lines == [
            "手札 席4 6♥ Q♦ ／ 席5 6♣ 7♣ ／ 席6 9♥ 6♠",
            "フロップ 3♦ J♠ 7♥",
            "ターン 5♥（ボード 3♦ J♠ 7♥ 5♥）",
            "リバー 10♦（ボード 3♦ J♠ 7♥ 5♥ 10♦）",
            "ハンド 1: ボード 3♦ J♠ 7♥ 5♥ 10♦ ／ 席4 6♥ Q♦ ／ 席5 6♣ 7♣（ワンペア）"
            " ／ 席6 9♥ 6♠（ハイカード）",
        ]

    def test_bets_from_numbers_are_not_marked_for_review(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal()
        tb.say("コール コール チェック")
        tb.say("600点")
        (bet,) = [a for a in tb.actions if a.action == "bet"]
        assert bet.amount == 600 and not bet.needs_review and "amount_only" in bet.reason


class TestAmountOnly:
    def test_a_number_facing_a_bet_is_a_raise(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal()
        tb.say("600")                                     # BTN（最初の手番）
        assert tb.played()[-1] == ("preflop", 6, "raise", 600)

    @pytest.mark.parametrize("text, reason", [
        ("200点", "いまのベット 200 以下"),                 # コールの額
        ("7", "いまのベット 200 以下"),
    ])
    def test_amounts_that_are_not_a_raise_are_ignored(self, tmp_path, text, reason):
        tb = _Table(tmp_path)
        tb.deal()
        tb.say(text)
        assert tb.played() == [] and reason in tb.notices[-1]

    def test_a_bet_below_the_big_blind_is_ignored(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal()
        tb.say("コール コール チェック")
        tb.say("100")
        assert tb.played()[-1][2] == "check" and "最小ベット 200" in tb.notices[-1]

    def test_repeating_the_bet_before_the_call_is_not_a_raise(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal()
        tb.say("コール コール チェック")
        tb.say("600点")
        tb.say("600点、600点コールです")
        assert tb.played()[-2:] == [("flop", 4, "bet", 600), ("flop", 5, "call", 600)]


class TestCheckAround:
    def test_everyone_left_checks(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal()
        tb.say("コール コール チェック")
        tb.say("チェック")                                  # 1 人目は個別に言った
        tb.say("チェックアラウンド")
        assert [a[:3] for a in tb.played()[-3:]] == [
            ("flop", 4, "check"), ("flop", 5, "check"), ("flop", 6, "check")]
        assert tb.gs.street == "turn"

    def test_on_the_river_it_ends_the_betting(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal()
        tb.say("コール コール チェック")
        for _ in range(3):
            tb.say("チェックアラウンド")
        assert tb.t._betting_over()   # noqa: SLF001
        assert any("ショーダウン" in n for n in tb.notices)

    def test_facing_a_bet_it_is_one_check(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal()
        tb.say("コール コール チェック")
        tb.say("ベット 600")
        tb.say("チェックアラウンド")
        last = tb.actions[-1]
        assert (last.seat, last.action, last.needs_review) == (5, "call", True)

    def test_said_again_before_the_next_card_it_is_ignored(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal()
        tb.say("コール コール チェック")
        tb.now = 20.0
        for i, card in enumerate(["2c", "7d", "9s"], start=1):
            tb.board(i, card)
        tb.now = 25.0
        tb.say("チェック チェック チェック")               # ラウンドが閉じてターンへ
        tb.say("チェックアラウンド")                        # まとめて言い直した（ターンの札はまだ）
        assert tb.gs.street == "turn" and tb.played()[-1][0] == "flop"
        assert "チェックアラウンド" in tb.notices[-1]


class TestStaleCall:
    def test_a_call_on_a_new_street_before_its_card_is_ignored(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal()
        tb.say("コール コール チェック")
        tb.now = 20.0
        for i, card in enumerate(["2c", "7d", "9s"], start=1):
            tb.board(i, card)
        tb.say("ベット 600 コール コール")                  # ターンへ（札はまだ）
        tb.say("コール")
        assert tb.played()[-1] == ("flop", 6, "call", 600)

    def test_a_call_after_the_card_is_still_read(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal()
        tb.say("コール コール チェック")
        tb.now = 20.0
        for i, card in enumerate(["2c", "7d", "9s"], start=1):
            tb.board(i, card)
        tb.now = 25.0
        tb.say("コール")                                   # ベットの聞き落とし（従来どおりチェック + 要確認）
        last = tb.actions[-1]
        assert (last.street, last.action, last.needs_review) == ("flop", "check", True)


class TestCardLines:
    def test_seats_read_after_the_start_are_shown_when_complete(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal({4: ["6h", "Qd"], 5: ["6c", "7c"]})
        for card in ("9h", "6s"):
            tb.t._process_rfid_event(RFIDEvent(   # noqa: SLF001
                tag_id=card, card=card, reader_id="r5", role="seat", seat=6,
                timestamp=tb.now, raw_tag_id=card))
        assert tb.card_lines == ["手札 席4 6♥ Q♦ ／ 席5 6♣ 7♣", "手札 席6 9♥ 6♠"]

    def test_a_redealt_board_card_is_shown(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal()
        tb.say("コール コール チェック")
        for i, card in enumerate(["2c", "7d", "9s"], start=1):
            tb.board(i, card)
        tb.board(2, "8d")
        assert tb.card_lines[-1] == "ボード 2 枚目を差し替え 7♦ → 8♦（2♣ 8♦ 9♠）"

    def test_a_fold_out_summary_lists_the_cards(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal()
        tb.say("フォールド フォールド")
        assert tb.card_lines[-1] == "ハンド 1: 席4 6♥ Q♦ ／ 席5 6♣ 7♣ ／ 席6 9♥ 6♠"

    @pytest.mark.parametrize("card, text", [("Td", "10♦"), ("As", "A♠"), ("7c", "7♣"), ("?E0:04", "?E0:04")])
    def test_card_text(self, card, text):
        assert _card_text(card) == text


class TestParsing:
    @pytest.mark.parametrize("text, expected", [
        ("600点", [("bet", 600, ("amount_only",))]),
        ("2千点", [("bet", 2000, ("amount_only",))]),
        ("はい、600点です", [("bet", 600, ("amount_only",))]),
        ("600、600", [("bet", 600, ("amount_only",))]),
        ("600点コールです。", [("call", 600, ())]),
        ("2千点、コール、ショーダウン",
         [("bet", 2000, ("amount_only",)), ("call", 0, ()), ("showdown", 0, ())]),
        ("コール、2千点、コール", [("call", 0, ()), ("bet", 2000, ("amount_only",)), ("call", 0, ())]),
        ("レイズ 600、コール", [("raise", 600, ()), ("call", 0, ())]),
        ("チェックアランド、ラストカード", [("check", 0, ("check_around",))]),
        ("チェックアラウンド", [("check", 0, ("check_around",))]),
        ("5 6 7 7 6 9", []),
        ("7ヒット!", []),
        ("ポット2千点", []),
        ("残り1500", []),
        ("ヒットアップです。", []),
    ])
    def test_utterances(self, text, expected):
        assert [(e.action, e.amount, e.parse_flags) for e in parse_actions(text)] == expected

    def test_seats_stay_with_their_part(self):
        events = parse_actions("シート3 600点、シート4 コール")
        assert [(e.action, e.amount, e.seat) for e in events] == [("bet", 600, 3), ("call", 0, 4)]

    def test_description(self):
        (amount,) = parse_actions("600点")
        (around,) = parse_actions("チェックアラウンド")
        assert describe_event(amount) == "bet/raise 600 （数字だけ）"
        assert describe_event(around) == "check 全員"


class TestNoise:
    @pytest.mark.parametrize("text", [
        "シート4 レイズ 2千、コール、チェック、フォールド、オールイン、ショーダウン、ウィナー、フォールド、"
        "オールイン、ショーダウン、ウィナー、ハンド、チェック、フォールド、オールイン",
        "シート4 レイズ 2千、コール、チェック、フォールド、オールイン、ショーダウン、ウィナー、ハンド、"
        "フォールド、オールイン、ショーダウン、ウィナー、",
        "ご視聴ありがとうございました。",
        "この動画をご覧頂きましてありがとうございます。",
        "ご覧いただきありがとうございます。",
    ])
    def test_store_hallucinations_are_noise(self, text):
        assert is_prompt_echo(text)

    @pytest.mark.parametrize("text", ["フォールド、オールイン、コール", "コール、チェック、フォールド",
                                      "チェック、チェック、600点"])
    def test_real_sequences_are_not(self, text):
        assert not is_prompt_echo(text)
