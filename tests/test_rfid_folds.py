"""tests/test_rfid_folds.py

フォールドは席の札の離脱で決める（オーナー決定 2026-09-25）:

- 札が 3 秒離れたまま戻らなければフォールド。卓の中央を通過したら待たない。
- 音声の「フォールド」は次の手番の人に付けない。札が離れかけている席があれば、その席をすぐフォールドにする。
  札が残っていれば別の解釈（何もしない）。
- 手番でない席の札が離れた = その前の人のアクションが聞き取れなかった → 間の人をチェック / コールで補う。
- 札が戻ったら、少なくともその時刻まではフォールドではない → 入れたフォールドを取り消して組み直す。
- ショーダウン（ベッティングが終わったあと）は札を前に出すので、離脱はマックにしない。
- 最後の 1 人を残すフォールドは確定を待つ（音声の「フォールド」・10 秒・次の配布で確定、「ショーダウン」で取り消し）。
- 札の離脱は、それより前に話し始めた発話を反映してから入れる。記録した leave / return / confirm で replay も同じになる。
"""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from audio.recognizer import parse_actions
from core.event_queue import make_audio_queue
from core.events import RFIDEvent
from core.game_state import PlayerState
from integration.engine import FOLDOUT_CONFIRM_SEC, IntegrationThread
from output.json_writer import JsonWriter

pytest.importorskip("pokerkit")

from core.poker_engine import PokerkitGameState  # noqa: E402
from integration.replay import replay_events  # noqa: E402

HOLES = {4: ["6s", "Qc"], 5: ["4s", "7c"], 6: ["Qd", "Ac"]}   # 店舗の 2 ハンド目（ボタン 席6）


class _Recorder:
    def __init__(self) -> None:
        self.events: list = []

    def record(self, event) -> None:
        self.events.append(event)


class _Table:
    """席の在否（札を置く・持ち上げる・中央を通す）と時刻を手で動かす卓。"""

    def __init__(self, tmp_path: Path, seats=(4, 5, 6)):
        self.seats = seats
        self.gs = PokerkitGameState(
            [PlayerState(seat=s, name=f"P{s}", stack=10000) for s in seats], sb=100, bb=200)
        self.now = 0.0
        self.cards: dict[int, list[str]] = {}
        self.absent_since: dict[int, float] = {}
        self.mucked_at: dict[int, float] = {}
        self.speech_since: float | None = None
        self.hands: list = []
        self.actions: list = []
        self.notices: list[str] = []
        self.recorder = _Recorder()
        self.t = IntegrationThread(
            audio_queue=make_audio_queue(), game_state=self.gs,
            json_writer=JsonWriter(tmp_path, "folds"), on_hand=self.hands.append,
            on_action=self.actions.append, on_notice=self.notices.append,
            stop_event=threading.Event(), clock=lambda: self.now, event_recorder=self.recorder,
            auto_new_hand=True, auto_winner=True, rfid_folds=True,
            seat_presence=self._presence, board_presence=lambda: {"present_count": 0},
            speech_pending_since=lambda: self.speech_since,
        )

    def _presence(self) -> dict:
        return {
            s: {"present": bool(self.cards.get(s)), "cards": list(self.cards.get(s, [])),
                "absent_since": self.absent_since.get(s), "mucked_at": self.mucked_at.get(s)}
            for s in self.seats
        }

    def deal(self, holes=HOLES) -> None:
        for seat, cards in holes.items():
            self.put(seat, cards)
            for card in cards:
                ev = RFIDEvent(tag_id=card, card=card, reader_id=f"r{seat}", role="seat", seat=seat,
                               timestamp=self.now, raw_tag_id=card)
                self.recorder.record(ev)
                self.t._process_rfid_event(ev)   # noqa: SLF001
        self.tick(self.now + 2.0)
        assert self.t._hand_open                # noqa: SLF001

    def put(self, seat: int, cards) -> None:
        self.cards[seat] = list(cards)
        self.absent_since.pop(seat, None)

    def lift(self, seat: int) -> None:
        self.cards[seat] = []
        self.absent_since[seat] = self.now

    def muck(self, seat: int) -> None:
        self.lift(seat)
        self.mucked_at[seat] = self.now

    def tick(self, until: float, step: float = 0.1) -> None:
        """run() の 1 周（在否の確認 + 発話が無いときの処理）を `until` まで回す。"""
        while self.now < until - 1e-9:
            self.now = round(self.now + step, 3)
            self.t._check_deal_presence()        # noqa: SLF001
            self.t._poll_departures()            # noqa: SLF001
            self.t._apply_idle_departures()      # noqa: SLF001
            self.t._check_foldout_timeout()      # noqa: SLF001

    def say(self, text: str, spoken_at: float | None = None) -> None:
        start = self.now if spoken_at is None else spoken_at
        for event in parse_actions(text, confidence=0.9, utterance_start_ts=start):
            event.timestamp = self.now
            self.recorder.record(event)
            self.t._handle_audio_event(event)    # noqa: SLF001

    def played(self) -> list[tuple]:
        return [(a.street, a.seat, a.action, a.amount) for a in self.t._current_actions]   # noqa: SLF001


class TestDepartures:
    def test_the_actor_folds_when_the_cards_stay_off_for_three_seconds(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal()
        tb.lift(6)                                  # BTN（最初の手番）
        tb.tick(tb.now + 2.8)
        assert tb.played() == []
        tb.tick(tb.now + 0.5)
        assert tb.played() == [("preflop", 6, "fold", 0)]
        assert tb.actions[-1].actor_source == "rfid_departure" and not tb.actions[-1].needs_review
        tb.say("コール")
        assert tb.played()[-1] == ("preflop", 4, "call", 100)

    def test_a_peek_is_not_a_fold(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal()
        tb.lift(6)
        tb.tick(tb.now + 2.0)
        tb.put(6, HOLES[6])
        tb.tick(tb.now + 5.0)
        tb.say("600")
        assert tb.played() == [("preflop", 6, "raise", 600)]

    def test_a_card_through_the_middle_folds_at_once(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal()
        tb.muck(6)
        tb.tick(tb.now + 0.3)
        assert tb.played() == [("preflop", 6, "fold", 0)]
        assert tb.actions[-1].actor_source == "rfid_muck"


class TestFoldWord:
    def test_not_given_to_the_next_actor(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal()
        tb.say("フォールド")
        assert tb.played() == [] and "札が席に残っている" in tb.notices[-1]

    def test_it_confirms_a_departure_early(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal()
        tb.lift(6)
        tb.tick(tb.now + 1.0)
        tb.say("フォールド")
        assert tb.played() == [("preflop", 6, "fold", 0)]


class TestMissedActions:
    """手番でない席の札が離れた = 前の人のアクションが聞き取れなかった（店舗の 2 ハンド目）。"""

    def _hand_b_preflop(self, tb: _Table, fold_word: bool) -> None:
        tb.deal()
        tb.say("ロープヘッグ")                      # 席6 のレイズ 600 が読めなかった
        tb.lift(4)                                  # 席4（SB）がフォールド
        tb.tick(tb.now + 4.0)
        assert tb.played() == []                   # 手番でない離脱は次のアクションまで待つ
        if fold_word:
            tb.say("フォールド")
        tb.say("2500")
        tb.say("コール")

    @pytest.mark.parametrize("fold_word", [True, False])
    def test_store_hand_b(self, tmp_path, fold_word):
        tb = _Table(tmp_path)
        self._hand_b_preflop(tb, fold_word)
        assert tb.played() == [
            ("preflop", 6, "call", 200), ("preflop", 4, "fold", 0),
            ("preflop", 5, "raise", 2500), ("preflop", 6, "call", 2300),
        ]
        implied = tb.t._current_actions[0]          # noqa: SLF001
        assert implied.actor_source == "implied" and implied.needs_review
        assert tb.gs.pot == 5100

    def test_a_departure_from_an_earlier_street_waits_for_its_turn(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal()
        tb.say("コール コール チェック")            # フロップへ（席4 が先）
        tb.tick(tb.now + 0.5)
        tb.lift(6)                                  # フロップが配られる前に持ち上げた（席6 は最後の手番）
        tb.tick(tb.now + 1.5)
        for i, card in enumerate(["2c", "7d", "9s"], start=1):
            tb.t._process_rfid_event(RFIDEvent(     # noqa: SLF001
                tag_id=card, card=card, reader_id="b", role="board", seat=None,
                timestamp=tb.now, raw_tag_id=card, board_index=i))
        tb.tick(tb.now + 4.0)
        tb.say("チェック")                          # 席4
        assert tb.played()[-1] == ("flop", 4, "check", 0)
        tb.say("ベット 600")                        # 席5
        assert tb.played()[-2:] == [("flop", 5, "bet", 600), ("flop", 6, "fold", 0)]


class TestReturn:
    def test_a_fold_is_undone_when_the_cards_come_back(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal()
        tb.lift(6)                                  # 札を手に持って考えている
        tb.tick(tb.now + 3.5)
        assert tb.played() == [("preflop", 6, "fold", 0)]
        tb.say("コール")                            # 席6 のコール（札はまだ手の中）
        assert tb.played()[-1] == ("preflop", 4, "call", 100)
        tb.put(6, HOLES[6])                         # 札が戻った = フォールドではなかった
        tb.tick(tb.now + 0.2)
        assert tb.played() == [("preflop", 6, "call", 200)]
        assert any("取り消して記録を組み直しました" in n for n in tb.notices)


class TestShowdown:
    def _to_showdown(self, tb: _Table) -> None:
        tb.deal()
        tb.say("コール コール チェック")
        for _ in range(3):
            tb.say("チェックアラウンド")
        assert tb.t._betting_over()                 # noqa: SLF001

    def test_cards_pushed_forward_are_not_mucks(self, tmp_path):
        tb = _Table(tmp_path)
        self._to_showdown(tb)
        for seat in (4, 5, 6):
            tb.lift(seat)
        tb.tick(tb.now + 5.0)
        assert tb.hands == [] and [a for a in tb.played() if a[2] == "fold"] == []

    def test_the_dealer_says_fold_for_a_muck(self, tmp_path):
        tb = _Table(tmp_path, seats=(4, 5))
        tb.deal({4: ["6s", "Qc"], 5: ["4s", "7c"]})
        tb.say("コール チェック")
        for _ in range(3):
            tb.say("チェックアラウンド")
        tb.lift(4)
        tb.lift(5)
        tb.tick(tb.now + 5.0)
        tb.say("フォールド")                        # 見せずにマック（アウトオブポジションから）
        (hand,) = tb.hands
        assert hand.winner_source == "fold"


class TestLastFold:
    def _two_folds(self, tb: _Table) -> None:
        tb.deal()
        tb.lift(6)
        tb.tick(tb.now + 3.5)                       # 席6 フォールド
        tb.lift(4)
        tb.tick(tb.now + 3.5)                       # 席4 フォールド → 席5 だけ

    def test_it_waits_then_confirms(self, tmp_path):
        tb = _Table(tmp_path)
        self._two_folds(tb)
        assert tb.hands == [] and tb.t._foldout_pending is not None   # noqa: SLF001
        tb.tick(tb.now + FOLDOUT_CONFIRM_SEC)
        (hand,) = tb.hands
        assert hand.winner_seat == 5 and hand.winner_source == "fold"

    def test_the_dealer_confirms_it(self, tmp_path):
        tb = _Table(tmp_path)
        self._two_folds(tb)
        tb.say("フォールド")
        assert len(tb.hands) == 1 and tb.hands[0].winner_seat == 5

    def test_the_cards_coming_back_undo_it(self, tmp_path):
        tb = _Table(tmp_path)
        self._two_folds(tb)
        tb.put(4, HOLES[4])
        tb.tick(tb.now + FOLDOUT_CONFIRM_SEC + 1)
        assert tb.hands == [] and tb.played() == [("preflop", 6, "fold", 0)]

    def test_a_showdown_undoes_it(self, tmp_path):
        tb = _Table(tmp_path, seats=(4, 5))
        tb.deal({4: ["6s", "Qc"], 5: ["4s", "7c"]})
        tb.say("コール チェック")
        for _ in range(2):
            tb.say("チェックアラウンド")
        tb.say("ベット 1000")                       # リバー: 席4（BB が先）のベット
        tb.lift(5)                                  # 席5 のコールは聞き取れず、札を前に出した
        tb.tick(tb.now + 3.5)
        assert tb.t._foldout_pending is not None    # noqa: SLF001
        tb.say("ショーダウン")
        assert tb.played()[-1][1:] == (5, "call", 1000)
        assert tb.t._betting_over() and tb.hands == []   # noqa: SLF001


class TestOrderingAndReplay:
    def test_speech_before_the_departure_goes_first(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal()
        tb.speech_since = tb.now                    # 席6 のレイズが認識中
        spoken = tb.now
        tb.tick(tb.now + 1.0)
        tb.lift(6)                                  # レイズのあと札を持ち上げた
        tb.tick(tb.now + 5.0)
        assert tb.played() == []                   # 認識待ちの発話より前には入れない
        tb.speech_since = None
        tb.say("600", spoken_at=spoken)
        tb.tick(tb.now + 1.0)
        assert tb.played() == [("preflop", 6, "raise", 600)]

    def test_replay_matches_live(self, tmp_path):
        tb = _Table(tmp_path)
        TestMissedActions()._hand_b_preflop(tb, fold_word=False)
        tb.lift(5)
        tb.tick(tb.now + 3.5)                       # フロップ: 席5 フォールド → 席6 だけ
        tb.tick(tb.now + FOLDOUT_CONFIRM_SEC)
        (live,) = tb.hands
        replayed = replay_events(
            tb.recorder.events, backend="pokerkit",
            players=[PlayerState(seat=s, name=f"P{s}", stack=10000) for s in (4, 5, 6)],
            sb=100, bb=200, session_id="replay", out_dir=tmp_path / "replay",
            auto_new_hand=True, auto_winner=True, rfid_folds=True,
        )
        assert len(replayed) == 1
        assert ([(a.street, a.seat, a.action, a.amount) for a in replayed[0].actions]
                == [(a.street, a.seat, a.action, a.amount) for a in live.actions])
        assert replayed[0].winner_seat == live.winner_seat == 6


def test_recorded_seat_signals_match_the_schema(tmp_path):
    import json

    jsonschema = pytest.importorskip("jsonschema")
    from output.event_recorder import event_to_envelope

    schema = json.loads((Path(__file__).resolve().parents[1]
                         / "docs/contracts/schemas/reconstruction_event.schema.json").read_text(encoding="utf-8"))
    tb = _Table(tmp_path)
    tb.deal()
    tb.lift(6)
    tb.tick(tb.now + 3.5)
    tb.put(6, HOLES[6])
    tb.tick(tb.now + 0.3)
    kinds = [e.kind for e in tb.recorder.events if isinstance(e, RFIDEvent)]
    assert "leave" in kinds and "return" in kinds
    for event in tb.recorder.events:
        jsonschema.validate(event_to_envelope(event), schema)
