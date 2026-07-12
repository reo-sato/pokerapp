from __future__ import annotations

import logging
import math
import threading
import time
from typing import Optional

from audio.recognizer import WhisperTranscriber, parse_action
from core.event_queue import EventQueue

logger = logging.getLogger(__name__)

# 無音判定の RMS 閾値（0〜32767 スケール）
_SILENCE_RMS_THRESHOLD = 300
# 音声チャンクの最大バッファ秒数（この秒数分溜まったら強制的に推論へ送る）
_MAX_BUFFER_SECONDS = 5.0
# バッファフラッシュの最小秒数（短すぎる音声チャンクは無視）
_MIN_BUFFER_SECONDS = 0.3

# 死活表示のレベル正規化に使う RMS 上限（これ以上は 1.0 に飽和。発話時の実測オーダー）
_HEALTH_LEVEL_FULL_RMS = 8000.0


def _calc_rms(data: bytes) -> float:
    """PCM16 バイト列の RMS を計算する。"""
    if not data:
        return 0.0
    import struct

    n = len(data) // 2
    samples = struct.unpack(f"{n}h", data[:n * 2])
    return math.sqrt(sum(s * s for s in samples) / n)


class AudioThread(threading.Thread):
    """マイク音声を PyAudio でキャプチャし、faster-whisper で認識した AudioEvent を
    audio_queue に送出するデーモンスレッド。

    PyAudio がインストールされていない環境でも起動できる（ただし何もしない）。
    """

    def __init__(
        self,
        audio_queue: EventQueue,
        device_id: int = 0,
        sample_rate: int = 16000,
        model_size: str = "medium",
        language: str = "ja",
        stop_event: Optional[threading.Event] = None,
    ) -> None:
        super().__init__(daemon=True, name="AudioThread")
        self._audio_queue = audio_queue
        self._device_id = device_id
        self._sample_rate = sample_rate
        self._stop_event = stop_event or threading.Event()
        self._transcriber = WhisperTranscriber(model_size=model_size, language=language)
        # 死活表示（dashboard が読む。dict ごと差し替える = GIL で atomic、lock 不要）:
        #   state: starting | running | unavailable(pyaudio 無し) | error | stopped
        #   level: 直近チャンクの RMS を 0..1 に正規化 / last_chunk_at: unix 秒
        self.health: dict = {"state": "starting", "level": 0.0, "last_chunk_at": None}

    def stop(self) -> None:
        """スレッドの停止を要求する。"""
        self._stop_event.set()

    def run(self) -> None:
        try:
            import pyaudio  # type: ignore[import]
        except ImportError:
            logger.warning("pyaudio not installed. AudioThread will not capture audio.")
            self.health = {"state": "unavailable", "level": 0.0, "last_chunk_at": None}
            return

        pa = pyaudio.PyAudio()
        chunk_size = 1024
        try:
            stream = pa.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=self._sample_rate,
                input=True,
                input_device_index=self._device_id,
                frames_per_buffer=chunk_size,
            )
        except OSError as e:
            # デバイス不在/占有。クラッシュさせず死活表示に出す（エラーハンドリング方針）。
            logger.error("Could not open audio input device %d: %s", self._device_id, e)
            self.health = {"state": "error", "level": 0.0, "last_chunk_at": None}
            pa.terminate()
            return
        logger.info("AudioThread started (device_id=%d, rate=%d)", self._device_id, self._sample_rate)
        self.health = {"state": "running", "level": 0.0, "last_chunk_at": None}

        buffer: list[bytes] = []
        buffer_start_time: float = time.time()
        silence_chunks = 0
        silence_threshold_chunks = int(self._sample_rate / chunk_size * 0.5)  # 約0.5秒の無音

        try:
            while not self._stop_event.is_set():
                try:
                    data = stream.read(chunk_size, exception_on_overflow=False)
                except OSError as e:
                    logger.warning("Audio read error: %s", e)
                    continue

                rms = _calc_rms(data)
                self.health = {
                    "state": "running",
                    "level": min(1.0, rms / _HEALTH_LEVEL_FULL_RMS),
                    "last_chunk_at": time.time(),
                }
                buffer.append(data)
                elapsed = time.time() - buffer_start_time

                if rms < _SILENCE_RMS_THRESHOLD:
                    silence_chunks += 1
                else:
                    silence_chunks = 0

                # 無音が続いた or 最大バッファ秒数を超えたら推論へ送る
                should_flush = (
                    silence_chunks >= silence_threshold_chunks
                    or elapsed >= _MAX_BUFFER_SECONDS
                )

                if should_flush and elapsed >= _MIN_BUFFER_SECONDS:
                    audio_bytes = b"".join(buffer)
                    buffer = []
                    buffer_start_time = time.time()
                    silence_chunks = 0
                    self._process_chunk(audio_bytes)

        finally:
            stream.stop_stream()
            stream.close()
            pa.terminate()
            self.health = {"state": "stopped", "level": 0.0, "last_chunk_at": None}
            logger.info("AudioThread stopped")

    def _process_chunk(self, audio_bytes: bytes) -> None:
        """音声チャンクをテキストに変換し、アクションを検出して queue に送出する。"""
        try:
            text, confidence = self._transcriber.transcribe_with_confidence(audio_bytes)
            if not text:
                return
            logger.debug("Transcribed: %r (confidence=%s)", text, confidence)
            event = parse_action(text, confidence=confidence)
            if event is not None:
                logger.info("AudioEvent: action=%s amount=%d", event.action, event.amount)
                self._audio_queue.put(event)
        except Exception:
            logger.exception("Error in _process_chunk (chunk size=%d bytes)", len(audio_bytes))
