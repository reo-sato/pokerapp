"""audio/vosk_recorder.py

Vosk ベースのオフライン音声認識スレッド。
config.json の audio.engine = "vosk" で有効になる。

セットアップ:
  pip install vosk pyaudio
  # モデルのダウンロードと配置
  # https://alphacephei.com/vosk/models → vosk-model-small-ja-0.22.zip
  # 解凍して ./models/vosk-model-small-ja-0.22/ に配置する

認識結果は parse_action() を通して AudioEvent に変換し audio_queue に流す。
既存の IntegrationThread / GUI は Whisper 経路と同一インターフェースで動作する。
"""
from __future__ import annotations

import json
import logging
import os
import threading
from typing import Optional

from audio.recorder import _CHUNK_SIZE, open_input_stream
from audio.recognizer import parse_action
from core.event_queue import EventQueue

logger = logging.getLogger(__name__)


class VoskAudioThread(threading.Thread):
    """Vosk を使ってマイク音声を認識し、AudioEvent を audio_queue に送出するデーモンスレッド。

    grammar (list[str]) を渡すと KaldiRecognizer を限定語彙モードで初期化する。
    grammar=None の場合は通常認識モード（フリーテキスト）。

    PyAudio / vosk がインストールされていない環境でも起動できる（ただし何もしない）。
    """

    def __init__(
        self,
        audio_queue: EventQueue,
        device_id: Optional[int] = 0,
        sample_rate: int = 16000,
        model_path: str = "./models/vosk-model-small-ja-0.22",
        grammar: Optional[list[str]] = None,
        stop_event: Optional[threading.Event] = None,
        frames_per_buffer: Optional[int] = None,
    ) -> None:
        super().__init__(daemon=True, name="VoskAudioThread")
        self._audio_queue = audio_queue
        self._device_id: Optional[int] = (
            device_id if (device_id is not None and device_id >= 0) else None
        )
        self._sample_rate = sample_rate
        self._model_path = model_path
        self._grammar = grammar
        self._stop_event = stop_event or threading.Event()
        self._frames_per_buffer = frames_per_buffer

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        # ── 1. vosk インポート確認 ────────────────────────────────────────────
        try:
            import vosk  # type: ignore[import]
        except ImportError:
            logger.warning(
                "vosk not installed. VoskAudioThread will not capture audio. "
                "Install with: pip install vosk"
            )
            return

        # ── 2. モデルロード ───────────────────────────────────────────────────
        if not os.path.isdir(self._model_path):
            logger.error(
                "Vosk model not found: %r\n"
                "  Download: https://alphacephei.com/vosk/models\n"
                "  Recommended: vosk-model-small-ja-0.22.zip\n"
                "  Extract to: %r",
                self._model_path, self._model_path,
            )
            return

        try:
            vosk.SetLogLevel(-1)
            model = vosk.Model(self._model_path)
        except Exception:
            logger.exception("Failed to load Vosk model from %r", self._model_path)
            return

        # ── 3. KaldiRecognizer 初期化 ─────────────────────────────────────────
        if self._grammar:
            # ensure_ascii=True で \uXXXX エスケープにする。
            # Windows の Vosk C++ 層が UTF-8 文字列を CP932 として読む場合に
            # 日本語が文字化けするのを防ぐため、純粋な ASCII JSON を渡す。
            grammar_json = json.dumps(self._grammar, ensure_ascii=False)
            recognizer = vosk.KaldiRecognizer(model, float(self._sample_rate), grammar_json)
            logger.info(
                "Vosk grammar mode: %d words  (sample_rate=%d)",
                len(self._grammar), self._sample_rate,
            )
        else:
            recognizer = vosk.KaldiRecognizer(model, float(self._sample_rate))
            logger.info("Vosk free recognition mode (sample_rate=%d)", self._sample_rate)
        recognizer.SetWords(True)

        logger.info(
            "VoskAudioThread: engine=vosk device_id=%s rate=%d model=%r grammar=%s",
            self._device_id, self._sample_rate, self._model_path,
            "enabled" if self._grammar else "disabled",
        )

        # ── 4. PyAudio インポート確認 ──────────────────────────────────────────
        try:
            import pyaudio  # type: ignore[import]  # noqa: F401
        except ImportError:
            logger.warning("pyaudio not installed. VoskAudioThread will not capture audio.")
            return

        pa = pyaudio.PyAudio()
        try:
            stream = open_input_stream(
                pa, self._device_id, self._sample_rate, self._frames_per_buffer
            )
        except Exception:
            logger.exception(
                "VoskAudioThread: failed to open input stream (device_id=%s, rate=%d).",
                self._device_id, self._sample_rate,
            )
            pa.terminate()
            return

        chunk_size = _CHUNK_SIZE
        logger.info("VoskAudioThread started (chunk_size=%d)", chunk_size)

        # ── 5. 認識ループ ──────────────────────────────────────────────────────
        try:
            while not self._stop_event.is_set():
                try:
                    data = stream.read(chunk_size, exception_on_overflow=False)
                except OSError as e:
                    logger.warning("Vosk audio read error: %s", e)
                    continue

                if recognizer.AcceptWaveform(data):
                    result = json.loads(recognizer.Result())
                    text = result.get("text", "").strip()
                    logger.debug("Vosk final: %r", text)
                    if text:
                        event = parse_action(text)
                        if event is not None:
                            logger.info(
                                "VoskAudioEvent: action=%s amount=%d text=%r",
                                event.action, event.amount, text,
                            )
                            self._audio_queue.put(event)
                else:
                    partial = json.loads(recognizer.PartialResult()).get("partial", "")
                    if partial:
                        logger.debug("Vosk partial: %r", partial)

        finally:
            stream.stop_stream()
            stream.close()
            pa.terminate()
            logger.info("VoskAudioThread stopped")
