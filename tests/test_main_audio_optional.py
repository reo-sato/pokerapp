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

    def test_hiragana_is_normalized_to_katakana(self):
        """ISSUE-0027: IME 変換しないまま打った「ちぇっく」も拾う（ASR の書き起こし揺れも同様）。"""
        for text, action in [
            ("ちぇっく", "check"),
            ("こーる", "call"),
            ("ふぉーるど", "fold"),
            ("れいず", "raise"),
            ("おーるいん", "allin"),
            ("うぃなー", "winner"),
            ("はんど開始", "new_hand"),
        ]:
            ev = parse_action(text)
            assert ev is not None and ev.action == action, text

    def test_hiragana_keeps_amount_and_seat(self):
        ev = parse_action("べっと 500")
        assert ev is not None and ev.action == "bet" and ev.amount == 500
        ev = parse_action("しーと3 こーる")
        assert ev is not None and ev.action == "call" and ev.seat == 3
        # 席番号は金額として拾わない（正規化しても除去が効くこと）
        ev = parse_action("しーと1 れいず 800")
        assert ev is not None and ev.action == "raise" and ev.seat == 1 and ev.amount == 800

    def test_raw_text_keeps_original_form(self):
        """`raw_text` は正規化前のまま（ログ・監査で実際の入力が分かるように）。"""
        ev = parse_action("ちぇっく")
        assert ev is not None and ev.raw_text == "ちぇっく"

    def test_unparsable_returns_none(self):
        """認識できない行は None → CLI は使い方を表示して queue には積まない。"""
        assert parse_action("こんばんは") is None


class TestCliCommandNormalization:
    """ISSUE-0030: 日本語 IME のまま打った全角コマンド（`ｎ` / `ｑ` / `ｗ　１`）も通す。"""

    def test_fullwidth_letters_become_halfwidth(self):
        assert main._normalize_cli_command("ｎ") == "n"          # noqa: SLF001
        assert main._normalize_cli_command("ｑ") == "q"          # noqa: SLF001
        assert main._normalize_cli_command("Ｎ").lower() == "n"  # noqa: SLF001

    def test_fullwidth_space_and_digits(self):
        assert main._normalize_cli_command("ｗ　１") == "w 1"      # noqa: SLF001
        assert main._normalize_cli_command("ｒ　１　５００") == "r 1 500"  # noqa: SLF001

    def test_halfwidth_is_unchanged(self):
        assert main._normalize_cli_command("w 1") == "w 1"       # noqa: SLF001

    def test_japanese_text_is_untouched(self):
        """読み上げ文はこの写像の対象外（カナ・漢字は範囲外なので素通り）。"""
        assert main._normalize_cli_command("シート3 コール") == "シート3 コール"  # noqa: SLF001

    def test_glued_command_is_split(self):
        """ISSUE-0034: ログが入力行に割り込む環境で空白を打ち損ねても通す（w/r のみ）。"""
        assert main._normalize_cli_command("w1") == "w 1"          # noqa: SLF001
        assert main._normalize_cli_command("ｗ１") == "w 1"         # noqa: SLF001
        assert main._normalize_cli_command("r1 500") == "r 1 500"  # noqa: SLF001

    def test_glued_split_does_not_touch_other_input(self):
        assert main._normalize_cli_command("q") == "q"             # noqa: SLF001
        assert main._normalize_cli_command("n") == "n"             # noqa: SLF001
        assert main._normalize_cli_command("w 1") == "w 1"         # noqa: SLF001
        # 読み上げ文は素通り（コマンド letter + 数字の形ではない）
        assert main._normalize_cli_command("ベット500") == "ベット500"   # noqa: SLF001


class TestCliAudioStatus:
    """ADR-0060: ログはファイルに行くので、音声の準備状況と聞き取った文は CLI に直接出す。"""

    def _report(self, capsys, health: dict) -> str:
        from types import SimpleNamespace

        main._report_audio_start(SimpleNamespace(health=health), 1, wait_sec=0)  # noqa: SLF001
        return capsys.readouterr().out

    def test_running_names_the_mic(self, capsys):
        out = self._report(capsys, {"state": "running", "device_name": "マイク (USB Audio)"})
        assert "番号 1（マイク (USB Audio)）で聞き取っています" in out

    def test_open_failure_points_at_audio_check(self, capsys):
        out = self._report(capsys, {"state": "error", "error": "Invalid sample rate"})
        assert "開けませんでした" in out and "Invalid sample rate" in out
        assert r"tools\audio_check.py list" in out

    def test_missing_pyaudio(self, capsys):
        assert "PyAudio が無い" in self._report(capsys, {"state": "unavailable"})

    def test_heard_line(self, capsys):
        from audio.recorder import Transcript

        for text, expected in [("シート3 レイズ 600", "→ raise 600 席3"), ("えーと", "→ アクションとして読めず")]:
            ev = parse_action(text)
            main._print_transcript(Transcript(  # noqa: SLF001
                text=text, confidence=0.8, events=(ev,) if ev else (), audio_sec=1.0,
                infer_sec=0.5, utterance_start_ts=None, heard_at=0.0,
            ))
            assert f"[聞き取り] 「{text}」{expected}" in capsys.readouterr().out

    def test_the_hook_reaches_the_thread(self):
        def hook(t):
            return None

        thread = main._make_audio_thread({}, make_audio_queue(), threading.Event(), on_transcript=hook)
        assert thread._on_transcript is hook  # noqa: SLF001


def test_cli_keeps_going_when_the_model_cannot_load(tmp_path, monkeypatch, capsys):
    """初回のモデル取得に失敗しても（ここでは faster-whisper 無し）落ちずにキーボードで進行できる。"""
    import sys

    monkeypatch.setitem(sys.modules, "faster_whisper", None)   # import すると ImportError
    cfg = {"audio": {"enabled": True}, "rfid": {"enabled": False}, "engine": {"backend": "legacy"},
           "table_state": {"enabled": False}}
    answers = iter(["2", "", "1000", "", "1000", "5", "10", "", str(tmp_path / "logs"), "q"])
    monkeypatch.setattr("core.config.load_config", lambda path=None: cfg)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    main.run_cli()
    out = capsys.readouterr().out
    assert "音声認識モデル（medium）を読み込んでいます" in out
    assert "音声認識モデルを読み込めませんでした（faster-whisper が入っていません）" in out
    assert "セッション終了" in out
