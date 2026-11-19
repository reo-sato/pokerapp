"""書き起こしの API を店舗テストで使う前の確かめ（`tools/asr_check.py`, ADR-0072）。

鍵の値は画面に出さない・Windows に登録した鍵もロガーが読む・API の失敗は次にやることの分かる文にする。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core import remote_asr
from tools import asr_check

PCM = b"\x01\x00" * 16000          # 1 秒


class _Fake:
    asr_name = "openai-gpt-transcribe"

    def __init__(self, error: Exception | None = None) -> None:
        self.sent: list[bytes] = []
        self.error = error

    def _request(self, wav: bytes) -> str:
        if self.error:
            raise self.error
        self.sent.append(wav)
        return "レイズ 3200"


@pytest.fixture
def no_key(monkeypatch):
    for name in remote_asr.KEY_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(remote_asr, "read_user_env", lambda name: None)


def _save(log_dir: Path, sid: str, name: str, seconds: float = 1.0) -> Path:
    path = log_dir / "audio" / sid / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(remote_asr.wav_bytes(b"\x01\x00" * int(16000 * seconds)))
    return path


def test_the_key_registered_in_windows_is_used(no_key, monkeypatch):
    """`setx` の前から開いていた窓・タスクの子にも届くよう、環境変数に無ければ Windows の登録を読む。"""
    assert remote_asr.api_key() is None and remote_asr.key_source() is None
    monkeypatch.setattr(remote_asr, "read_user_env", lambda name: "sk-reg" if name == "POKERAPP_OPENAI_API_KEY" else None)
    assert remote_asr.api_key() == "sk-reg" and remote_asr.key_source() == "registry"
    monkeypatch.setenv("POKERAPP_OPENAI_API_KEY", "sk-env")
    assert remote_asr.api_key() == "sk-env" and remote_asr.key_source() == "env"
    import main

    assert main._wants_remote({"transcriber": "openai"})    # noqa: SLF001


def test_the_key_is_not_shown_in_errors():
    text = asr_check.explain_error(remote_asr.RemoteAsrError(
        'HTTP 401: {"error": {"message": "Incorrect API key provided: sk-proj-abcd****wxyz."}}'))
    assert "set-key" in text and "abcd" not in text and "wxyz" not in text
    assert "Billing" in asr_check.explain_error(remote_asr.RemoteAsrError("HTTP 429: quota"))
    assert "remote_model" in asr_check.explain_error(remote_asr.RemoteAsrError("HTTP 404: model not found"))
    assert "Wi-Fi" in asr_check.explain_error(remote_asr.RemoteAsrError("書き起こしの API につながりません: timed out"))


def test_check_sends_the_newest_play_utterances(tmp_path):
    _save(tmp_path, "2026-11-05_a", "100.wav")
    for name in ("200.wav", "300.wav", "400.wav", "500.wav"):
        _save(tmp_path, "2026-11-19_b", name)
    _save(tmp_path, "2026-11-19_b", "600.wav", seconds=0.2)       # 短すぎる
    _save(tmp_path, "2026-11-19_b", "off_700.wav")                 # 聞き流した発話は送らない
    assert [p.name for p in asr_check.find_samples(tmp_path)] == ["500.wav", "400.wav", "300.wav"]
    lines: list[str] = []
    fake = _Fake()
    assert asr_check.run_check({"audio": {"transcriber": "openai"}}, tmp_path, out=lines.append, transcriber=fake) == 0
    assert len(fake.sent) == 3 and any("✓ API に届きました" in x for x in lines)
    assert any("「レイズ 3200」" in x for x in lines)


def test_check_without_saved_audio_sends_silence_and_tells_to_switch(tmp_path):
    lines: list[str] = []
    fake = _Fake()
    assert asr_check.run_check({"audio": {"transcriber": "local"}}, tmp_path, out=lines.append, transcriber=fake) == 0
    assert len(fake.sent) == 1 and asr_check.wav_seconds(fake.sent[0]) == pytest.approx(1.0)
    assert any("use openai" in x for x in lines)


def test_check_fails_clearly(tmp_path, no_key):
    lines: list[str] = []
    assert asr_check.run_check({"audio": {"transcriber": "openai"}}, tmp_path, out=lines.append) == 1
    assert any("鍵（POKERAPP_OPENAI_API_KEY）: ありません" in x for x in lines)
    assert any("set-key" in x for x in lines)
    lines.clear()
    fake = _Fake(remote_asr.RemoteAsrError("HTTP 401: bad"))
    assert asr_check.run_check({"audio": {"transcriber": "openai"}}, tmp_path, out=lines.append, transcriber=fake) == 1
    assert any(x.startswith("✗") and "鍵が違う" in x for x in lines)


def test_store_key_checks_the_paste_and_writes_to_windows(monkeypatch, no_key):
    written: list = []
    monkeypatch.setattr(asr_check.os, "name", "nt")
    monkeypatch.setattr(remote_asr, "write_user_env", lambda name, value: written.append((name, value)))
    lines: list[str] = []
    assert not asr_check.store_key("", out=lines.append)
    assert not asr_check.store_key("abc def", out=lines.append)
    assert asr_check.store_key("  sk-proj-xyz  \n", out=lines.append)
    assert written == [("POKERAPP_OPENAI_API_KEY", "sk-proj-xyz")]
    assert all("sk-proj-xyz" not in x for x in lines)               # 値は表示しない
    assert remote_asr.api_key() == "sk-proj-xyz"


def test_use_switches_the_transcriber(tmp_path, capsys):
    cfg = tmp_path / "config.json"
    cfg.write_text('{"audio": {"transcriber": "local", "enabled": true}}', encoding="utf-8")
    assert asr_check.main(["--config", str(cfg), "use", "openai"]) == 0
    import json

    assert json.loads(cfg.read_text(encoding="utf-8"))["audio"] == {"transcriber": "openai", "enabled": True}
    assert "OpenAI の API" in capsys.readouterr().out


def test_no_arguments_means_check(monkeypatch):
    """`asr_check.cmd` をダブルクリック（引数なし）= check。"""
    called: list = []
    monkeypatch.setattr(asr_check, "_cmd_check", lambda args: called.append(args.command) or 0)
    assert asr_check.main([]) == 0 and called == ["check"]
