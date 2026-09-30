"""tests/test_corpus_0930_readings.py

店舗の読み上げ集（2026-09-30, 2 人 × 163 句, 音楽を流した店内）で読めていなかった書き起こし。
"""
from __future__ import annotations

import pytest

from audio.recognizer import is_announcement, parse_actions
from core.positions import parse_position
from tools.audio_check import RDP_AUDIO_HINT, InputDevice, WDM_KS_OPEN_HINT, only_wdm_ks, open_error_hint


def _read(text: str) -> list[tuple]:
    return [(e.action, e.amount, e.position, e.hand_name) for e in parse_actions(text)]


class TestWords:
    def test_check_raise_with_a_long_vowel(self):
        """「チェックレーズ 2千」を「チェック」+ 額と読んでいた。"""
        assert _read("チェックレーズ 2千") == [("raise", 2000, None, None)]

    @pytest.mark.parametrize("text, name", [("プルハウス", "Full house"), ("二ペア", "Two pair"),
                                            ("三カード", "Three of a kind"), ("四カード", "Four of a kind")])
    def test_hand_names(self, text, name):
        assert _read(text) == [("end_hand", 0, None, name)]

    @pytest.mark.parametrize("text, position", [
        ("ビックブラインド レイズ1200", "BB"), ("カット オフ コール", "CO"), ("ATG フォールド", "UTG"),
        ("UDG フォールド", "UTG"),
    ])
    def test_position_spellings(self, text, position):
        assert parse_actions(text)[0].position == position

    def test_ascii_position_spellings_need_a_boundary(self):
        assert parse_position("ATGC") is None and parse_position("budget") is None


class TestAnnouncements:
    @pytest.mark.parametrize("text", ["ブラインド200、オールイン400です。", "ブラインド 200 400 です", "ブラインド二百、四百"])
    def test_a_blind_announcement_is_not_an_action(self, text):
        assert parse_actions(text) == [] and is_announcement(text)

    @pytest.mark.parametrize("text", ["ポット1万2000です。", "ポット 3000"])
    def test_a_pot_announcement(self, text):
        assert parse_actions(text) == [] and is_announcement(text)

    @pytest.mark.parametrize("text", ["ビッグブラインド レイズ 1200", "スモールブラインド コール", "レイズ 600", "ご視聴ありがとうございました。"])
    def test_not_an_announcement(self, text):
        assert not is_announcement(text)


class TestRdpHint:
    """RDP の音声が接続元に回っていると、マイクは WDM-KS 方式にしか出ず、Bluetooth のマイクを開けない
    （店舗 2026-09-30: [Errno -9999] Unanticipated host error）。"""

    def test_only_wdm_ks(self):
        wdm = [InputDevice(11, "Headset (…DJI Mic Mini 2-ED8C97)", "Windows WDM-KS", True, False),
               InputDevice(6, "Microphone (Realtek HD Audio Mic input)", "Windows WDM-KS", False, False)]
        assert only_wdm_ks(wdm) and only_wdm_ks([{"api": "Windows WDM-KS"}])
        assert not only_wdm_ks(wdm + [InputDevice(1, "Headset (DJI Mic Mini 2-EBA00C", "MME", True, True)])
        assert not only_wdm_ks([])
        assert "リモート PC で再生" in RDP_AUDIO_HINT

    def test_open_error_hint(self):
        assert open_error_hint({"api": "Windows WDM-KS"}) == WDM_KS_OPEN_HINT
        assert open_error_hint({"api": "MME"}) == ""
