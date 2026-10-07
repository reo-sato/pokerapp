"""tests/test_estimator.py

推定器 v1（`integration/estimator.py`）: 生の観測の再生（world_replay）に直し（聞こえなかったアクション・余計な語・
ボタン・持ち上げ）を入れ、log 事後確率で筋の通る記録を選ぶ。採点の各項（語と行の対応・札の離脱の時刻・言い直し・
ストリートと札の時刻）と、店舗の真のアクションのあるハンドでの振る舞いを確かめる。
"""
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path

import pytest

from core.events import AudioEvent
from integration.estimator import (
    CONTENT_FILES,
    EXPLANATION_MARK,
    PARAMS,
    READING_OVERRIDES,
    HandWindow,
    SessionEstimator,
    _Row,
    _restates,
    _street_open,
    align_words,
    content_hash,
    hand_from_key,
    params_hash,
    record_key,
)
from integration.world_replay import PresenceTimeline, SeatObservation

STORE = Path(__file__).resolve().parent / "fixtures" / "store"
T0 = datetime(2026, 9, 29, 12, 0, 0).timestamp()


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t).isoformat(timespec="milliseconds")


def _word(action: str, start: float, delivered: float, text: str = "", amount: int = 0) -> AudioEvent:
    return AudioEvent(action=action, amount=amount, timestamp=delivered, raw_text=text or action,
                      utterance_start_ts=start)


def _row(street: str, seat: int, action: str, t: float, source: str = "engine_prior", amount: int = 0,
         reason: str = "") -> dict:
    return {"street": street, "seat": seat, "action": action, "amount": amount, "timestamp": _iso(t),
            "actor_source": source, "reason": reason}


def _estimator(presence: PresenceTimeline | None = None, transcripts: tuple = ()) -> SessionEstimator:
    setup = {"players": [{"seat": s, "name": n, "stack": 10000} for s, n in ((4, "A"), (5, "B"), (6, "C"))],
             "sb": 100, "bb": 200}
    return SessionEstimator([], list(transcripts), presence, setup, {}, "s")


def _window() -> HandWindow:
    return HandWindow(hand_id=1, start=T0, end=T0 + 120.0,
                      base={"players": [{"seat": 4}, {"seat": 5}, {"seat": 6}], "button_seat": 6})


def _presence(departures: dict[int, float]) -> PresenceTimeline:
    """席ごとに T0 から札が載り、`departures` の時刻に離れて戻らない在否。"""
    seats = {}
    for seat in (4, 5, 6):
        obs = [SeatObservation(T0, True, None, None)]
        if seat in departures:
            obs.append(SeatObservation(departures[seat] + 0.5, False, departures[seat], None))
        seats[seat] = obs
    return PresenceTimeline(seats)


class TestAlignWords:
    def test_rows_and_words(self):
        call = _word("call", T0 + 10, T0 + 11)
        fold = _word("fold", T0 + 20, T0 + 21)
        late_fold = _word("fold", T0 + 31, T0 + 32)          # 札の離脱で作ったフォールドを言ったもの
        around = _word("check", T0 + 40, T0 + 41, "チェックアラウンド")
        chatter = _word("bet", T0 + 50, T0 + 51, "千円", amount=1000)
        actions = [
            _row("preflop", 4, "call", T0 + 11, amount=200),
            _row("preflop", 5, "fold", T0 + 20, source="spoken_fold"),
            _row("preflop", 6, "fold", T0 + 30, source="rfid_departure"),
            _row("flop", 4, "check", T0 + 41, reason="check_around"),
            _row("flop", 6, "check", T0 + 41, reason="check_around"),
            _row("flop", 5, "check", T0 + 45),                    # 入れた（聞こえなかった）行
        ]
        inserted = {_iso(T0 + 45): "check"}
        rows, unused = align_words(actions, [call, fold, late_fold, around, chatter], inserted)
        assert [r.word for r in rows] == [call, fold, late_fold, around, around, None]
        assert rows[3].collective and rows[4].collective
        assert unused == [chatter]

    def test_record_key_ignores_showdown_mucks(self):
        hand = {"actions": [_row("river", 4, "call", T0 + 1, amount=500)], "winner_seat": 4, "board": []}
        muck = dict(hand, actions=hand["actions"] + [_row("showdown", 5, "fold", T0 + 5)])
        assert record_key(hand) == record_key(muck)


class TestDepartures:
    """札の離脱: 「フォールド」の声とほぼ同時に離れた席がフォールドした席（店舗 20 回で中央値 0.1 秒）。"""

    def _terms(self, est: SessionEstimator, fold_seat: int, word_start: float, caller: int | None = None):
        word = _word("fold", word_start, word_start + 0.8)
        hand = {"players": [{"seat": 4}, {"seat": 5}, {"seat": 6}], "winner_source": "fold"}
        betting = [_Row("river", 6, "bet", T0 + 25, _word("bet", T0 + 25, T0 + 26, amount=2000)),
                   _Row("river", fold_seat, "fold", word_start, word, source="spoken_fold")]
        if caller is not None:
            betting.append(_Row("river", caller, "call", T0 + 50, _word("call", T0 + 50, T0 + 51)))
        flags: list[str] = []
        terms = est._departure_terms(_window(), hand, betting, flags)
        return sum(v for _, v in terms), flags

    def test_the_folder_is_the_seat_whose_cards_left_with_the_word(self):
        est = _estimator(_presence({5: T0 + 30.03}))
        right, right_flags = self._terms(est, 5, T0 + 30.0, caller=4)
        wrong, wrong_flags = self._terms(est, 4, T0 + 30.0, caller=5)
        assert right - wrong > 5.0
        assert right_flags == []
        assert "札が離れないフォールド（river 席4）" in wrong_flags and "残っている席5 の札が離れた" in wrong_flags

    def test_the_word_long_before_the_departure_is_unlikely_but_not_impossible(self):
        """声から 10 秒離れた離脱は裾（語が発話の後ろの方・早いマック, ±20 秒）で受ける = ほぼ同時よりずっと低いが、
        「離脱が無い + 残っている席の離脱」（−8.8）ほどは罰しない（監査 2 回目）。"""
        est = _estimator(_presence({5: T0 + 40.0}))
        near, _ = self._terms(est, 5, T0 + 39.9)
        far, flags = self._terms(est, 5, T0 + 30.0)
        assert near - far > 5.0 and flags == []
        assert far > math.log(PARAMS["p_nodepart"]) + math.log(1 / PARAMS["lift_sec"])

    def test_a_flicker_is_not_a_fold(self):
        """すぐ戻った離脱（ちらつき・のぞき見）はどの仮説でも数えない。"""
        seats = {s: [SeatObservation(T0, True, None, None)] for s in (4, 5, 6)}
        seats[5] += [SeatObservation(T0 + 30.5, False, T0 + 30.0, None), SeatObservation(T0 + 31.5, True, None, None)]
        est = _estimator(PresenceTimeline(seats))
        total, flags = self._terms(est, 5, T0 + 30.0, caller=4)
        assert "札が離れないフォールド（river 席5）" in flags

    def test_a_seat_never_read_is_not_evidence(self):
        """その席の札がハンドで一度も読めていなければ、フォールドの札が離れなくても罰しない（監査 2 回目）。"""
        unread = _estimator(PresenceTimeline({s: [SeatObservation(T0, True, None, None)] for s in (4, 6)}))
        total, flags = self._terms(unread, 5, T0 + 30.0, caller=4)
        assert (total, flags) == (0.0, [])
        _, read_flags = self._terms(_estimator(_presence({})), 5, T0 + 30.0, caller=4)
        assert read_flags == ["札が離れないフォールド（river 席5）"]

    def test_cards_that_come_back_were_not_folded(self):
        """フォールドした札はそのハンドの中では戻らない: 10 秒後に戻った離脱（持ち上げ）はフォールドの離脱にしない。"""
        seats = {s: [SeatObservation(T0, True, None, None)] for s in (4, 5, 6)}
        seats[5] += [SeatObservation(T0 + 30.5, False, T0 + 30.0, None), SeatObservation(T0 + 40.0, True, None, None)]
        _, flags = self._terms(_estimator(PresenceTimeline(seats)), 5, T0 + 30.0, caller=4)
        assert "札が離れないフォールド（river 席5）" in flags

    def test_departures_long_after_the_hand_are_not_scored(self):
        """離脱を数える窓はハンドの終わり + 60 秒まで（次の配布の揺れ・片付けをこのハンドの離脱として採点しない）。"""
        est = _estimator(_presence({5: T0 + 30.03, 4: T0 + 400.0}))
        hand = {"players": [{"seat": 4}, {"seat": 5}, {"seat": 6}], "winner_source": "fold"}
        betting = [_Row("river", 6, "bet", T0 + 25, _word("bet", T0 + 25, T0 + 26, amount=2000)),
                   _Row("river", 5, "fold", T0 + 30.0, _word("fold", T0 + 30.0, T0 + 30.8), source="spoken_fold")]

        def names(ended: float) -> list[str]:
            w = HandWindow(hand_id=1, start=T0, end=T0 + 600.0,
                           base={"players": hand["players"], "button_seat": 6, "ended_at": _iso(T0 + ended)})
            return [name for name, _ in est._departure_terms(w, hand, betting, [])]

        assert not any(name.startswith("席4") for name in names(60.0))
        assert "席4 の離脱（ショーダウン・片付け）" in names(380.0)

    def test_an_unannounced_fold_leaves_a_few_seconds_after_the_bet(self):
        est = _estimator(_presence({5: T0 + 28.0}))
        hand = {"players": [{"seat": 4}, {"seat": 5}, {"seat": 6}], "winner_source": "fold"}
        bet = _Row("river", 6, "bet", T0 + 25, _word("bet", T0 + 25, T0 + 26, amount=2000))
        quick = [bet, _Row("river", 5, "fold", T0 + 28, None, source="rfid_departure")]
        slow_est = _estimator(_presence({5: T0 + 45.0}))
        slow = [bet, _Row("river", 5, "fold", T0 + 45, None, source="rfid_departure")]
        q = sum(v for _, v in est._departure_terms(_window(), hand, quick, []))
        s = sum(v for _, v in slow_est._departure_terms(_window(), hand, slow, []))
        assert q > s


class TestDepartureEvidence:
    """店舗 2026-10-06 の見直し（Fable 5.1 の助言 P2〜P5）: 聞き違いのコールと札の離脱の組・片付けの一瞬の読み取り・
    フォールドのあとも札が残る・ベットの無いところのフォールド・記録上の離脱は読み取りのラグで語のあとになりやすい。"""

    def test_a_blip_does_not_end_an_absence(self):
        seats = {s: [SeatObservation(T0, True, None, None)] for s in (4, 5, 6)}
        seats[5] += [SeatObservation(T0 + 30.5, False, T0 + 30.0, None),
                     SeatObservation(T0 + 50.0, True, None, None),          # 片付けで 0.1 秒だけ読めた
                     SeatObservation(T0 + 50.1, False, T0 + 50.1, None)]
        presence = PresenceTimeline(seats)
        assert presence.departures(T0, T0 + 120) == [(5, T0 + 30.0, T0 + 50.0), (5, T0 + 50.1, None)]
        assert presence.departures(T0, T0 + 120, blip_sec=1.0, settle_sec=3.0) == [(5, T0 + 30.0, None)]
        _, flags = TestDepartures()._terms(_estimator(presence), 5, T0 + 30.0, caller=4)
        assert flags == []

    def test_a_flicker_is_still_a_flicker(self):
        """短い不在のあとの短い読み取り（読み取りのちらつき）はつなげない。"""
        seats = {5: [SeatObservation(T0, True, None, None), SeatObservation(T0 + 10.5, False, T0 + 10.0, None),
                     SeatObservation(T0 + 11.0, True, None, None), SeatObservation(T0 + 11.2, False, T0 + 11.2, None),
                     SeatObservation(T0 + 12.0, True, None, None)]}
        assert PresenceTimeline(seats).departures(T0, T0 + 60, blip_sec=1.0, settle_sec=3.0) == [
            (5, T0 + 10.0, T0 + 11.0), (5, T0 + 11.2, T0 + 12.0)]

    def test_cards_still_on_the_seat_long_after_the_fold(self):
        """フォールドから 10 秒たっても札が席にある（店舗の真のフォールド 0/172）は、離脱が見えないだけより重い。"""
        est = _estimator(_presence({}))
        _, flags = TestDepartures()._terms(est, 4, T0 + 30.0, caller=5)
        hand = {"players": [{"seat": 4}, {"seat": 5}, {"seat": 6}], "winner_source": "fold"}
        betting = [_Row("river", 6, "bet", T0 + 25, _word("bet", T0 + 25, T0 + 26, amount=2000)),
                   _Row("river", 4, "fold", T0 + 30.0, _word("fold", T0 + 30.0, T0 + 30.8), source="spoken_fold")]
        terms = dict(est._departure_terms(_window(), hand, betting, []))
        assert terms["席4 はフォールドのあとも札が席に載っていた"] == pytest.approx(math.log(PARAMS["p_present_after_fold"]))
        assert "札が離れないフォールド（river 席4）" in flags
        ending = HandWindow(hand_id=1, start=T0, end=T0 + 120.0,
                            base={"players": hand["players"], "button_seat": 6, "ended_at": _iso(T0 + 35.0)})
        short = dict(est._departure_terms(ending, dict(hand, ended_at=_iso(T0 + 35.0)), betting, []))
        assert short["席4 のフォールドに札の離脱が無い"] == pytest.approx(math.log(PARAMS["p_nodepart"]))

    def test_a_momentary_read_after_the_fold_is_not_cards_staying(self):
        """フォールドの 10 秒後に一瞬だけ読めた（片付けの札がかすめた）のは「札が残っていた」にしない。"""
        seats = {s: [SeatObservation(T0, True, None, None)] for s in (4, 5, 6)}
        seats[4] = [SeatObservation(T0, True, None, None), SeatObservation(T0 + 25.5, False, T0 + 25.0, None),
                    SeatObservation(T0 + 39.8, True, None, None), SeatObservation(T0 + 40.3, False, T0 + 40.3, None)]
        est = _estimator(PresenceTimeline(seats))
        assert est._present_at(4, T0 + 40.0, T0 + 120.0) is False
        assert est._present_at(5, T0 + 40.0, T0 + 120.0) is True
        assert est._present_at(5, T0 + 40.0, T0 + 30.0) is False          # ハンドの終わりのあと

    def test_an_open_fold_is_unlikely(self):
        est = _estimator()
        hand = {"players": [{"seat": 4}, {"seat": 5}, {"seat": 6}], "winner_source": "fold"}
        open_fold = [_Row("flop", 4, "check", T0 + 20, _word("check", T0 + 20, T0 + 21)),
                     _Row("flop", 5, "fold", T0 + 25, None, source="rfid_departure", reasons=frozenset({"no_bet"}))]
        flags: list[str] = []
        terms = est._action_terms(hand, open_fold, flags)
        assert ("ベットの無いところのフォールド", math.log(PARAMS["p_open_fold"])) in terms
        assert "ベットの無いところのフォールド（flop 席5）" in flags
        facing = [_Row("flop", 4, "bet", T0 + 20, _word("bet", T0 + 20, T0 + 21, amount=400)),
                  _Row("flop", 5, "fold", T0 + 25, None, source="rfid_departure")]
        assert not any(name == "ベットの無いところのフォールド" for name, _ in est._action_terms(hand, facing, []))

    def test_a_recorded_departure_after_the_word_is_read_lag(self):
        """ディーラーは札が離れるのを見てから「フォールド」と言う（オーナー 2026-10-06）。記録上の離脱が語のあとに
        なるのは読み取りのラグ（3 周続けて読めなかったら離れたとする ≈ 1 秒 + 札がリーダーの近くに残る）なので、
        語のあとの離脱は、同じだけ前の離脱より起こりやすい。"""
        est = _estimator(_presence({5: T0 + 31.5}))
        late, _ = TestDepartures()._terms(est, 5, T0 + 30.0, caller=4)
        early_est = _estimator(_presence({5: T0 + 28.5}))
        early, _ = TestDepartures()._terms(early_est, 5, T0 + 30.0, caller=4)
        assert late > early

    def test_a_garbled_call_word_pairs_with_the_departure(self):
        garbled = _word("call", T0 + 30.0, T0 + 31.0, "これで終わります")
        garbled.parse_flags = ("garbled_call",)
        actions = [_row("river", 6, "bet", T0 + 26, amount=2000),
                   _row("river", 5, "fold", T0 + 30.1, source="rfid_departure")]
        rows, unused = align_words(actions, [_word("bet", T0 + 25, T0 + 26, amount=2000), garbled], {})
        assert rows[1].word is garbled and unused == []
        far = _row("river", 5, "fold", T0 + 33.0, source="rfid_departure")     # 語の 3 秒あとの離脱は組にしない
        _, unused = align_words([actions[0], far], [_word("bet", T0 + 25, T0 + 26, amount=2000), garbled], {})
        assert unused == [garbled]

    def test_a_garbled_call_used_as_a_call_is_not_taken_by_a_fold(self):
        garbled = _word("call", T0 + 30.0, T0 + 31.0, "コールド")
        garbled.parse_flags = ("garbled_call",)
        actions = [_row("river", 5, "fold", T0 + 30.2, source="rfid_departure"),
                   _row("river", 6, "call", T0 + 31.0, amount=2000)]
        rows, _ = align_words(actions, [garbled], {})
        assert rows[0].word is None and rows[1].word is garbled


class TestWords:
    def test_the_last_fold_is_often_not_announced(self):
        est = _estimator()
        assert est._p_miss("fold", "call", last_fold=True) == PARAMS["p_miss_last_fold"]
        assert est._p_miss("fold", "call") == PARAMS["p_miss_fold"]
        assert est._p_miss("check", "check") == PARAMS["p_miss_repeat"]
        assert est._p_miss("raise", "call") == PARAMS["p_miss_wager"]

    def test_a_repeated_word_is_a_restatement(self):
        rows = [{"utterance_start_ts": T0 + 10, "text": "チェック、チェック。"},
                {"utterance_start_ts": T0 + 15, "text": "コールします。 コール。"},
                {"utterance_start_ts": T0 + 20, "text": "これはなんかちょっと割れてはいるけどね、チェック"}]
        est = _estimator(transcripts=tuple(rows))
        # 言い方を変えた繰り返しは言い直し、同じ語の繰り返しは 2 人分のことも多い（台本はすべて 2 人分）
        assert est._p_phantom(T0 + 15, (("call", "コールします"), ("call", "コール")), 1) == PARAMS["p_restate"]
        assert est._p_phantom(T0 + 10, (("check", "チェック"), ("check", "チェック")), 1) == PARAMS["p_restate_same"]
        assert est._p_phantom(T0 + 10, (("check", "チェック"),), 0) == PARAMS["p_phantom_short"]
        assert est._p_phantom(T0 + 20, (("check", "チェック"),), 0) == PARAMS["p_phantom"]

    def test_an_amount_or_all_in_said_again_restates_the_row(self):
        raised = _Row("preflop", 4, "raise", T0 + 10, _word("raise", T0 + 10, T0 + 11, amount=2500),
                      raw={"amount": 2500})
        allin = _Row("turn", 4, "allin", T0 + 40, _word("allin", T0 + 40, T0 + 41), raw={"amount": 2700})
        rows = [raised, allin]
        assert _restates(_word("raise", T0 + 14, T0 + 15, amount=2500), rows)
        assert not _restates(_word("raise", T0 + 14, T0 + 15, amount=3000), rows)
        assert _restates(_word("allin", T0 + 45, T0 + 46), rows)
        assert not _restates(_word("allin", T0 + 80, T0 + 81), rows)     # 30 秒より後は別の話


class TestSumRules:
    """監査 2 回目 (e): 総和則をテストで固定する。"""

    @pytest.mark.parametrize("action,previous,winner_source", [
        ("check", None, "cards"), ("check", "check", "cards"), ("call", "call", "cards"),
        ("fold", None, "cards"), ("fold", None, "fold"), ("bet", None, "cards")])
    def test_said_and_unsaid_come_from_the_same_probability(self, action, previous, winner_source):
        est = _estimator()
        hand = {"actions": [], "winner_source": winner_source}

        def last_row_term(said: bool) -> float:
            rows = [] if previous is None else [_Row("flop", 4, previous, T0 + 5, _word(previous, T0 + 5, T0 + 6))]
            word = _word(action, T0 + 10, T0 + 11, amount=500 if action == "bet" else 0) if said else None
            rows.append(_Row("flop", 5, action, T0 + 10, word))
            terms = est._action_terms(hand, rows, [])
            return next(v for name, v in reversed(terms) if name.startswith(("言われた", "言われない")))

        assert math.exp(last_row_term(True)) + math.exp(last_row_term(False)) == pytest.approx(1.0)

    @pytest.mark.parametrize("seats,button", [((4, 5, 6), 6), ((1, 2, 3, 5, 7, 9), 3)])
    def test_the_button_prior_sums_to_one_at_any_table_size(self, seats, button):
        est = _estimator()
        w = HandWindow(hand_id=1, start=T0, end=T0 + 60.0,
                       base={"players": [{"seat": s} for s in seats], "button_seat": button})
        assert sum(math.exp(est._button_logp(w, s)) for s in seats) == pytest.approx(1.0)

    def test_a_neighbour_is_the_likelier_button_mistake(self):
        """動かし忘れ・動かしすぎ（隣の席）を 4 倍厚く。"""
        est = _estimator()
        w = HandWindow(hand_id=1, start=T0, end=T0 + 60.0,
                       base={"players": [{"seat": s} for s in (1, 2, 3, 5, 7, 9)], "button_seat": 3})
        assert est._button_logp(w, 2) == pytest.approx(est._button_logp(w, 5))
        assert est._button_logp(w, 5) - est._button_logp(w, 7) == pytest.approx(math.log(4))


class TestHandTerms:
    _INFO = {"tokens": [], "inserted": {}, "button": 6}

    def _hand(self, **extra) -> dict:
        return {"players": [{"seat": s} for s in (4, 5, 6)], "actions": [], "winner_seat": 4,
                "winner_source": "fold", **extra}

    def test_a_hand_that_ends_with_someone_still_to_act_is_unlikely(self):
        """実卓のハンドは全員が降りるかショーダウンでしか終わらない（語を捨てて短くしたハンドが勝者の操作で終わる
        別解を止める = 監査 2 回目の要約 2）。"""
        est = _estimator()
        closed, _, closed_flags = est.score(_window(), self._hand(), [], self._INFO, ())
        opened, _, open_flags = est.score(_window(), self._hand(betting_open_at_end=True), [], self._INFO, ())
        assert opened - closed == pytest.approx(math.log(PARAMS["p_unclosed"]), abs=1e-3)
        assert "ベッティングが閉じないまま終わった" in open_flags and not closed_flags

    def test_every_utterance_adds_the_reading_it_takes(self):
        """各発話でちょうど 1 つの読みを選び、その確からしさを足す: 直しの無い候補にも既定の読み（決まり文句になった
        発話 = 雑談だった log(1 − r)）の確からしさが入る（監査 2 回目）。"""
        rows = ({"utterance_start_ts": T0 + 10, "text": "ご覧いただきありがとうございます。",
                 "ear": {"text": "コール", "logp": -3.0, "candidates": []}},
                {"utterance_start_ts": T0 + 20, "text": "コール", "confidence": 0.9})
        est = _estimator(transcripts=rows)
        chatter = math.log(1 - READING_OVERRIDES["canned_action"])
        assert est._default_logp.get(T0 + 10) == pytest.approx(chatter)
        assert est._default_logp.get(T0 + 20) == 0.0
        _, terms, _ = est.score(_window(), self._hand(), [], self._INFO, ())
        assert [v for name, v in terms if name.startswith("既定の読み")] == [pytest.approx(chatter)]

    def test_a_canned_phrase_may_hide_an_action(self):
        """決まり文句になってライブの規則でも読めなかった発話は「雑談だった / アクションを言った」の 2 つの状態。
        アクションはフォールド・チェック・コールに一様 + 第 2 の耳の候補（作業計画の監査 2026-10-01 の最優先）。
        どれかは推定器がハンドの筋で決める。"""
        r = READING_OVERRIDES["canned_action"]
        rows = ({"utterance_start_ts": T0 + 10, "text": "ご視聴ありがとうございました。",
                 "ear": {"text": "", "logp": -1.0, "candidates": []}},)
        est = _estimator(transcripts=rows)
        options = est._options(T0 + 10)
        assert options[0].source == "drop" and options[0].logp == pytest.approx(math.log(1 - r))
        assert sorted((o.text, round(o.logp, 6)) for o in options[1:]) == sorted(
            (w, round(math.log(r / 3), 6)) for w in ("フォールド", "チェック", "コール"))
        # 確からしさの和は 1（雑談 + アクション）
        assert sum(math.exp(o.logp) for o in options) == pytest.approx(1.0)
        # 選択点にするのは読めた賭けの語の近く（`canned_near_sec` 以内）の決まり文句だけ（ハンドの途中の長い雑談は広げない）
        near = [e for e in est.candidate_edits(_window(), {"tokens": [_word("call", T0 + 12, T0 + 13)]})
                if e.kind == "read"]
        far = [e for e in est.candidate_edits(_window(), {"tokens": [_word("call", T0 + 40, T0 + 41)]})
               if e.kind == "read"]
        assert sorted(e.value for e in near) == sorted(["フォールド", "チェック", "コール"]) and far == []
        inserts = {e.at for e in est.candidate_edits(_window(), {"tokens": [_word("call", T0 + 12, T0 + 13)]})
                   if e.kind == "insert"}
        assert T0 + 10 - 0.3 in inserts                   # 決まり文句の前にも聞こえなかったアクションを入れられる

    def test_showdown_words_say_the_hand_was_not_folded_out(self):
        """ショーダウンの声（「ショーダウン」・役の名前）があるのに全員降りて終わったハンドは低い（真のアクションの
        92 ハンド: 声のあった 23 ハンドはすべてショーダウン、全員降りた 45 ハンドで声 0）。声の数によらず 1 つ。"""
        est = _estimator()
        words = [AudioEvent(action="end_hand", amount=0, timestamp=T0 + 30, raw_text="ツーペア",
                            utterance_start_ts=T0 + 29, hand_name="two_pair"),
                 AudioEvent(action="showdown", amount=0, timestamp=T0 + 31, raw_text="ショーダウン",
                            utterance_start_ts=T0 + 30.5)]
        info = dict(self._INFO, tokens=words)
        folded, terms, flags = est.score(_window(), self._hand(), [], info, ())
        shown, _, shown_flags = est.score(_window(), self._hand(winner_source="cards"), [], info, ())
        quiet, _, _ = est.score(_window(), self._hand(), [], self._INFO, ())
        assert [v for n, v in terms if n.startswith("ショーダウンの声")] == [
            pytest.approx(math.log(PARAMS["p_showdown_word"]))]
        assert "ショーダウンの声があるのに全員降りて終わった" in flags and not shown_flags
        assert shown - folded == pytest.approx(math.log(1 - PARAMS["p_showdown_word"])
                                               - math.log(PARAMS["p_showdown_word"]), abs=1e-3)
        assert quiet - folded == pytest.approx(-math.log(PARAMS["p_showdown_word"]), abs=1e-3)

    def test_an_amount_the_second_ear_also_heard_differently_is_reviewed(self):
        """賭けの額の発話に、選んだ読みから `review_margin` 以内の別の額の読みがあれば要確認（採点は変えない）。
        第 2 の耳がはっきり別の額を下に見ている（候補の確からしさが 5 離れた）ときは付けない。"""
        ear = {"text": "八百", "logp": -1.0, "candidates": [{"text": "八百", "logp": -1.0}, {"text": "二百", "logp": -6.0}]}
        rows = ({"utterance_start_ts": T0 + 10, "text": "ご視聴ありがとうございました。",
                 "ear": dict(ear, candidates=[*ear["candidates"], {"text": "六百", "logp": -1.5}])},
                {"utterance_start_ts": T0 + 20, "text": "ご視聴ありがとうございました。", "ear": ear})
        est = _estimator(transcripts=rows)
        close = _word("bet", T0 + 10, T0 + 11, "八百", amount=800)
        clear = _word("bet", T0 + 20, T0 + 21, "八百", amount=800)
        rows_ = [_Row("flop", 4, "bet", T0 + 11, close), _Row("turn", 5, "bet", T0 + 21, clear)]
        assert est._amount_readings(rows_, ()) == ["額の読みが 2 通り（flop 席4 800 / 600）"]

    def test_the_reading_values_are_in_the_fingerprint(self, monkeypatch):
        import integration.estimator as module

        before = params_hash()
        monkeypatch.setitem(module.READING_OVERRIDES, "canned_action", 0.1)
        assert params_hash() != before


def _heads_up_river(last_word: str) -> tuple[SessionEstimator, HandWindow]:
    """ヘッズアップ（席4 = ボタン・SB、席5 = BB）を声だけで: リバーの 500 のあとのコールが決まり文句になった。"""
    rows = [{"utterance_start_ts": T0 + s, "audio_sec": 0.9, "text": text, "confidence": 0.9}
            for s, text in ((2, "コール"), (4, "チェック"), (6, "チェック"), (8, "チェック"), (10, "チェック"),
                            (12, "チェック"), (14, "500"), (20, last_word))]
    rows.insert(7, {"utterance_start_ts": T0 + 17, "audio_sec": 1.0, "text": "ご覧いただきありがとうございます。",
                    "confidence": 0.3, "ear": {"text": "", "logp": -1.0, "candidates": []}})
    setup = {"players": [{"seat": s, "name": f"P{s}", "stack": 10000} for s in (4, 5)], "sb": 100, "bb": 200,
             "button_prior": 5}
    typed = [AudioEvent(action="new_hand", amount=0, timestamp=T0, raw_text="")]
    est = SessionEstimator(typed, rows, None, setup, {"auto_new_hand": False, "rfid_folds": False}, "s")
    return est, est.windows()[0]


class TestCannedPhrase:
    def test_a_canned_phrase_can_be_the_call_that_closes_the_round(self):
        """役の名前（ショーダウン）の前のコールが決まり文句になった: そのままではラウンドが閉じないまま終わる。決まり文句を
        コールと読む直しで閉じる（要確認 = 直しを使った）。"""
        pytest.importorskip("pokerkit")
        est, w = _heads_up_river("ツーペア")
        base = est.replay(w, ())[0]
        result = est.estimate_hand(w)
        river = [(a["seat"], a["action"]) for a in result.best.hand["actions"] if a["street"] == "river"]
        assert base.get("betting_open_at_end") and river == [(5, "bet"), (4, "call")]
        assert [e.value for e in result.best.edits] == ["コール"] and result.reasons

    def test_a_better_explanation_of_the_same_record_is_kept(self):
        """記録を変えない直しでも、得点が良ければその記録の説明として残す: 「ショーダウン」でエンジンが補ったコール
        （言われない）より、決まり文句の下のコールの方が筋が通る（店舗 42f7b964 ハンド 2）。"""
        pytest.importorskip("pokerkit")
        est, w = _heads_up_river("ショーダウン")
        base = est.replay(w, ())[0]
        result = est.estimate_hand(w)
        river = [(a["seat"], a["action"]) for a in base["actions"] if a["street"] == "river"]
        assert river == [(5, "bet"), (4, "call")] and record_key(result.best.hand) == record_key(base)
        assert [e.value for e in result.best.edits] == ["コール"]
        # 直しの無い再生（読み直し）は候補の外に残す（物差しの「読み直し」・推定のファイルの changed が使う）
        assert result.base is not None and not result.base.edits and record_key(result.base.hand) == record_key(base)
        assert all(c.edits for c in result.candidates)
        # 記録を変えない説明だけの直し: 要確認の理由には残し（再現率を下げない）、印を付けて別に数える（監査 3 回目）
        assert result.explanation_only
        assert result.explanation_reasons and all(r.endswith(EXPLANATION_MARK) for r in result.explanation_reasons)
        assert set(result.explanation_reasons) <= set(result.reasons)

    def test_an_edit_that_changes_the_record_is_not_marked(self):
        pytest.importorskip("pokerkit")
        est, w = _heads_up_river("ツーペア")
        result = est.estimate_hand(w)
        assert not result.explanation_only and result.explanation_reasons == []
        # 候補の記録はすべて残る（別のプロセスが候補を減らして返しても物差しの内訳が変わらない）
        assert result.record_keys == [c.key() for c in result.candidates]
        assert record_key(hand_from_key(result.record_keys[0])) == result.record_keys[0]


def _two_hands(winner_at: float, winner_seat: int) -> SessionEstimator:
    """ヘッズアップ（席4 = ボタン・SB、席5 = BB）を声だけで 2 ハンド。ハンド 1 は席5 のベットに席4 が降りて席5 の
    勝ち。画面の勝者（打った `w` と同じ）が `winner_at` に届く。"""
    rows = [{"utterance_start_ts": T0 + s, "audio_sec": 0.8, "text": text, "confidence": 0.9}
            for s, text in ((2, "コール"), (4, "チェック"), (8, "500"), (10, "フォールド"),
                            (50, "コール"), (52, "チェック"))]
    setup = {"players": [{"seat": s, "name": f"P{s}", "stack": 10000} for s in (4, 5)], "sb": 100, "bb": 200,
             "button_prior": 5}
    typed = [AudioEvent(action="new_hand", amount=0, timestamp=T0, raw_text=""),
             AudioEvent(action="new_hand", amount=0, timestamp=T0 + 40, raw_text=""),
             AudioEvent(action="winner", amount=0, timestamp=winner_at, raw_text=f"シート{winner_seat} ウィナー",
                        seat=winner_seat)]
    return SessionEstimator(typed, rows, None, setup, {"auto_new_hand": False, "rfid_folds": False}, "s")


class TestWinnerAfterTheHand:
    """ハンドが終わったあとに届いた勝者の指定（打った `w`・台本の画面の勝者）が、推定の勝者と違えば要確認（ライブの
    `winner_after_hand_end` と同じ。監査 3 回目の必須 3。採点には足さない）。"""

    def test_a_different_winner_after_the_hand_is_reviewed(self):
        pytest.importorskip("pokerkit")
        est = _two_hands(T0 + 20, 4)
        w = est.windows()[0]
        result = est.estimate_hand(w)
        assert result.best.hand["winner_seat"] == 5
        assert "終わったあとの勝者の指定「シート4 ウィナー」が勝者（席5）と違う" in result.reasons
        same = _two_hands(T0 + 20, 5)
        assert not any(r.startswith("終わったあとの勝者") for r in same.estimate_hand(same.windows()[0]).reasons)

    def test_a_late_winner_after_the_next_deal_belongs_to_the_previous_hand(self):
        """次のハンドが始まってから（アクションの前・`LATE_WINNER_SEC` 以内）届いた勝者の指定は前のハンドのもの:
        次のハンドの窓で流すと、その新しいハンドをその席の勝ちで終えてしまう。"""
        pytest.importorskip("pokerkit")
        est = _two_hands(T0 + 45, 4)
        first, second = est.windows()
        assert first.late == (T0 + 45,) and second.skip == (T0 + 45,)
        assert "終わったあとの勝者の指定「シート4 ウィナー」が勝者（席5）と違う" in est.estimate_hand(first).reasons
        hand2 = est.replay(second, ())[0]
        assert [(a["seat"], a["action"]) for a in hand2["actions"]][:2] == [(5, "call"), (4, "check")]


class TestContentFingerprint:
    """推定器まわりのファイルの内容の指紋（監査 3 回目の必須 4: `params_hash` は値だけ）。"""

    def test_every_file_exists_and_a_change_moves_the_fingerprint(self, tmp_path):
        root = Path(__file__).resolve().parent.parent
        assert all((root / rel).exists() for rel in CONTENT_FILES)
        for rel in CONTENT_FILES:
            (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / rel).write_bytes((root / rel).read_bytes())
        assert content_hash(tmp_path) == content_hash(root)
        target = tmp_path / "core" / "bet_sizing.py"
        target.write_bytes(target.read_bytes().replace(b"\n", b"\r\n"))     # Windows の改行でも同じ
        assert content_hash(tmp_path) == content_hash(root)
        target.write_bytes(target.read_bytes() + b"# x\n")
        assert content_hash(tmp_path) != content_hash(root)

    # 推定器が読み込むリポジトリのファイルのうち、推定の結果を変えないもの（記録・画面・道具の補助）
    NOT_IN_FINGERPRINT = {
        "audio/devices.py": "マイクの一覧（再生では使わない）",
        "audio/recorder.py": "録音（再生では使わない）",
        "core/atomic_io.py": "ファイルの書き方",
        "core/event_queue.py": "スレッド間のキュー",
        "core/hand_correction.py": "訂正の重ね方（読む側）",
        "core/player.py": "player registry", "core/player_repository.py": "player registry",
        "core/session.py": "session layer", "core/session_repository.py": "session layer",
        "core/table_state.py": "卓状態の表示",
        "output/event_recorder.py": "観測の記録（ライブ）", "output/json_writer.py": "記録の書き出し",
        "output/table_state_writer.py": "卓状態の書き出し",
        "tools/audio_check.py": "マイクの確認の道具", "tools/pack_logs.py": "ログをまとめる道具",
        "tools/read_corpus.py": "読み上げ集の道具", "tools/test_script.py": "台本の生成",
    }

    def test_the_files_the_estimator_reads_are_fingerprinted_or_explained(self):
        """推定器・発話の読みの選択肢・再生器が読み込むリポジトリのファイルは、指紋に入れるか、入れない理由を書く
        （新しい読み込みが指紋から漏れない）。"""
        import ast

        root = Path(__file__).resolve().parent.parent

        def path_of(name: str) -> Path | None:
            for cand in (root / (name.replace(".", "/") + ".py"), root / name.replace(".", "/") / "__init__.py"):
                if cand.exists():
                    return cand
            return None

        seen: dict[str, Path] = {}
        stack = ["integration.estimator", "tools.estimate", "integration.world_replay"]
        while stack:
            name = stack.pop()
            path = path_of(name)
            if name in seen or path is None:
                continue
            seen[name] = path
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    stack += [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    stack += [node.module, *(f"{node.module}.{a.name}" for a in node.names)]
        files = {p.relative_to(root).as_posix() for p in seen.values() if p.name != "__init__.py"}
        assert files - set(CONTENT_FILES) - set(self.NOT_IN_FINGERPRINT) == set()


class TestStreetTime:
    def test_the_flop_opens_with_its_first_card(self):
        """フロップの 1 枚の読み遅れでフロップのアクションが全部「札より前」にならない（監査 2 回目: d0f055fb ハンド 5
        は 3 枚目が 12 秒遅れて読まれた）。"""
        assert _street_open({1: T0 + 10, 2: T0 + 10.5, 3: T0 + 22}, 3) == T0 + 10
        assert _street_open({1: T0 + 10, 2: T0 + 10.5, 3: T0 + 22, 4: T0 + 40}, 4) == T0 + 40
        assert _street_open({4: T0 + 40}, 5) is None

    def test_an_action_before_its_street_card_is_unlikely(self):
        est = _estimator()
        timeline = [{"index": i, "card": c, "dealt_at": _iso(T0 + t)}
                    for i, c, t in ((1, "Kd", 10), (2, "7c", 10), (3, "9h", 10), (4, "2h", 40))]
        hand = {"board_timeline": timeline}
        early = [_Row("turn", 4, "fold", T0 + 30, None, source="rfid_departure")]
        on_time = [_Row("flop", 4, "fold", T0 + 30, None, source="rfid_departure")]
        flags: list[str] = []
        # 札より前は起きない（オーナー 2026-10-07: ターンの札・宣言より前にターンのアクションは言わない）
        assert sum(v for _, v in est._street_time_terms(hand, early, flags)) == pytest.approx(
            math.log(PARAMS["p_before_street_card"]))
        assert flags == ["turn のアクションが札より前（席4 fold）"]
        assert est._street_time_terms(hand, on_time, []) == []
        # 札の 2〜3 秒前は弱い項（札が読めるのは宣言から最大 2.5 秒遅れる = `before_card_slack_sec`）
        near = [_Row("turn", 4, "fold", T0 + 37.5, None, source="rfid_departure")]
        assert sum(v for _, v in est._street_time_terms(hand, near, [])) == pytest.approx(
            math.log(PARAMS["p_street_time"]))
        # 同じ札の前の行は札 1 枚に 1 回（札が遅れて読めた = 1 つの出来事。店舗 09-27 のターン）
        two = [_Row("turn", 4, "fold", T0 + 30, None, source="rfid_departure"),
               _Row("turn", 5, "fold", T0 + 33, None, source="rfid_departure")]
        flags2: list[str] = []
        assert sum(v for _, v in est._street_time_terms(hand, two, flags2)) == pytest.approx(
            math.log(PARAMS["p_before_street_card"]))
        assert len(flags2) == 2

    def test_a_spread_out_flop_keeps_the_soft_term(self):
        """フロップの札が離れて読めた（3 枚目が 13 秒あと = 札の位置がずれた疑い, 09-29 d0f055fb ハンド 5）ハンドでは、
        札より前も弱い項のまま。"""
        est = _estimator()
        timeline = [{"index": i, "card": c, "dealt_at": _iso(T0 + t)}
                    for i, c, t in ((1, "Kd", 10), (2, "7c", 10), (3, "9h", 23), (4, "2h", 40))]
        early = [_Row("turn", 4, "fold", T0 + 30, None, source="rfid_departure")]
        assert sum(v for _, v in est._street_time_terms({"board_timeline": timeline}, early, [])) == pytest.approx(
            math.log(PARAMS["p_street_time"]))
        # フロップの 1 枚が読めていない（読めなかった札の代わりに次の札が数えられたかもしれない）も同じ
        two = [item for item in timeline if item["index"] != 3]
        assert sum(v for _, v in est._street_time_terms({"board_timeline": two}, early, [])) == pytest.approx(
            math.log(PARAMS["p_street_time"]))

    def test_the_dealers_street_call_opens_an_unread_street(self):
        """ターンの札が読めなかった: ディーラーの「ターンです」をターンの始まりにする（宣言の時刻は札より不確かなので
        弱い項）。"""
        transcripts = [{"utterance_start_ts": T0 + 38.0, "heard_at": T0 + 40.0, "text": "ターンです。", "audio_sec": 1.0}]
        est = _estimator(transcripts=transcripts)
        timeline = [{"index": i, "card": c, "dealt_at": _iso(T0 + t)}
                    for i, c, t in ((1, "Kd", 10), (2, "7c", 10), (3, "9h", 10))]
        early = [_Row("turn", 4, "fold", T0 + 30, None, source="rfid_departure")]
        assert sum(v for _, v in est._street_time_terms({"board_timeline": timeline}, early, [], _window())) == (
            pytest.approx(math.log(PARAMS["p_street_time"])))
        assert est._street_time_terms({"board_timeline": timeline}, early, []) == []   # 窓が無ければ宣言は見ない


# ───────────────────────── 店舗の真のアクションのあるハンド（開発データ）─────────────────────────


def _session(prefix: str) -> tuple[SessionEstimator, dict]:
    from integration.replay import load_events

    folder = next(STORE.glob(f"*{prefix}"))
    expected = json.loads((folder / "expected.json").read_text(encoding="utf-8"))
    setup = expected["setup"]
    presence = PresenceTimeline.from_rows(
        json.loads(line) for line in (folder / "presence.jsonl").read_text(encoding="utf-8").splitlines() if line)
    transcripts = [json.loads(line) for line in (folder / "transcripts.jsonl").read_text(encoding="utf-8").splitlines()
                   if line]
    flags = {k: bool(setup.get(k)) for k in ("auto_new_hand", "auto_winner", "rfid_folds")}
    est = SessionEstimator(load_events(folder / "events.jsonl"), transcripts, presence, setup, flags,
                           expected["session_id"])
    return est, {h["hand_id"]: h["truth"] for h in expected["hands"]}


@pytest.mark.slow       # 本番と同じ探索の幅で店舗のハンドを推定する（合わせて約 85 秒。幅は変えない = 監査 1 D2）
@pytest.mark.parametrize("prefix,hand_id,correct,review", [
    # ボタンの置き忘れ + 「チェック、チェック」の言い直し: 記録の再生では崩れ、推定で丸ごと正しい（直しを使った = 要確認）
    ("9d1d8536", 4, True, True),
    # リバーの「フォールド」と同時に席5 の札が離れた → リバーは直る。フロップの無言のチェックは声でも札でも決まらない
    ("fded6f75", 1, False, True),
    # 聞き違いの「フォールド」と「ヘッドゾップ」がそろって誤りを指す。席4 の札は最後まで離れない → 要確認
    ("027e4b15", 1, False, True),
    ("7b897671", 2, True, False),
])
def test_store_hands(prefix, hand_id, correct, review):
    pytest.importorskip("pokerkit")
    from tools.measure_capture_accuracy import hand_fully_correct

    est, truth = _session(prefix)
    w = next(x for x in est.windows() if x.hand_id == hand_id)
    result = est.estimate_hand(w)
    assert hand_fully_correct(truth[hand_id], result.best.hand) is correct
    assert bool(result.reasons) is review
    assert 0.0 < result.posteriors[0] <= 1.0 - PARAMS["outside"] + 1e-9
    if prefix == "9d1d8536":
        assert result.best.hand["button_seat"] == 5
    if prefix == "fded6f75":
        river = [(a["seat"], a["action"]) for a in result.best.hand["actions"] if a["street"] == "river"]
        assert river[-1] == (5, "fold")
