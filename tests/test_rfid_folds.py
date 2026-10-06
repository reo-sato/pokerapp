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
from integration.engine import FOLDOUT_CONFIRM_SEC, POST_FOLDOUT_LISTEN_SEC, IntegrationThread
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
            self.t._apply_idle_observations()    # noqa: SLF001
            self.t._check_foldout_timeout()      # noqa: SLF001
            self.t._check_showdown_timeout()     # noqa: SLF001
            self.t._check_post_hand_listening()  # noqa: SLF001

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


class TestUnreadSeat:
    """このハンドで札が一度も読めていない席（2026-10-01 のリハーサル: 席の設定が 1 つずれて席 7 のリーダーに札が
    無く、各ハンドの最初の「フォールド」が席 7 に付いて、その前の人がコールで補われた）。"""

    def test_a_fold_word_does_not_go_to_a_seat_whose_cards_were_never_read(self, tmp_path):
        tb = _Table(tmp_path, seats=(4, 5, 6, 7))       # ボタン 席7 → 最初の手番は 席6
        tb.deal()                                       # 席7 の札は読めていない
        tb.say("フォールド")
        assert tb.played() == []                       # 前は「席6 コール（補い）・席7 フォールド」
        tb.lift(6)
        tb.tick(tb.now + 3.5)
        assert tb.played() == [("preflop", 6, "fold", 0)]

    def test_an_unread_actor_folds_on_the_next_action(self, tmp_path):
        tb = _Table(tmp_path, seats=(4, 5, 6, 7))
        tb.deal({4: HOLES[4], 5: HOLES[5], 7: HOLES[6]})   # 手番の 席6 の札が読めていない
        tb.say("フォールド")
        tb.say("600")
        assert tb.played() == [("preflop", 6, "fold", 0), ("preflop", 7, "raise", 600)]


class TestSeatSetup:
    """卓で使っていない席に手札がある = 起動時の席の設定が違う（2026-10-01 のリハーサル: ロガーは席 4〜7、
    札は席 3〜6 のリーダー）。配り終わるのを待ってから、ハンドごとに 1 回知らせる。"""

    def _deal_with_seat_3(self, tb: _Table) -> None:
        tb.deal()                                       # 席4・5・6（ロガーの席）
        for card in ("Kh", "Kd"):                       # 4 人目の札は 席3 のリーダー（ロガーの席ではない）
            tb.t._process_rfid_event(RFIDEvent(        # noqa: SLF001
                tag_id=card, card=card, reader_id="r3", role="seat", seat=3, timestamp=tb.now,
                raw_tag_id=card))

    def test_cards_on_a_seat_outside_the_game_are_reported(self, tmp_path):
        tb = _Table(tmp_path, seats=(4, 5, 6, 7))
        self._deal_with_seat_3(tb)
        warnings = [n for n in tb.notices if "席の設定" in n]
        tb.t._check_seat_setup()                        # noqa: SLF001 — 配って 5 秒たつまでは言わない
        assert [n for n in tb.notices if "席の設定" in n] == warnings == []
        tb.tick(tb.now + 5.0)
        tb.t._check_seat_setup()                        # noqa: SLF001
        tb.t._check_seat_setup()                        # noqa: SLF001 — 1 ハンドに 1 回
        warnings = [n for n in tb.notices if "席の設定" in n]
        assert len(warnings) == 1
        assert "手札は 席3・4・5・6" in warnings[0] and "ロガーの席は 4・5・6・7" in warnings[0]
        assert "席7 に手札がありません" in warnings[0]

    def test_no_warning_when_the_cards_are_on_the_logger_seats(self, tmp_path):
        tb = _Table(tmp_path, seats=(4, 5, 6, 7))
        tb.deal()
        tb.tick(tb.now + 6.0)
        tb.t._check_seat_setup()                        # noqa: SLF001
        assert not [n for n in tb.notices if "席の設定" in n or "入っていません" in n]


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


class TestListeningAfterAFoldOut:
    """全員フォールドで確定したのに 2 席以上に札が載っていれば（降りた判断が早すぎた疑い）、`POST_FOLDOUT_LISTEN_SEC`
    まで聞き取りを続けて書き起こしに残す（記録には入れない）。札が片付く・次の配布で止める。"""

    def _fold_out(self, tb: _Table, *, cards_back: bool) -> None:
        tb.t._listen_gate = threading.Event()       # noqa: SLF001
        tb.deal()
        assert tb.t._listen_gate.is_set()           # noqa: SLF001
        tb.lift(6)
        tb.tick(tb.now + 3.5)                       # 席6 フォールド
        tb.lift(4)
        tb.tick(tb.now + 3.5)                       # 席4 フォールド → 席5 だけ（確定待ち）
        if cards_back:
            tb.put(4, HOLES[4])                     # 席4 の札が載ったまま確定する
        tb.say("フォールド")                        # ディーラーの宣言で確定
        assert len(tb.hands) == 1 and tb.hands[0].winner_source == "fold"

    def test_it_keeps_listening_while_two_seats_hold_cards(self, tmp_path):
        tb = _Table(tmp_path)
        self._fold_out(tb, cards_back=True)
        gate = tb.t._listen_gate                    # noqa: SLF001
        assert gate.is_set()
        tb.say("ベット 2000")                       # ハンドの外 = 記録には入らない
        assert tb.actions[-1].actor_source == "unresolved" and len(tb.hands) == 1
        tb.tick(tb.now + POST_FOLDOUT_LISTEN_SEC - 1.0)
        assert gate.is_set()
        tb.tick(tb.now + 2.0)
        assert not gate.is_set()                   # 時間切れ

    def test_it_stops_when_the_cards_are_collected(self, tmp_path):
        tb = _Table(tmp_path)
        self._fold_out(tb, cards_back=True)
        tb.lift(4)
        tb.lift(5)
        tb.tick(tb.now + 0.2)
        assert not tb.t._listen_gate.is_set()      # noqa: SLF001

    def test_a_clean_fold_out_stops_listening_at_once(self, tmp_path):
        tb = _Table(tmp_path)
        self._fold_out(tb, cards_back=False)        # 札が残っているのは勝った 席5 だけ
        assert not tb.t._listen_gate.is_set()      # noqa: SLF001


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


class TestGarbledCall:
    """「これで終わりです」はディーラーが言わない = 常にほかの語の聞き違い（オーナー 2026-10-06）。言葉と同じころ
    （`GARBLED_FOLD_BEFORE_SEC` 前〜`GARBLED_FOLD_AFTER_SEC` 後）に札が離れた席があれば、その席のフォールドの語。
    そうでなく手番の人がベットに向き合っていればコール（要確認）。どちらでもなければ記録しない。"""

    def _replayed(self, tb: _Table, tmp_path: Path) -> list[tuple]:
        replayed = replay_events(
            tb.recorder.events, backend="pokerkit",
            players=[PlayerState(seat=s, name=f"P{s}", stack=10000) for s in (4, 5, 6)],
            sb=100, bb=200, session_id="replay", out_dir=tmp_path / "replay",
            auto_new_hand=True, auto_winner=True, rfid_folds=True,
        )
        return [(a.street, a.seat, a.action, a.amount) for h in replayed for a in h.actions]

    def test_facing_a_bet_it_is_a_call(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal()
        tb.say("レイズ 600")                        # 席6
        tb.say("これで終わりです。")                # 席4
        assert tb.played()[-1] == ("preflop", 4, "call", 500)
        record = tb.t._current_actions[-1]          # noqa: SLF001
        assert record.needs_review and "garbled_call" in record.reason

    def test_cards_leaving_with_the_word_make_it_that_seats_fold(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal()
        tb.say("レイズ 600")
        tb.lift(4)                                  # 席4 の札が離れた
        spoken = tb.now + 0.1
        tb.tick(tb.now + 1.5)
        tb.say("これで終わりです。", spoken_at=spoken)
        assert tb.played()[-1] == ("preflop", 4, "fold", 0)
        tb.say("コール")
        assert tb.played()[-1] == ("preflop", 5, "call", 400)
        tb.lift(5)                                  # フロップの前に席5 も降りた → 席6 の勝ち
        tb.tick(tb.now + 3.5 + FOLDOUT_CONFIRM_SEC)
        (live,) = tb.hands
        assert self._replayed(tb, tmp_path) == [(a.street, a.seat, a.action, a.amount) for a in live.actions]

    def test_a_fold_already_taken_from_the_cards_takes_the_word(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal()
        tb.say("レイズ 600")
        tb.lift(4)
        lifted = tb.now
        tb.tick(tb.now + 3.5)                       # 席4 は札の離脱でフォールド
        assert tb.played()[-1] == ("preflop", 4, "fold", 0)
        tb.say("これで終わりです。", spoken_at=lifted + 0.5)
        assert tb.played()[-1] == ("preflop", 4, "fold", 0) and "席4 のフォールド" in tb.notices[-1]
        tb.say("コール")
        assert tb.played()[-1] == ("preflop", 5, "call", 400)

    def test_cards_leaving_later_are_the_showdown(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal()
        tb.say("レイズ 600")
        tb.lift(4)
        tb.tick(tb.now + 1.0)
        tb.say("これで終わりです。", spoken_at=tb.now - 3.0)   # 札が離れる 2 秒前の言葉 = 別の語
        assert tb.played()[-1] == ("preflop", 4, "call", 500)

    def test_with_nothing_to_call_it_is_not_recorded(self, tmp_path):
        tb = _Table(tmp_path)
        tb.deal()
        for text in ("コール", "コール", "チェック"):
            tb.say(text)
        tb.say("これで終わりです。")
        assert [p for p in tb.played() if p[0] == "flop"] == [] and "記録しませんでした" in tb.notices[-1]


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


class TestStoreSessions:
    """店舗の 4 回目の通しテスト（音声のアクションなし）: ショーダウンで前に出した札をフォールドにしていた。

    ボードの札でストリートを進め（前のラウンドは札が残っている人のチェック / コールで閉じる）、リバーで
    ベットが無いときの札の離脱はチェック（ショーダウンに向けて前に出した）にする。
    """

    def _board(self, tb: _Table, index: int, card: str) -> None:
        ev = RFIDEvent(tag_id=card, card=card, reader_id="b", role="board", seat=None,
                       timestamp=tb.now, raw_tag_id=card, board_index=index)
        tb.recorder.record(ev)
        tb.t._process_rfid_event(ev)   # noqa: SLF001

    def _play(self, tb: _Table, holes, board, departures) -> None:
        tb.deal(holes)
        t0 = tb.now
        events = [(at, "board", (i, card)) for i, (at, card) in enumerate(board, start=1)]
        events += [(at, "lift", seat) for at, seat in departures]
        for at, kind, arg in sorted(events, key=lambda e: e[0]):
            tb.tick(t0 + at)
            if kind == "board":
                self._board(tb, *arg)
            elif arg == 4 and holes is SESSION_A:
                tb.muck(arg)                        # 席4 の札は卓の中央を通った
            else:
                tb.lift(arg)
        tb.tick(t0 + 95)

    def _next_deal(self, tb: _Table) -> None:
        tb.cards = {}
        tb.deal({4: ["As", "Ad"], 5: ["Ks", "Kd"], 6: ["Qs", "Qd"]})

    def test_unread_flop_card_d0f055fb_hand_5(self, tmp_path):
        """店舗 2026-09-29 d0f055fb ハンド 5（オーナーのメモ）: フロップの 1 枚（6s）が読めず、ターンの Kc は 4 枚目、
        リバーの 9s は 5 枚目（RFIDThread がフロップに充当しない）。リバーで札を前に出した席4 はフォールドではなく
        ショーダウン。読めていない 1 枚しだいで勝者が変わる（8・7 なら席4）ので、チップは動かさない。"""
        tb = _Table(tmp_path)
        holes = {4: ["8h", "7d"], 5: ["2d", "Tc"], 6: ["9d", "Td"]}
        tb.deal(holes)
        t0 = tb.now
        for at, kind, arg in [(15.9, "board", (2, "Kh")), (16.7, "board", (1, "3c")), (23.2, "lift", 6),
                              (28.8, "board", (4, "Kc")), (44.0, "board", (5, "9s")), (49.6, "lift", 4)]:
            tb.tick(t0 + at)
            if kind == "board":
                self._board(tb, *arg)
            else:
                tb.lift(arg)
        assert tb.t._board_cards == ["3c", "Kh", "??", "Kc", "9s"]          # noqa: SLF001
        tb.tick(t0 + 95)
        played = tb.played()
        assert {a[0] for a in played} >= {"preflop", "flop", "turn", "river"}
        assert ("flop", 6, "fold", 0) in played
        assert [a for a in played if a[1] == 4 and a[2] == "fold"] == []    # 前に出した札はフォールドにしない
        tb.cards = {}
        tb.deal({4: ["As", "Ad"], 5: ["Ks", "Kd"], 6: ["Qs", "Qd"]})
        (hand,) = tb.hands
        assert hand.board == ["3c", "Kh", "??", "Kc", "9s"]
        assert hand.winner_seat is None and hand.winner_source == "undetermined" and hand.review_required
        assert all(p["result"] == 0 for p in hand.players)

    def test_session_a(self, tmp_path):
        tb = _Table(tmp_path)
        self._play(tb, SESSION_A,
                   board=[(14.5, "5d"), (15.0, "Jc"), (17.7, "Th"), (36.6, "8c"), (53.1, "5h")],
                   departures=[(6.6, 4), (63.1, 5), (66.7, 6)])
        # ショーダウンで見せる・マックが無いまま SHOWDOWN_MUCK_SEC たった = 手札で決める（次の配布まで待たない）
        (hand,) = tb.hands
        played = [(a.street, a.seat, a.action, a.amount) for a in hand.actions]
        assert ("preflop", 4, "fold", 0) in played
        assert [a for a in played if a[2] == "fold" and a[1] in (5, 6)] == []
        assert played[-2:] == [("river", 5, "check", 0), ("river", 6, "check", 0)]
        assert (hand.winner_seat, hand.winner_source) == (6, "cards")   # J と 8 のツーペア

    def test_session_b(self, tmp_path):
        tb = _Table(tmp_path)
        self._play(tb, SESSION_B,
                   board=[(18.1, "Jd"), (21.0, "Ac"), (21.6, "3d"), (44.1, "9d"), (50.8, "5h")],
                   departures=[(32.2, 4), (72.9, 5), (75.2, 6)])
        (hand,) = tb.hands                                                # ショーダウン → 手札で決めた
        played = [(a.street, a.seat, a.action, a.amount) for a in hand.actions]
        assert ("flop", 4, "fold", 0) in played                           # フロップで降りた
        assert [a for a in played if a[2] == "fold" and a[1] in (5, 6)] == []
        assert (hand.winner_seat, hand.winner_source) == (6, "cards")   # J のペア、K キッカー

    def test_replay_matches_live(self, tmp_path):
        tb = _Table(tmp_path)
        self._play(tb, SESSION_B,
                   board=[(18.1, "Jd"), (21.0, "Ac"), (21.6, "3d"), (44.1, "9d"), (50.8, "5h")],
                   departures=[(32.2, 4), (72.9, 5), (75.2, 6)])
        self._next_deal(tb)
        (live,) = tb.hands
        replayed = replay_events(
            tb.recorder.events, backend="pokerkit",
            players=[PlayerState(seat=s, name=f"P{s}", stack=10000) for s in (4, 5, 6)],
            sb=100, bb=200, session_id="replay", out_dir=tmp_path / "replay",
            auto_new_hand=True, auto_winner=True, rfid_folds=True,
        )
        assert ([(a.street, a.seat, a.action, a.amount) for a in replayed[0].actions]
                == [(a.street, a.seat, a.action, a.amount) for a in live.actions])
        assert replayed[0].winner_seat == 6

    def test_new_cards_on_the_seat_are_not_a_return(self, tmp_path):
        tb = _Table(tmp_path)
        self._play(tb, SESSION_B,
                   board=[(18.1, "Jd"), (21.0, "Ac"), (21.6, "3d"), (44.1, "9d"), (50.8, "5h")],
                   departures=[(32.2, 4), (72.9, 5), (75.2, 6)])
        self._next_deal(tb)
        assert not any("取り消して" in n for n in tb.notices)


SESSION_A = {4: ["2c", "Kh"], 5: ["6d", "7h"], 6: ["Js", "8s"]}
SESSION_B = {4: ["6s", "Qd"], 5: ["4c", "Jh"], 6: ["Jc", "Kh"]}


class TestSilentMic:
    """ワイヤレスマイクの電池切れでハンドが丸ごと記録されなかった（店舗の 4 回目の通しテスト）。"""

    def _table(self, tmp_path, heard):
        tb = _Table(tmp_path)
        tb.t._voice_heard_at = lambda: heard["at"]   # noqa: SLF001
        return tb

    def test_a_silent_mic_is_reported_once(self, tmp_path):
        heard = {"at": None}
        tb = self._table(tmp_path, heard)
        tb.deal()
        for _ in range(3):
            tb.now += 30
            tb.t._check_silent_mic()                 # noqa: SLF001
        warnings = [n for n in tb.notices if "マイクに声が入っていません" in n]
        assert len(warnings) == 1

    def test_no_warning_while_the_dealer_speaks(self, tmp_path):
        heard = {"at": None}
        tb = self._table(tmp_path, heard)
        tb.deal()
        for _ in range(4):
            tb.now += 30
            heard["at"] = tb.now - 5
            tb.t._check_silent_mic()                 # noqa: SLF001
        assert not any("マイクに声が入っていません" in n for n in tb.notices)
