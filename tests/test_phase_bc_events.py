"""tests/test_phase_bc_events.py

Phase B+C (イベント記録基盤) の回帰テスト。

B: AudioEvent への seat / confidence の additive 追加と、その populate 経路
   （parse_action の confidence passthrough + 明示席抽出、
    WhisperTranscriber.transcribe_with_confidence）。
"""
from __future__ import annotations

from audio.recognizer import WhisperTranscriber, parse_action


class TestParseActionAdditive:
    def test_confidence_passthrough(self) -> None:
        ev = parse_action("シート3 レイズ 800", confidence=0.77)
        assert ev is not None
        assert ev.confidence == 0.77

    def test_confidence_defaults_none(self) -> None:
        ev = parse_action("シート2 コール")
        assert ev is not None
        assert ev.confidence is None

    def test_explicit_seat_extracted(self) -> None:
        ev = parse_action("シート3 レイズ 800")
        assert ev is not None
        assert ev.seat == 3

    def test_explicit_seat_english(self) -> None:
        ev = parse_action("seat 5 raise 1200")
        assert ev is not None
        assert ev.seat == 5

    def test_explicit_seat_fullwidth(self) -> None:
        ev = parse_action("シート４ ベット 500")
        assert ev is not None
        assert ev.seat == 4

    def test_no_seat_reference_is_none(self) -> None:
        ev = parse_action("レイズ 800")
        assert ev is not None
        assert ev.seat is None

    def test_seat_out_of_range_is_none(self) -> None:
        # 範囲外(>9)は actor として無効 → None
        ev = parse_action("シート12 レイズ 800")
        assert ev is not None
        assert ev.seat is None

    def test_amount_still_excludes_seat_number(self) -> None:
        # seat 追加後も席番号が amount に混入しない（既存挙動の回帰）
        ev = parse_action("シート1 レイズ 800")
        assert ev is not None
        assert ev.action == "raise"
        assert ev.amount == 800
        assert ev.seat == 1


class TestTranscribeWithConfidence:
    def test_returns_empty_and_none_without_model(self) -> None:
        # faster-whisper 未導入環境ではモデルが None → ("", None) を返す（クラッシュしない）。
        t = WhisperTranscriber(model_size="tiny", language="ja")
        assert t.transcribe_with_confidence(b"\x00\x00") == ("", None)

    def test_transcribe_delegates_to_text(self) -> None:
        t = WhisperTranscriber(model_size="tiny", language="ja")
        assert t.transcribe(b"\x00\x00") == ""
