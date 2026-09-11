"""tests/test_main_audio_optional.py

`config.audio.enabled` でマイク入力（AudioThread）を切れること（B5: マイク無しの実機テスト）。

狙い: 実プレイ環境で **RFID のカード読み取りだけ**を先に確認したい / マイクが繋がっていない
とき、AudioThread を起動せずに hand logger を走らせられる。アクションは `--cli` の入力ループが
読み上げ文を `parse_action` に通して AudioEvent にするので、音声と同じ語彙・同じ経路で進行する。
"""
from __future__ import annotations

import threading

import main
from audio.recognizer import parse_action
from core.event_queue import make_audio_queue


class TestMakeAudioThread:
    def test_enabled_false_returns_none(self):
        cfg = {"audio": {"enabled": False, "device_id": 0}}
        assert main._make_audio_thread(cfg, make_audio_queue(), threading.Event()) is None

    def test_default_is_enabled(self):
        """既定（キー無し）は従来どおり AudioThread を作る = 挙動不変。"""
        thread = main._make_audio_thread({"audio": {}}, make_audio_queue(), threading.Event())
        assert thread is not None
        assert not thread.is_alive()      # 生成のみ（start はしない）

    def test_missing_audio_section_is_enabled(self):
        assert main._make_audio_thread({}, make_audio_queue(), threading.Event()) is not None

    def test_enabled_true_passes_config_through(self):
        cfg = {"audio": {"enabled": True, "device_id": 3, "sample_rate": 8000,
                         "whisper_model": "tiny", "language": "en"}}
        thread = main._make_audio_thread(cfg, make_audio_queue(), threading.Event())
        assert thread is not None
        assert thread._device_id == 3          # noqa: SLF001
        assert thread._sample_rate == 8000     # noqa: SLF001


class TestDummyActionVocabulary:
    """CLI の「読み上げ文をそのまま打つ」経路は音声と同じ `parse_action` を通る。

    ここでは CLI が受け付ける想定の文が AudioEvent になることを固定する（語彙の二重管理を
    防ぐため、CLI 側に独自パーサを持たせない設計の回帰テスト）。
    """

    def test_street_actions_parse(self):
        for text, action in [
            ("チェック", "check"),
            ("コール", "call"),
            ("フォールド", "fold"),
            ("オールイン", "allin"),
        ]:
            ev = parse_action(text)
            assert ev is not None and ev.action == action, text

    def test_bet_with_amount_parses(self):
        ev = parse_action("ベット 500")
        assert ev is not None and ev.action == "bet" and ev.amount == 500

    def test_seat_prefix_is_carried(self):
        ev = parse_action("シート3 コール")
        assert ev is not None and ev.action == "call" and ev.seat == 3

    def test_unparsable_returns_none(self):
        """認識できない行は None → CLI は使い方を表示して queue には積まない。"""
        assert parse_action("こんばんは") is None
