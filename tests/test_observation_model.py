"""tests/test_observation_model.py

observation_model.py の単体テスト (M2)。
- Normal / Dirichlet の数値性
- legal_actions による NEG_INF
- 金額整合 (CALL の degenerate / BET の log-Normal)
- default_priors() の構築健全性
"""
from __future__ import annotations

import math
import time

import pytest

from core.events import ASRAlternative, AudioEvent, RFIDEvent, WordTiming
from integration.action_inference import BettingState
from integration.observation_model import (
    EvidenceInterval,
    NEG_INF,
    PriorParams,
    _amount_log_likelihood,
    _lexicon_log_likelihood,
    _lognormal_log_pdf,
    _normal_log_pdf,
    _position_bucket,
    _position_prior_log,
    _word_prior,
    compute_log_likelihood,
    default_amount_for,
    default_priors,
    evidence_from_audio,
    evidence_from_rfid,
    is_legal,
)


def _state(**kwargs) -> BettingState:
    s = BettingState()
    s.bb_amount = kwargs.pop("bb_amount", 200)
    s.sb_amount = kwargs.pop("sb_amount", 100)
    for k, v in kwargs.items():
        setattr(s, k, v)
    return s


def _audio(text: str, action: str = "amount_only", amount: int = 0) -> AudioEvent:
    return AudioEvent(action=action, amount=amount, timestamp=time.time(), raw_text=text)


# ── Math helpers ─────────────────────────────────────────────────────────────

class TestMath:
    def test_normal_log_pdf_peak_at_mu(self) -> None:
        # 最大値は x=mu
        a = _normal_log_pdf(0.4, mu=0.4, sigma=0.6)
        b = _normal_log_pdf(2.0, mu=0.4, sigma=0.6)
        assert a > b

    def test_normal_log_pdf_sigma_zero_returns_neg_inf(self) -> None:
        assert _normal_log_pdf(0.0, 0.0, 0.0) == NEG_INF

    def test_lognormal_log_pdf_only_positive(self) -> None:
        assert _lognormal_log_pdf(0.0, math.log(100.0), 0.7) == NEG_INF
        assert _lognormal_log_pdf(-5.0, math.log(100.0), 0.7) == NEG_INF
        assert _lognormal_log_pdf(100.0, math.log(100.0), 0.7) > -math.inf


# ── Lexicon ─────────────────────────────────────────────────────────────────

class TestLexicon:
    def test_known_word_high_for_matching_action(self) -> None:
        prior = default_priors()
        p_call_given_call = _word_prior("コール", "call", prior)
        p_call_given_fold = _word_prior("コール", "fold", prior)
        assert p_call_given_call > p_call_given_fold

    def test_unknown_word_returns_small_floor(self) -> None:
        prior = default_priors()
        # コーラ (Coke) はアクション語彙にない
        for action in ("call", "fold", "raise", "bet", "check"):
            p = _word_prior("コーラ", action, prior)
            assert 0 < p < 0.02   # alpha_unknown / total はかなり小さい

    def test_lexicon_likelihood_uses_alternatives(self) -> None:
        prior = default_priors()
        # N-best あり: 「コール」確信高
        ev = AudioEvent(
            action="amount_only", amount=200, timestamp=time.time(), raw_text="コール",
            alternatives=[
                ASRAlternative(text="コール", confidence=0.85, words=[]),
                ASRAlternative(text="フォール", confidence=0.10, words=[]),
            ],
        )
        ll_call = _lexicon_log_likelihood(ev, "call", prior)
        ll_fold = _lexicon_log_likelihood(ev, "fold", prior)
        assert ll_call > ll_fold

    def test_lexicon_likelihood_falls_back_to_raw_text(self) -> None:
        prior = default_priors()
        ev = AudioEvent(action="call", amount=200, timestamp=time.time(), raw_text="コール")
        # alternatives が空でも raw_text 経由で評価される
        ll_call = _lexicon_log_likelihood(ev, "call", prior)
        ll_fold = _lexicon_log_likelihood(ev, "fold", prior)
        assert ll_call > ll_fold


# ── Amount integrity ─────────────────────────────────────────────────────────

class TestAmount:
    def test_check_amount_must_be_zero(self) -> None:
        s = _state()
        prior = default_priors()
        assert _amount_log_likelihood(0, "check", s, None, prior) == 0.0
        assert _amount_log_likelihood(200, "check", s, None, prior) == NEG_INF

    def test_call_amount_degenerate_at_current_bet(self) -> None:
        s = _state(current_bet=200, is_opened=True)
        prior = default_priors()
        assert _amount_log_likelihood(200, "call", s, None, prior) == 0.0
        assert _amount_log_likelihood(600, "call", s, None, prior) == NEG_INF

    def test_bet_amount_lognormal(self) -> None:
        s = _state(bb_amount=200)
        prior = default_priors()
        ll_2bb = _amount_log_likelihood(400, "bet", s, None, prior)   # peak
        ll_8bb = _amount_log_likelihood(1600, "bet", s, None, prior)  # tail
        assert ll_2bb > ll_8bb
        assert _amount_log_likelihood(0, "bet", s, None, prior) == NEG_INF


# ── Hard constraints ────────────────────────────────────────────────────────

class TestLegal:
    def test_check_illegal_when_current_bet_positive(self) -> None:
        s = _state(current_bet=200, is_opened=True)
        assert not is_legal("check", 0, s, actor_seat=1)

    def test_check_legal_when_no_bet(self) -> None:
        s = _state(current_bet=0, is_opened=False)
        assert is_legal("check", 0, s, actor_seat=1)

    def test_raise_requires_opened_state(self) -> None:
        s = _state(current_bet=0, is_opened=False)
        assert not is_legal("raise", 400, s, actor_seat=1)
        s2 = _state(current_bet=200, is_opened=True)
        assert is_legal("raise", 600, s2, actor_seat=1)

    def test_folded_seat_cannot_act(self) -> None:
        s = _state(current_bet=200, is_opened=True, folded_seats=[3])
        assert not is_legal("call", 200, s, actor_seat=3)

    def test_call_requires_amount_equal_current_bet(self) -> None:
        s = _state(current_bet=200, is_opened=True)
        assert is_legal("call", 200, s, actor_seat=1)
        assert not is_legal("call", 100, s, actor_seat=1)


# ── compute_log_likelihood ──────────────────────────────────────────────────

class TestComputeLogLikelihood:
    def test_legal_violation_returns_neg_inf(self) -> None:
        s = _state(current_bet=200, is_opened=True)
        prior = default_priors()
        ev = evidence_from_audio(_audio("チェック", action="check", amount=0))
        # CHECK は current_bet=200 のとき不可
        ll = compute_log_likelihood(ev, "check", 0, s, actor_seat=1, prior=prior)
        assert ll == NEG_INF

    def test_amount_mismatch_returns_neg_inf(self) -> None:
        s = _state(current_bet=200, is_opened=True)
        prior = default_priors()
        ev = evidence_from_audio(_audio("コール 600", action="call", amount=600))
        # CALL は current_bet=200 ぴったりでないと degenerate で消える
        ll = compute_log_likelihood(ev, "call", 600, s, actor_seat=1, prior=prior)
        assert ll == NEG_INF

    def test_consistent_call_high_likelihood(self) -> None:
        s = _state(current_bet=200, is_opened=True, active_seats=[1, 2, 3, 4], button_seat=4)
        prior = default_priors()
        ev = evidence_from_audio(_audio("コール 200", action="call", amount=200))
        ll = compute_log_likelihood(ev, "call", 200, s, actor_seat=2, prior=prior)
        assert ll > -math.inf
        assert ll < 0   # 未正規化なので絶対値ではなく相対比較

    def test_rfid_fold_uses_t_end(self) -> None:
        s = _state(current_bet=200, is_opened=True, active_seats=[1, 2, 3, 4], button_seat=4)
        prior = default_priors()
        t0 = 1000.0
        rfid = RFIDEvent(
            tag_id="x", card="Ah", reader_id="seat_3", role="seat", seat=3,
            timestamp=t0, raw_tag_id="x", t_end=t0 + 0.2,
        )
        ev = evidence_from_rfid(rfid)
        ll = compute_log_likelihood(ev, "fold", 0, s, actor_seat=3, prior=prior)
        assert ll > -math.inf


# ── Position bucket ─────────────────────────────────────────────────────────

class TestPositionBucket:
    def test_button_is_late(self) -> None:
        s = _state(button_seat=4, active_seats=[1, 2, 3, 4, 5, 6])
        assert _position_bucket(4, s) == "late"

    def test_sb_is_early(self) -> None:
        s = _state(button_seat=4, active_seats=[1, 2, 3, 4, 5, 6])
        # button=4 から左隣 = seat 5 (= SB)
        assert _position_bucket(5, s) == "early"

    def test_heads_up_all_late(self) -> None:
        s = _state(button_seat=4, active_seats=[3, 4])
        for seat in s.active_seats:
            assert _position_bucket(seat, s) == "late"

    def test_position_prior_fold_dominant_early(self) -> None:
        prior = default_priors()
        s = _state(button_seat=4, active_seats=[1, 2, 3, 4, 5, 6])
        # seat 5 (SB) は early bucket → FOLD prior が最大
        p_fold = _position_prior_log("fold", 5, s, prior)
        p_raise = _position_prior_log("raise", 5, s, prior)
        assert p_fold > p_raise


# ── default_priors integrity ────────────────────────────────────────────────

class TestDefaultPriors:
    def test_contains_canonical_actions(self) -> None:
        prior = default_priors()
        for a in ("call", "raise", "fold", "check", "bet", "allin"):
            assert a in prior.lexicon
            assert prior.lexicon_total.get(a, 0) > 0

    def test_position_buckets_complete(self) -> None:
        prior = default_priors()
        for bucket in ("early", "middle", "late"):
            assert bucket in prior.position_prior
            entries = prior.position_prior[bucket]
            for action_key in ("FOLD", "CALL", "RAISE", "CHECK", "BET"):
                assert action_key in entries

    def test_default_amount_for_actions(self) -> None:
        s = _state(current_bet=200, bb_amount=200, is_opened=True)
        assert default_amount_for("check", s) == 0
        assert default_amount_for("fold", s) == 0
        assert default_amount_for("call", s) == 200
        assert default_amount_for("bet", s) > 0
        assert default_amount_for("raise", s) > 200
