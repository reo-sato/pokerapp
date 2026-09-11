"""tests/test_phase_d_corrections.py

Phase D (#7) part 1 — apply_corrections の単体テスト（ADR-0009 §5 の修復表）。

apply_corrections は純関数で pokerkit を必要としない（LegalContext を直接構築して検証）。
ライブ経路への結線（actor 推定 = D2）はまだ行わないため挙動不変。
"""
from __future__ import annotations

from audio.recognizer import apply_corrections
from core.poker_engine import LegalContext


def _no_bet() -> LegalContext:
    # 当ストリート未ベット: check / bet が合法、call 不要。
    return LegalContext(
        actor_seat=1,
        legal_actions=frozenset({"check", "bet", "allin"}),
        amount_to_call=0,
        min_raise=200,
        max_raise=10000,
    )


def _facing_bet() -> LegalContext:
    # ベットに直面: fold / call / raise が合法。call 額 300、min-raise to 600。
    return LegalContext(
        actor_seat=1,
        legal_actions=frozenset({"fold", "call", "raise", "allin"}),
        amount_to_call=300,
        min_raise=600,
        max_raise=10000,
    )


def _call_only() -> LegalContext:
    # raise 非合法（call/fold のみ）。
    return LegalContext(
        actor_seat=1,
        legal_actions=frozenset({"fold", "call"}),
        amount_to_call=300,
        min_raise=0,
        max_raise=0,
    )


class TestCheckCall:
    def test_check_when_no_bet(self):
        r = apply_corrections("check", 0, _no_bet())
        assert (r.action, r.amount, r.needs_review) == ("check", 0, False)
        assert r.corrected_from is None

    def test_call_heard_but_check(self):
        # call 不要なのに "call" → check に一意化し flag。
        r = apply_corrections("call", 0, _no_bet())
        assert r.action == "check"
        assert r.needs_review is True
        assert r.corrected_from == "call"

    def test_call_uses_state_amount(self):
        # heard 額(999)は無視し engine の call 額(300)を採用。
        r = apply_corrections("call", 999, _facing_bet())
        assert (r.action, r.amount, r.needs_review) == ("call", 300, False)

    def test_check_facing_bet_remaps_to_call(self):
        r = apply_corrections("check", 0, _facing_bet())
        assert r.action == "call"
        assert r.amount == 300
        assert r.needs_review is True
        assert r.corrected_from == "check"
        assert r.reason == "check_facing_bet"

    def test_check_illegal_without_call_folds(self):
        ctx = LegalContext(1, frozenset({"fold"}), 300, 0, 0)
        r = apply_corrections("check", 0, ctx)
        assert r.action == "fold"
        assert r.needs_review is True
        assert r.corrected_from == "check"


class TestBetRaise:
    def test_bet_when_no_bet(self):
        r = apply_corrections("bet", 500, _no_bet())
        assert (r.action, r.amount, r.needs_review) == ("bet", 500, False)
        assert r.corrected_from is None

    def test_bet_remapped_to_raise_when_bet_exists(self):
        r = apply_corrections("bet", 800, _facing_bet())
        assert r.action == "raise"
        assert r.amount == 800
        assert r.corrected_from == "bet"
        assert r.reason == "bet_to_raise"
        assert r.needs_review is False

    def test_raise_legal_in_range(self):
        r = apply_corrections("raise", 800, _facing_bet())
        assert (r.action, r.amount, r.needs_review) == ("raise", 800, False)
        assert r.corrected_from is None

    def test_raise_illegal_to_call(self):
        r = apply_corrections("raise", 800, _call_only())
        assert r.action == "call"
        assert r.amount == 300
        assert r.needs_review is True
        assert r.corrected_from == "raise"

    def test_amount_below_min_snaps_up(self):
        # heard 50 < min-raise 600 → 600 に clamp。gap(550) <= m(600) なので review なし。
        r = apply_corrections("raise", 50, _facing_bet())
        assert r.action == "raise"
        assert r.amount == 600

    def test_amount_far_over_max_flags(self):
        r = apply_corrections("raise", 20000, _facing_bet())
        assert r.amount == 10000
        assert r.needs_review is True
        assert r.reason == "amount_snapped"

    def test_no_amount_heard_flags(self):
        r = apply_corrections("raise", 0, _facing_bet())
        assert r.amount == 600           # min へ
        assert r.needs_review is True
        assert r.reason == "no_amount_heard"


class TestAllinFoldMisc:
    def test_allin_uses_max_raise(self):
        r = apply_corrections("allin", 0, _facing_bet())
        assert (r.action, r.amount, r.needs_review) == ("allin", 10000, False)

    def test_allin_call_all_in_when_no_raise(self):
        r = apply_corrections("allin", 0, _call_only())
        assert r.action == "allin"
        assert r.amount == 300            # s==0 → call 額

    def test_fold_passthrough(self):
        r = apply_corrections("fold", 0, _facing_bet())
        assert (r.action, r.amount, r.needs_review) == ("fold", 0, False)

    def test_empty_legal_context_flags(self):
        ctx = LegalContext(None, frozenset(), 0, 0, 0)
        r = apply_corrections("bet", 500, ctx)
        assert r.action == "bet"
        assert r.needs_review is True
        assert r.reason == "no_legal_context"

    def test_non_betting_action_passthrough(self):
        r = apply_corrections("winner", 0, _facing_bet())
        assert (r.action, r.amount, r.needs_review) == ("winner", 0, False)

    def test_asr_confidence_threaded(self):
        r = apply_corrections("fold", 0, _facing_bet(), whisper_conf=0.91)
        assert r.asr_confidence == 0.91
