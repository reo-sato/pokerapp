"""tests/test_amount_space.py

額の候補をその場面で使える額に絞り、ポットに対する大きさで重み付けする（オーナー 2026-10-01）:

- 「可能なベット / レイズ額は最小 1bb、最大でもスタックサイズまで、かつ最小デノミネーションのチップ刻み程度の数しか
  ないので、この狭い範囲に限ってアクション額空間を事前に用意しておけば聞き取りが楽になるのでは？」
- 「現在のポットサイズに対してリーズナブルな額（多くはポットサイズ以下、多くても 3 倍）に重み付けをすると良いかも」

第 2 の耳は額ごとの点数を残す（額を読んだ発話も聞き直す）。engine は、音で読んだ額の候補・いま使えない額（最小レイズに
届かない）のときに、使える額だけから「音の点数 + ポットに対する大きさの重み」で選ぶ。推定器は engine が額を決めた印を
要確認の理由にし、賭けの大きさの重みを採点に足す。
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest

from audio import second_ear as se
from audio.recognizer import parse_actions, parse_garbled_amount, phonetic_amount_event
from core.bet_sizing import amount_prior, hand_wager_ratios, pot_fraction, size_prior
from core.engine_types import LegalContext
from core.events import AudioEvent
from integration.replay import event_from_envelope
from output.event_recorder import event_to_envelope


def _ctx(committed=0, to_call=200, min_raise=400, max_raise=10000) -> LegalContext:
    return LegalContext(actor_seat=6, legal_actions=frozenset({"fold", "call", "raise", "allin"}),
                        amount_to_call=to_call, min_raise=min_raise, max_raise=max_raise, bb=200,
                        committed=committed, chip=100)


class TestBetSizing:
    def test_a_preflop_open(self):
        """ブラインド 100/200 のポット 300 に 600 まで = 上乗せ 400 ÷ コール後のポット 500 = 0.8 倍。"""
        assert pot_fraction(600, _ctx(), 300) == pytest.approx(0.8)

    def test_a_bet(self):
        ctx = _ctx(to_call=0, min_raise=200)
        assert pot_fraction(500, ctx, 1000) == pytest.approx(0.5)

    @pytest.mark.parametrize("ratio, weight", [(0.0, 0.0), (0.8, 0.0), (1.0, 0.0), (2.0, -1.0), (3.0, -2.0),
                                               (4.0, -4.0), (50.0, -8.0)])
    def test_the_shape(self, ratio, weight):
        """ポット以下は同じ、3 倍までゆるく、その先は急に（オーナー: 多くはポット以下、多くても 3 倍）。"""
        assert size_prior(ratio) == pytest.approx(weight)

    def test_an_allin_is_never_unnatural(self):
        ctx = _ctx(max_raise=10000)
        assert size_prior(pot_fraction(10000, ctx, 300)) == -8.0
        assert amount_prior(10000, ctx, 300) == 0.0

    def test_the_ratios_of_a_recorded_hand(self):
        """店舗 d0f055fb ハンド 1（BTN 席6・SB 席4・BB 席5）: SB の 600 = 1.0 倍、フロップの 600 = 0.5 倍、
        リバーの 1500 = 0.625 倍。コールの額は上乗せ分。"""
        actions = [
            {"street": "preflop", "seat": 6, "action": "fold", "amount": 0},
            {"street": "preflop", "seat": 4, "action": "raise", "amount": 600},
            {"street": "preflop", "seat": 5, "action": "call", "amount": 400},
            {"street": "flop", "seat": 4, "action": "bet", "amount": 600},
            {"street": "flop", "seat": 5, "action": "call", "amount": 600},
            {"street": "turn", "seat": 4, "action": "check", "amount": 0},
            {"street": "turn", "seat": 5, "action": "check", "amount": 0},
            {"street": "river", "seat": 4, "action": "bet", "amount": 1500},
            {"street": "river", "seat": 5, "action": "fold", "amount": 0},
        ]
        ratios = hand_wager_ratios(actions, {"sb": 100, "bb": 200}, {"4": "SB", "5": "BB", "6": "BTN"})
        assert ratios == pytest.approx({1: 1.0, 3: 0.5, 7: 0.625})

    def test_heads_up_the_button_posts_the_small_blind(self):
        actions = [{"street": "preflop", "seat": 4, "action": "raise", "amount": 600}]
        assert hand_wager_ratios(actions, {"sb": 100, "bb": 200}, {"4": "BTN", "5": "BB"}) == pytest.approx({0: 1.0})

    def test_no_blinds_no_ratios(self):
        assert hand_wager_ratios([{"street": "preflop", "seat": 4, "action": "raise", "amount": 600}], {}, {}) == {}


class TestEarTable:
    def test_the_best_score_per_amount(self):
        candidates = se.build_candidates((300, 1200))
        scores = np.full(len(candidates), -20.0)
        by_text = {c.text: i for i, c in enumerate(candidates)}
        scores[by_text["千二百点"]] = -2.0
        scores[by_text["レイズ 千二百"]] = -1.0
        scores[by_text["三百"]] = -3.0
        assert se.amount_scores_of(candidates, scores) == [(1200, -1.0), (300, -3.0)]

    def test_hear_keeps_the_table(self):
        from tests.test_second_ear import FakeModel

        model = FakeModel(frames=5, seed=1)
        ear = se.SecondEar(model, [se.Candidate("コール", "コール", 1200), se.Candidate("チェック", "チェック", 600),
                                   se.Candidate("フォールド", "フォールド")])
        result = ear.hear(np.zeros(16000), top=2)
        assert {a for a, _ in result.amounts} == {600, 1200}
        assert result.amounts == sorted(result.amounts, key=lambda kv: -kv[1])
        row = result.to_dict()
        assert [a for a, _ in row["amounts"]] == [a for a, _ in result.amounts]
        assert se.amount_table(row) == {a: pytest.approx(s, abs=1e-3) for a, s in result.amounts}

    def test_an_old_record_has_no_table(self):
        assert se.amount_table({"text": "", "logp": -1.0, "candidates": []}) == {}
        assert se.amount_table(None) == {}


EAR = {"text": "レイズ千三百", "logp": -0.5, "candidates": [{"text": "レイズ 千三百", "logp": -0.5}],
       "amounts": [[1300, -0.5], [300, -0.9], [3000, -2.0]]}


class TestApplyEar:
    def test_one_wager_gets_the_table(self):
        (event,), used = se.apply_ear(parse_actions("レイズ 300"), "レイズ 300", EAR)
        assert used is None and (event.action, event.amount) == ("raise", 300)
        assert event.amount_scores == ((1300, -0.5), (300, -0.9), (3000, -2.0))

    def test_two_wagers_do_not(self):
        events, _ = se.apply_ear(parse_actions("500、1500"), "500、1500", EAR)
        assert len(events) == 2 and all(not e.amount_scores for e in events)

    def test_a_record_without_the_table(self):
        (event,), _ = se.apply_ear(parse_actions("レイズ 300"), "レイズ 300", {k: v for k, v in EAR.items()
                                                                            if k != "amounts"})
        assert event.amount_scores == ()

    def test_wager_utterances_are_heard_again(self):
        assert se.wants_amount_scores(parse_actions("レイズ 1200"))
        assert not se.wants_amount_scores(parse_actions("コール"))
        assert not se.wants_amount_scores(parse_actions("レイズ"))

    def test_a_garbled_amount_keeps_its_own_scores(self):
        """音の近さだけで読んだ額の候補の点数 = −10 × 距離。第 2 の耳の候補から読んだときは、その候補の点数
        （確からしさ − 10 × 距離）で、額ごとの表では上書きしない。"""
        event = parse_garbled_amount("よっしゃんてん")
        assert [a for a, _ in event.amount_scores] == [40000, 4000]
        assert event.amount_scores[0][1] > event.amount_scores[1][1]
        ear = {"text": "先天", "logp": -3.646, "candidates": [{"text": "千点", "logp": -1.944},
                                                             {"text": "四千点", "logp": -3.306}],
               "amounts": [[1000, -1.944], [4000, -3.306]]}
        (event,), used = se.apply_ear([], "よっしゃんてん", ear)
        assert used == "四千点" and "phonetic_amount" in event.parse_flags
        assert [a for a, _ in event.amount_scores] == [4000, 1000] and event.amount_scores[0][1] < -3.306


class TestRecord:
    def test_scores_round_trip_and_match_the_schema(self):
        jsonschema = pytest.importorskip("jsonschema")
        schema = json.loads((Path(__file__).resolve().parents[1]
                             / "docs/contracts/schemas/reconstruction_event.schema.json").read_text(encoding="utf-8"))
        event = AudioEvent(action="raise", amount=1300, timestamp=5.0, raw_text="レイズ 300",
                           parse_flags=("legal_amount",), amount_scores=((1300, -0.5), (300, -0.9)))
        envelope = event_to_envelope(event)
        jsonschema.validate(envelope, schema)
        back = event_from_envelope(json.loads(json.dumps(envelope)))
        assert back.amount_scores == ((1300, -0.5), (300, -0.9)) and back.parse_flags == ("legal_amount",)
        assert "amount_scores" not in event_to_envelope(AudioEvent(action="call", amount=0, timestamp=1.0,
                                                                   raw_text="コール"))


# ――― engine ―――

pokerkit = pytest.importorskip("pokerkit")

from tests.test_rfid_folds import _Table  # noqa: E402
from tests.test_store_2026_09_27 import STORE_HOLES  # noqa: E402
from tests.test_store_2026_09_29 import _acts, _replayed_open  # noqa: E402
from tests.test_store_2026_10_01 import _rebuilt  # noqa: E402


def _table(tmp_path) -> _Table:
    """ボタン 席6（最初の手番）/ SB 席4 / BB 席5。持ち点はどの席も 10000、ブラインド 100/200（ポット 300）。"""
    tb = _Table(tmp_path)
    tb.deal(STORE_HOLES)
    return tb


def _feed(tb: _Table, event: AudioEvent) -> None:
    event.timestamp = tb.now
    if event.utterance_start_ts is None:
        event.utterance_start_ts = tb.now
    tb.recorder.record(event)
    tb.t._handle_audio_event(event)    # noqa: SLF001


def _raise(amount: int, scores=(), flags=()) -> AudioEvent:
    return AudioEvent(action="raise", amount=amount, timestamp=0.0, raw_text=f"レイズ {amount}",
                      parse_flags=tuple(flags), amount_scores=tuple(scores), confidence=0.6)


SCORES_300 = ((300, -0.5), (1300, -1.0), (3000, -1.5))


class TestUnusableAmount:
    def test_the_nearest_usable_amount(self, tmp_path):
        """「レイズ 300」は最小レイズ 400 に届かない → 第 2 の耳の表の使える額から: 1300（音 −1.0・2.2 倍で −1.2）が
        3000（音 −1.5・5.6 倍で −7.2）に勝つ。"""
        tb = _table(tmp_path)
        _feed(tb, _raise(300, SCORES_300))
        assert _acts(tb) == [(6, "raise", 1300)]
        record = tb.t._current_actions[0]     # noqa: SLF001
        assert record.needs_review and "legal_amount" in record.reason.split("+")
        assert any("300 はいま使えない額" in n and "1300" in n for n in tb.notices)

    def test_without_the_table_nothing_changes(self, tmp_path):
        tb = _table(tmp_path)
        _feed(tb, _raise(300))
        assert "legal_amount" not in (tb.t._current_actions[0].reason or "")    # noqa: SLF001

    def test_a_usable_amount_is_kept(self, tmp_path):
        tb = _table(tmp_path)
        _feed(tb, _raise(1300, ((600, -0.1), (1300, -0.4))))
        assert _acts(tb) == [(6, "raise", 1300)]
        assert "legal_amount" not in (tb.t._current_actions[0].reason or "")    # noqa: SLF001

    def test_too_far_from_what_was_heard(self, tmp_path):
        """使える額の音がいちばん確からしい額から 5 より遠ければ選ばない（従来どおり）。"""
        tb = _table(tmp_path)
        _feed(tb, _raise(300, ((300, -0.5), (1300, -9.0))))
        assert "legal_amount" not in (tb.t._current_actions[0].reason or "")    # noqa: SLF001

    def test_a_bare_number_is_chosen_again(self, tmp_path):
        """店の言い方（額だけ）: いまのベット 200 より大きく最小レイズ 400 に届かない「300」は、これまで最小レイズに
        寄せていた → 第 2 の耳の表から使える額を選ぶ。"""
        tb = _table(tmp_path)
        _feed(tb, AudioEvent(action="bet", amount=300, timestamp=0.0, raw_text="300",
                             parse_flags=("amount_only",), amount_scores=SCORES_300))
        assert _acts(tb) == [(6, "raise", 1300)]
        assert "legal_amount" in tb.t._current_actions[0].reason.split("+")    # noqa: SLF001

    def test_a_bare_number_at_the_current_bet_is_still_dropped(self, tmp_path):
        """いまのベット以下の数字（コールの額の言い直し・ポットの読み上げ・役の「7」など）は従来どおり記録しない。"""
        tb = _table(tmp_path)
        _feed(tb, AudioEvent(action="bet", amount=200, timestamp=0.0, raw_text="200",
                             parse_flags=("amount_only",), amount_scores=((200, -0.5), (1200, -1.0))))
        assert _acts(tb) == [] and any("数字だけの「200」" in n for n in tb.notices)

    def test_replay_and_rebuild(self, tmp_path):
        tb = _table(tmp_path)
        _feed(tb, _raise(300, SCORES_300))
        tb.tick(tb.now + 1.0)
        tb.say("コール")
        live = _acts(tb)
        assert live == [(6, "raise", 1300), (4, "call", 1200)]
        (hand,) = _replayed_open(tb, tmp_path)
        assert [(a.seat, a.action, a.amount) for a in hand.actions][:2] == live
        assert _rebuilt(tb) == live


class TestPotWeight:
    def test_a_close_call_goes_to_the_reasonable_size(self, tmp_path):
        """音は 1300（−1.5）のほうが近いが、ポット 300 への 1300 は 2.2 倍（−1.2）、600 は 0.8 倍（0）→ 600。差は
        0.7 で「額があいまい」。"""
        tb = _table(tmp_path)
        _feed(tb, phonetic_amount_event("テスト", (1300, 600), 0.4, tb.now, scores=((1300, -1.5), (600, -2.0))))
        assert _acts(tb) == [(6, "raise", 600)]
        reasons = tb.t._current_actions[0].reason.split("+")     # noqa: SLF001
        assert "phonetic_amount" in reasons and "ambiguous_amount" in reasons

    def test_a_clear_sound_wins(self, tmp_path):
        tb = _table(tmp_path)
        _feed(tb, phonetic_amount_event("テスト", (1300, 600), 0.4, tb.now, scores=((1300, -0.5), (600, -4.0))))
        assert _acts(tb) == [(6, "raise", 1300)]

    def test_nothing_usable(self, tmp_path):
        tb = _table(tmp_path)
        _feed(tb, phonetic_amount_event("テスト", (50000,), 0.4, tb.now, scores=((50000, -0.5),)))
        assert _acts(tb) == [] and any("使えない額" in n for n in tb.notices)


# ――― 推定器 ―――

from integration.estimator import PARAMS  # noqa: E402
from tests.test_estimator import T0, _estimator, _iso, _window, _word  # noqa: E402


def _hand(actions: list[dict]) -> dict:
    return {"players": [{"seat": s} for s in (4, 5, 6)], "actions": actions, "winner_seat": 6,
            "winner_source": "fold", "blinds": {"sb": 100, "bb": 200},
            "position_map": {"4": "SB", "5": "BB", "6": "BTN"}}


def _action(seat: int, action: str, amount: int, t: float, reason: str = "") -> dict:
    return {"street": "preflop", "seat": seat, "action": action, "amount": amount, "timestamp": _iso(t),
            "actor_source": "engine_prior", "reason": reason}


class TestEstimator:
    def test_an_amount_the_engine_chose_is_reviewed(self):
        """推定を記録の本体にしても、engine が額を決めた印（使える額に直した・意味のない語の額・言い直し）は要確認
        （作業計画の監査 §4）。"""
        word = _word("raise", T0 + 10, T0 + 11, "レイズ 300", amount=300)
        info = {"tokens": [word], "inserted": {}, "button": 6}
        hand = _hand([_action(6, "raise", 1300, T0 + 11, "legal_amount"), _action(4, "fold", 0, T0 + 12),
                      _action(5, "fold", 0, T0 + 13)])
        _, _, flags = _estimator().score(_window(), hand, [], info, ())
        assert "額の読み（preflop 席6 1300）" in flags

    def test_a_big_bet_for_the_pot_scores_lower(self):
        """ポット 300 への 1300（2.2 倍）は −1.2、600（0.8 倍）は 0。"""
        def size_terms(amount: int) -> list[float]:
            word = _word("raise", T0 + 10, T0 + 11, f"レイズ {amount}", amount=amount)
            info = {"tokens": [word], "inserted": {}, "button": 6}
            hand = _hand([_action(6, "raise", amount, T0 + 11), _action(4, "fold", 0, T0 + 12),
                          _action(5, "fold", 0, T0 + 13)])
            _, terms, _ = _estimator().score(_window(), hand, [], info, ())
            return [v for name, v in terms if name == "賭けの大きさ"]

        assert size_terms(1300) == [pytest.approx(PARAMS["amount_prior_weight"] * -1.2)]
        assert size_terms(600) == []
        assert math.isclose(size_prior(2.2), -1.2)
