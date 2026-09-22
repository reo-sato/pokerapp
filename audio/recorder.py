from __future__ import annotations

import logging
import math
import queue
import threading
import time
from typing import Callable, Optional

from audio.recognizer import WhisperTranscriber, parse_action
from core.event_queue import EventQueue

logger = logging.getLogger(__name__)

# 無音判定の RMS 閾値（0〜32767 スケール）
_SILENCE_RMS_THRESHOLD = 300
# 音声チャンクの最大バッファ秒数（この秒数分溜まったら強制的に推論へ送る）
_MAX_BUFFER_SECONDS = 5.0
# バッファフラッシュの最小秒数（短すぎる音声チャンクは無視）
_MIN_BUFFER_SECONDS = 0.3
# 推論待ちキューの上限（推論がキャプチャより遅い場合のバックプレッシャ。超過は最古を捨てる）
_INFERENCE_QUEUE_MAX = 8

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

    ADR-C (T4) の設計:
    - **キャプチャと推論を分離**する。キャプチャループは録音のみを行い、発話チャンクを
      内部キューに積む。推論は worker スレッドが行う（従来は推論がキャプチャを同期ブロックし、
      推論中の発話を取りこぼしていた）。
    - **有音ゲート**: 発話（RMS が閾値以上）を含まないバッファは推論に送らない
      （無音入力に initial_prompt をオウム返しするハルシネーション対策の第一線）。
    - **フラッシュ判定はサンプル数ベース**（wall-clock ではなく取得サンプル量。
      read 遅延やドリフトの影響を受けない）。
    - **発話開始時刻（utterance_start_ts）を AudioEvent に記録**する（ADR-B T1:
      ASR デコード遅延に依らないセンサー照合窓の始端）。

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
        transcriber: Optional[WhisperTranscriber] = None,
    ) -> None:
        """
        Args:
            transcriber: テスト用の差し替え点（None なら WhisperTranscriber を生成）。
                         `transcribe_with_confidence(bytes) -> (text, conf)` を持てばよい。
        """
        super().__init__(daemon=True, name="AudioThread")
        self._audio_queue = audio_queue
        self._device_id = device_id
        self._sample_rate = sample_rate
        self._stop_event = stop_event or threading.Event()
        self._transcriber = (
            transcriber if transcriber is not None
            else WhisperTranscriber(model_size=model_size, language=language)
        )
        # 推論待ちの (発話バイト列, 発話開始時刻)。None は worker 終了の sentinel。
        self._chunk_queue: "queue.Queue[Optional[tuple[bytes, float]]]" = queue.Queue(
            maxsize=_INFERENCE_QUEUE_MAX
        )
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

        worker = threading.Thread(
            target=self._inference_loop, daemon=True, name="AudioInference"
        )
        worker.start()

        try:
            self._capture_loop(
                lambda: stream.read(chunk_size, exception_on_overflow=False),
                chunk_size,
            )
        finally:
            stream.stop_stream()
            stream.close()
            pa.terminate()
            try:
                self._chunk_queue.put_nowait(None)  # worker へ終了 sentinel
            except queue.Full:
                pass
            self.health = {"state": "stopped", "level": 0.0, "last_chunk_at": None}
            logger.info("AudioThread stopped")

    # ――― キャプチャ（録音専用。推論でブロックしない） ―――

    def _capture_loop(self, read_chunk: Callable[[], bytes], chunk_size: int) -> None:
        """録音チャンクを読み続け、発話区間を切り出して推論キューに積む。

        `read_chunk` を注入可能にして PyAudio 無しでテストできる seam にする（ADR-C T4）。
        """
        buffer: list[bytes] = []
        buffered_samples = 0
        voiced_samples = 0
        voiced = False
        utterance_start_ts: Optional[float] = None
        prev_chunk: Optional[bytes] = None  # 発話立ち上がりの取りこぼし防止の 1 チャンク pre-roll
        silence_chunks = 0
        silence_threshold_chunks = max(1, int(self._sample_rate / chunk_size * 0.5))  # 約0.5秒
        max_buffer_samples = int(_MAX_BUFFER_SECONDS * self._sample_rate)
        min_buffer_samples = int(_MIN_BUFFER_SECONDS * self._sample_rate)

        def flush() -> None:
            nonlocal buffer, buffered_samples, voiced_samples, voiced
            nonlocal utterance_start_ts, silence_chunks
            # 最小長は「有音サンプル数」で判定する（末尾の無音でかさ増ししない）。
            if voiced and voiced_samples >= min_buffer_samples and utterance_start_ts is not None:
                self._enqueue_utterance(b"".join(buffer), utterance_start_ts)
            buffer = []
            buffered_samples = 0
            voiced_samples = 0
            voiced = False
            utterance_start_ts = None
            silence_chunks = 0

        while not self._stop_event.is_set():
            try:
                data = read_chunk()
            except OSError as e:
                logger.warning("Audio read error: %s", e)
                continue
            if not data:
                break  # テスト用 fake stream の終端

            now = time.time()
            rms = _calc_rms(data)
            self.health = {
                "state": "running",
                "level": min(1.0, rms / _HEALTH_LEVEL_FULL_RMS),
                "last_chunk_at": now,
            }

            is_voiced = rms >= _SILENCE_RMS_THRESHOLD
            if is_voiced:
                silence_chunks = 0
                voiced_samples += len(data) // 2
                if not voiced:
                    # 発話の開始: pre-roll（直前チャンク）から取り込み、開始時刻を記録。
                    voiced = True
                    utterance_start_ts = now - (len(data) // 2) / self._sample_rate
                    if prev_chunk is not None:
                        buffer.append(prev_chunk)
                        buffered_samples += len(prev_chunk) // 2
            else:
                silence_chunks += 1

            if voiced:
                buffer.append(data)
                buffered_samples += len(data) // 2
                if (
                    silence_chunks >= silence_threshold_chunks
                    or buffered_samples >= max_buffer_samples
                ):
                    flush()
            # 有音ゲート: 発話が始まるまでバッファは溜めない（無音を推論に送らない）。

            prev_chunk = data

        flush()  # 停止時に取り残しがあれば送る

    def _enqueue_utterance(self, audio_bytes: bytes, utterance_start_ts: float) -> None:
        """発話チャンクを推論キューに積む。満杯なら最古を捨てて警告する（新しい発話を優先）。"""
        item = (audio_bytes, utterance_start_ts)
        try:
            self._chunk_queue.put_nowait(item)
        except queue.Full:
            try:
                self._chunk_queue.get_nowait()
            except queue.Empty:
                pass
            logger.warning(
                "Inference queue full — dropping oldest utterance (ASR falling behind)"
            )
            try:
                self._chunk_queue.put_nowait(item)
            except queue.Full:
                logger.warning("Inference queue still full — utterance dropped")

    # ――― 推論 worker ―――

    def _inference_loop(self) -> None:
        """推論キューを消費して AudioEvent を送出する（キャプチャと独立に動く）。"""
        while True:
            try:
                item = self._chunk_queue.get(timeout=0.2)
            except queue.Empty:
                if self._stop_event.is_set():
                    break
                continue
            if item is None:
                break
            audio_bytes, utterance_start_ts = item
            self._process_chunk(audio_bytes, utterance_start_ts)

    def _process_chunk(
        self, audio_bytes: bytes, utterance_start_ts: Optional[float] = None
    ) -> None:
        """音声チャンクをテキストに変換し、アクションを検出して queue に送出する。"""
        try:
            text, confidence = self._transcriber.transcribe_with_confidence(audio_bytes)
            if not text:
                return
            logger.debug("Transcribed: %r (confidence=%s)", text, confidence)
            event = parse_action(
                text, confidence=confidence, utterance_start_ts=utterance_start_ts
            )
            if event is not None:
                logger.info("AudioEvent: action=%s amount=%d", event.action, event.amount)
                self._audio_queue.put(event)
        except Exception:
            logger.exception("Error in _process_chunk (chunk size=%d bytes)", len(audio_bytes))
