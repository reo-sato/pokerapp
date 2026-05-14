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
# stream.read() の読み取り単位（frames）。pa.open() の frames_per_buffer とは分離する
_CHUNK_SIZE = 1024


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
        device_id: Optional[int] = 0,
        sample_rate: int = 16000,
        model_size: str = "medium",
        language: str = "ja",
        stop_event: Optional[threading.Event] = None,
        frames_per_buffer: Optional[int] = None,
    ) -> None:
        super().__init__(daemon=True, name="AudioThread")
        self._audio_queue = audio_queue
        # None または負数は「既定入力デバイスを使う」扱いにする
        self._device_id: Optional[int] = (
            device_id if (device_id is not None and device_id >= 0) else None
        )
        self._sample_rate = sample_rate
        self._stop_event = stop_event or threading.Event()
        self._transcriber = WhisperTranscriber(model_size=model_size, language=language)
        # config から明示指定があれば最終フォールバックで使う（未指定なら _CHUNK_SIZE）
        self._frames_per_buffer: Optional[int] = frames_per_buffer

    def stop(self) -> None:
        """スレッドの停止を要求する。"""
        self._stop_event.set()

    def _open_input_stream(self, pa: object) -> object:
        """PyAudio 入力ストリームを段階的フォールバックで開く。

        試行順:
          1. 明示デバイス指定 + frames_per_buffer 未指定  (device_id が有効な場合のみ)
          2. 既定デバイス    + frames_per_buffer 未指定
          3. 既定デバイス    + frames_per_buffer 指定      (最終フォールバック)
        """
        import pyaudio  # noqa: PLC0415

        # 既定入力デバイス情報をログ
        try:
            di = pa.get_default_input_device_info()
            logger.info(
                "Default input device: id=%d name=%r channels=%d defaultRate=%.0f",
                di["index"], di["name"], di["maxInputChannels"], di["defaultSampleRate"],
            )
        except Exception as e:
            logger.warning("Could not get default input device info: %s", e)

        # 明示指定デバイスの情報をログ
        if self._device_id is not None:
            try:
                di = pa.get_device_info_by_index(self._device_id)
                logger.info(
                    "Requested input device: id=%d name=%r channels=%d defaultRate=%.0f",
                    self._device_id, di["name"],
                    di["maxInputChannels"], di["defaultSampleRate"],
                )
            except Exception as e:
                logger.warning(
                    "Could not get device info for device_id=%d: %s", self._device_id, e
                )

        base = dict(
            format=pyaudio.paInt16,
            channels=1,
            rate=self._sample_rate,
            input=True,
        )
        fallback_fpb = self._frames_per_buffer if self._frames_per_buffer is not None else _CHUNK_SIZE

        # 試行リストを構築
        attempts: list[dict] = []
        if self._device_id is not None:
            attempts.append({**base, "input_device_index": self._device_id})
        attempts.append(base.copy())
        attempts.append({**base, "frames_per_buffer": fallback_fpb})

        last_exc: Optional[Exception] = None
        for i, kwargs in enumerate(attempts, start=1):
            try:
                stream = pa.open(**kwargs)
                logger.info(
                    "pa.open() succeeded: attempt=%d device_id=%s frames_per_buffer=%s",
                    i,
                    kwargs.get("input_device_index", "default"),
                    kwargs.get("frames_per_buffer", "unset"),
                )
                return stream
            except OSError as e:
                logger.warning(
                    "pa.open() failed: attempt=%d device_id=%s rate=%d channels=1 "
                    "frames_per_buffer=%s error=%s",
                    i,
                    kwargs.get("input_device_index", "default"),
                    self._sample_rate,
                    kwargs.get("frames_per_buffer", "unset"),
                    e,
                )
                last_exc = e

        raise OSError(f"All pa.open() attempts failed. Last error: {last_exc}") from last_exc

    def run(self) -> None:
        try:
            import pyaudio  # type: ignore[import]  # noqa: F401
        except ImportError:
            logger.warning("pyaudio not installed. AudioThread will not capture audio.")
            return

        pa = pyaudio.PyAudio()
        try:
            stream = self._open_input_stream(pa)
        except Exception:
            logger.exception(
                "AudioThread: failed to open input stream (device_id=%s, rate=%d). "
                "AudioThread will exit.",
                self._device_id,
                self._sample_rate,
            )
            pa.terminate()
            return

        chunk_size = _CHUNK_SIZE
        logger.info(
            "AudioThread started (device_id=%s, rate=%d, chunk_size=%d)",
            self._device_id, self._sample_rate, chunk_size,
        )

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
            logger.info("AudioThread stopped")

    def _process_chunk(self, audio_bytes: bytes) -> None:
        """音声チャンクをテキストに変換し、アクションを検出して queue に送出する。"""
        try:
            text = self._transcriber.transcribe(audio_bytes)
            if not text:
                return
            logger.debug("Transcribed: %r", text)
            event = parse_action(text)
            if event is not None:
                logger.info("AudioEvent: action=%s amount=%d", event.action, event.amount)
                self._audio_queue.put(event)
        except Exception:
            logger.exception("Error in _process_chunk (chunk size=%d bytes)", len(audio_bytes))
