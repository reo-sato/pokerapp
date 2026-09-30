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
    PARAMS,
    READING_OVERRIDES,
    HandWindow,
    SessionEstimator,
    _Row,
    _restates,
    _street_open,
    align_words,
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
        """各発話でちょうど 1 つの読みを選び、その確からしさを足す: 直しの無い候補にも既定の読み（定型の幻聴の下で
        第 2 の耳が何かを聞いた発話を捨てる）の確からしさが入る（監査 2 回目）。"""
        rows = ({"utterance_start_ts": T0 + 10, "text": "ご覧いただきありがとうございます。",
                 "ear": {"text": "コール", "logp": -3.0, "candidates": []}},
                {"utterance_start_ts": T0 + 20, "text": "コール", "confidence": 0.9})
        est = _estimator(transcripts=rows)
        assert est._default_logp.get(T0 + 10) == READING_OVERRIDES["drop_heard"]
        assert est._default_logp.get(T0 + 20) == 0.0
        _, terms, _ = est.score(_window(), self._hand(), [], self._INFO, ())
        assert [v for name, v in terms if name.startswith("既定の読み")] == [READING_OVERRIDES["drop_heard"]]

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
        monkeypatch.setitem(module.READING_OVERRIDES, "drop_heard", -4.0)
        assert params_hash() != before


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
        assert sum(v for _, v in est._street_time_terms(hand, early, flags)) == pytest.approx(
            math.log(PARAMS["p_street_time"]))
        assert flags == ["turn のアクションが札より前（席4 fold）"]
        assert est._street_time_terms(hand, on_time, []) == []


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
