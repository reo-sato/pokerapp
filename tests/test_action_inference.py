"""tests/test_action_inference.py

state-aware な action 推定 / 検証ロジックのテスト。
"""
from __future__ import annotations

from audio.speech_normalizer import normalize_speech
from integration.action_inference import infer_action_from_state
from integration.betting_state import BettingState


def _setup_preflop(button: int = 1, active=(1, 2, 3, 4, 5, 6), sb=100, bb=200) -> BettingState:
    bs = BettingState()
    bs.start_hand(button_seat=button, active_seats=list(active),
                  sb_amount=sb, bb_amount=bb)
    return bs


def _setup_flop(button: int = 1, active=(1, 2, 3, 4, 5, 6)) -> BettingState:
    bs = _setup_preflop(button=button, active=active)
    bs.reset_for_new_street("flop")
    return bs


# ---------- 金額のみ発話 ----------

class TestAmountOnly:
    def test_opening_bet_on_empty_flop(self) -> None:
        bs = _setup_flop()  # current_bet=0, is_opened=False, actor=2
        r = infer_action_from_state(normalize_speech("600"), bs)
        assert r.action == "BET"
        assert r.amount == 600
        assert r.seat == 2
        assert r.needs_review is False
        assert "opening" in r.reason

    def test_call_when_amount_matches_current_bet(self) -> None:
        bs = _setup_preflop()  # current_bet=200 (BB), actor=4, contrib[4]=0
        r = infer_action_from_state(normalize_speech("200"), bs)
        assert r.action == "CALL"
        assert r.amount == 200
        assert r.needs_review is False

    def test_raise_when_amount_above_call_target(self) -> None:
        bs = _setup_preflop()
        r = infer_action_from_state(normalize_speech("600"), bs)
        assert r.action == "RAISE"
        assert r.amount == 600
        assert r.needs_review is False

    def test_below_call_amount_is_review(self) -> None:
        bs = _setup_flop()
        bs.apply_action(2, "BET", 600)
        # actor は 3、 contrib[3]=0、current_bet=600。amount=300 → 不正
        r = infer_action_from_state(normalize_speech("300"), bs)
        assert r.needs_review is True
        assert r.reason == "amount_only_below_call"

    def test_raise_when_already_matched_but_higher(self) -> None:
        # BB が preflop で raise する: contrib=200, current_bet=200, amount=600 → RAISE
        bs = _setup_preflop()
        # 強引に actor を BB に
        bs.actor_seat = bs.bb_seat
        r = infer_action_from_state(normalize_speech("600"), bs)
        assert r.action == "RAISE"
        assert r.amount == 600


# ---------- アクション+金額 ----------

class TestActionWithAmount:
    def test_bet_validated_when_not_opened(self) -> None:
        bs = _setup_flop()
        r = infer_action_from_state(normalize_speech("ベット 600"), bs)
        assert r.action == "BET"
        assert r.amount == 600
        assert r.needs_review is False

    def test_bet_when_already_opened_review(self) -> None:
        bs = _setup_flop()
        bs.apply_action(2, "BET", 400)
        # 次の seat が 'ベット 800' と言ったら本来 RAISE
        r = infer_action_from_state(normalize_speech("ベット 800"), bs)
        assert r.needs_review is True
        assert "raise" in r.reason.lower()

    def test_raise_below_current_bet_review(self) -> None:
        bs = _setup_preflop()
        r = infer_action_from_state(normalize_speech("レイズ 100"), bs)
        assert r.needs_review is True
        assert r.reason == "raise_amount_not_above_current_bet"

    def test_call_without_facing_bet_review(self) -> None:
        bs = _setup_flop()
        r = infer_action_from_state(normalize_speech("コール 300"), bs)
        assert r.needs_review is True

    def test_check_while_facing_bet_review(self) -> None:
        bs = _setup_preflop()  # actor=4, current_bet=200, contrib[4]=0
        r = infer_action_from_state(normalize_speech("チェック"), bs)
        assert r.needs_review is True


# ---------- アクションのみ ----------

class TestActionOnly:
    def test_call_action_only_uses_current_bet(self) -> None:
        bs = _setup_preflop()
        r = infer_action_from_state(normalize_speech("コール"), bs)
        assert r.action == "CALL"
        assert r.amount == 200

    def test_check_preflop_from_non_bb_review(self) -> None:
        bs = _setup_preflop()
        # contrib[4]=0, current_bet=200 → CHECK は facing bet エラー (より先に検出)
        r = infer_action_from_state(normalize_speech("チェック"), bs)
        assert r.needs_review is True

    def test_check_preflop_from_bb_ok(self) -> None:
        bs = _setup_preflop()
        # BB が limp 後の CHECK option (限定的テスト: facing bet がないとき)
        bs.actor_seat = bs.bb_seat  # seat 3
        # BB は contrib=200, current_bet=200 なので facing bet なし → CHECK OK
        r = infer_action_from_state(normalize_speech("チェック"), bs)
        assert r.action == "CHECK"
        assert r.needs_review is False

    def test_fold_always_valid(self) -> None:
        bs = _setup_preflop()
        r = infer_action_from_state(normalize_speech("フォールド"), bs)
        assert r.action == "FOLD"
        assert r.needs_review is False


# ---------- 席言及との整合性 ----------

class TestSeatMismatch:
    def test_speech_seat_matches_actor_is_ok(self) -> None:
        bs = _setup_preflop()  # actor=4
        r = infer_action_from_state(normalize_speech("シート4 コール"), bs)
        assert r.needs_review is False
        assert r.seat == 4

    def test_speech_seat_different_from_actor_review(self) -> None:
        bs = _setup_preflop()  # actor=4
        r = infer_action_from_state(normalize_speech("シート5 コール"), bs)
        assert r.needs_review is True
        assert "seat_mismatch" in r.reason


# ---------- legacy fallback (state 未初期化) ----------

class TestLegacyFallback:
    def test_uninitialized_state_allows_action(self) -> None:
        bs = BettingState()  # is_initialized=False
        r = infer_action_from_state(normalize_speech("レイズ 800"), bs)
        # legacy fallback: action そのまま、review しない
        assert r.action == "RAISE"
        assert r.amount == 800
        assert r.needs_review is False
