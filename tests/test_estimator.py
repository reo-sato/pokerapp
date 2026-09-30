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
    HandWindow,
    SessionEstimator,
    _Row,
    _restates,
    align_words,
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

    def test_the_word_long_before_the_departure_is_not_that_fold(self):
        est = _estimator(_presence({5: T0 + 40.0}))
        near, _ = self._terms(est, 5, T0 + 39.9)
        far, flags = self._terms(est, 5, T0 + 30.0)          # 10 秒前の声（別の話）
        assert near - far > 3.0 and "札が離れないフォールド（river 席5）" in flags

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


class TestStreetTime:
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
