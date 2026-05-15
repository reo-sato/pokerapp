"""tests/test_speech_normalizer.py"""
from __future__ import annotations

from audio.speech_normalizer import normalize_speech


class TestNormalizeSpeech:
    def test_amount_only_digits(self) -> None:
        n = normalize_speech("600")
        assert n.action is None
        assert n.amount == 600
        assert n.seat is None

    def test_amount_only_kanji(self) -> None:
        n = normalize_speech("千二百")
        assert n.action is None
        assert n.amount == 1200

    def test_action_and_amount(self) -> None:
        n = normalize_speech("ベット 600")
        assert n.action == "BET"
        assert n.amount == 600

    def test_call_only(self) -> None:
        n = normalize_speech("コール")
        assert n.action == "CALL"
        assert n.amount is None

    def test_seat_reference_extracted(self) -> None:
        n = normalize_speech("シート3 レイズ 800")
        assert n.action == "RAISE"
        assert n.amount == 800
        assert n.seat == 3

    def test_seat_kanji(self) -> None:
        n = normalize_speech("シート三 コール")
        assert n.action == "CALL"
        assert n.seat == 3

    def test_seat_does_not_pollute_amount(self) -> None:
        n = normalize_speech("シート1 レイズ 800")
        assert n.amount == 800

    def test_full_width_digits(self) -> None:
        n = normalize_speech("６００")
        assert n.amount == 600

    def test_empty(self) -> None:
        n = normalize_speech("")
        assert n.action is None
        assert n.amount is None
        assert n.seat is None

    def test_matched_rules_populated(self) -> None:
        n = normalize_speech("シート3 ベット 500")
        # action / seat / amount すべてマッチ
        assert any("action_keyword" in r for r in n.matched_rules)
        assert any(r.startswith("seat:") for r in n.matched_rules)
        assert any(r.startswith("amount:") for r in n.matched_rules)
