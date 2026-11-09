"""core/remote_asr.py

書き起こしの API（ADR-0072 追記 1・2, 2026-11-09）: 店舗 PC の Whisper の代わりに、OpenAI の whisper-1 で 1 発話ずつ
書き起こす。11/05 は今の Whisper と同じ 45/72、10/29 の声のハンドで 14 → 17/36。1 発話 1 秒前後で、店舗 PC の
CPU を使わない（書き起こしが溜まって店舗 PC がビジーになる・後回しでプレー中に履歴を見られない、を解く）。

- テスト中は店舗 PC から直接呼ぶ（オーナー 2026-11-09）。鍵は環境変数 `POKERAPP_OPENAI_API_KEY`（店舗 PC では `setx` で
  1 回登録）。リポジトリ・config・ログには置かない。他の店に配る前にクラウドの中継へ切り替える。
- 声かどうか（Silero VAD）は今までどおり店舗 PC で判定し、声でなければ API を呼ばない（費用と雑音への幻聴を減らす）。
- 失敗（ネット・API）は `RemoteAsrError` にする。録音側は音声を保存したまま後回しの列へ回し、つながったら送り直す。
- 自信の値は返さない（API は返さない。推定器は欠測として扱う）。

`tools/cloud_asr.py`（比べる道具）も、ここの `multipart` と `wav_bytes` を使う。
"""
from __future__ import annotations

import io
import json
import logging
import os
import urllib.error
import urllib.request
import uuid
import wave
from typing import Callable, Optional

from core.constants import WHISPER_PROMPT_JA

logger = logging.getLogger(__name__)

OPENAI_URL = "https://api.openai.com/v1/audio/transcriptions"
KEY_ENV = ("POKERAPP_OPENAI_API_KEY", "OPENAI_API_KEY")
DEFAULT_MODEL = "whisper-1"
TIMEOUT_SEC = 15.0


class RemoteAsrError(Exception):
    """書き起こしの API を呼べなかった（ネット・API の失敗・鍵が無い）。"""


def api_key() -> Optional[str]:
    """書き起こしの API の鍵（環境変数）。無ければ None。"""
    return next((os.environ[n] for n in KEY_ENV if os.environ.get(n)), None)


def wav_bytes(pcm16: bytes, sample_rate: int = 16000) -> bytes:
    """16 bit モノラルの PCM に WAV の頭を付ける（API は WAV のファイルとして受け取る）。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(sample_rate)
        out.writeframes(pcm16)
    return buf.getvalue()


def multipart(fields: dict[str, str], wav: bytes) -> tuple[bytes, str]:
    """multipart/form-data の本文と Content-Type（標準の部品だけで作る）。"""
    boundary = uuid.uuid4().hex
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
             for k, v in fields.items()]
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="a.wav"\r\n'
                 "Content-Type: audio/wav\r\n\r\n".encode() + wav + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def post(url: str, body: bytes, headers: dict[str, str], timeout: float = TIMEOUT_SEC) -> dict:
    """POST して JSON を返す。失敗は `RemoteAsrError`（鍵は例外の文に入れない）。"""
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise RemoteAsrError(f"HTTP {e.code}: {detail}") from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise RemoteAsrError(f"書き起こしの API につながりません: {getattr(e, 'reason', e)}") from e


def transcribe_openai(wav: bytes, *, key: str, model: str = DEFAULT_MODEL, prompt: Optional[str] = WHISPER_PROMPT_JA,
                      language: str = "ja", timeout: float = TIMEOUT_SEC,
                      post_fn: Optional[Callable[..., dict]] = None) -> str:
    """1 発話の WAV を OpenAI で書き起こした文（ライブの Whisper と同じプロンプト・言語・温度 0）。"""
    fields = {"model": model, "language": language, "response_format": "json", "temperature": "0"}
    if prompt:
        fields["prompt"] = prompt
    body, ctype = multipart(fields, wav)
    data = (post_fn or post)(OPENAI_URL, body, {"Authorization": f"Bearer {key}", "Content-Type": ctype},
                             timeout=timeout)
    return str((data or {}).get("text") or "").strip()


class OpenAITranscriber:
    """`audio.recognizer.WhisperTranscriber` と同じ約束（`recognize(bytes) -> Recognition`・`ready`・`load_error`）で、
    書き起こしを OpenAI の API にする。`recognize` は失敗すると `RemoteAsrError` を投げる（録音側が後回しの列へ回す）。"""

    remote = True       # 録音側が、つながらないときに後回しの列へ回す印

    def __init__(self, model: str = DEFAULT_MODEL, language: str = "ja", vad_threshold: float = 0.5,
                 sample_rate: int = 16000, timeout: float = TIMEOUT_SEC, key: Optional[str] = None,
                 post_fn: Optional[Callable[..., dict]] = None, prompt: Optional[str] = WHISPER_PROMPT_JA) -> None:
        self.model = model
        self.asr_name = f"openai-{model}"
        self._language = language
        self._vad_threshold = float(vad_threshold)
        self._sample_rate = int(sample_rate)
        self._timeout = float(timeout)
        self._key = key if key is not None else api_key()
        self._post = post_fn
        self._prompt = prompt
        self.load_error: Optional[str] = None if self._key else "書き起こしの API の鍵がありません（POKERAPP_OPENAI_API_KEY）"

    @property
    def ready(self) -> bool:
        """鍵があるか（API を呼べるかは、呼んでみるまで分からない）。"""
        return bool(self._key)

    def has_speech(self, audio_array) -> bool:
        """音に声が含まれるか（Silero VAD、`WhisperTranscriber.has_speech` と同じ）。使えなければ True。"""
        if self._vad_threshold <= 0:
            return True
        try:
            from faster_whisper.vad import VadOptions, get_speech_timestamps  # type: ignore[import]

            options = VadOptions(threshold=self._vad_threshold, min_speech_duration_ms=100)
            return bool(get_speech_timestamps(audio_array, options))
        except Exception:  # noqa: BLE001 — VAD が動かなければ全部送る
            logger.warning("VAD が使えないので、有音の音をすべて書き起こしの API に送ります", exc_info=True)
            self._vad_threshold = 0.0
            return True

    def recognize(self, audio_bytes: bytes):
        from audio.recognizer import Recognition

        if not self._key:
            raise RemoteAsrError(self.load_error or "鍵がありません")
        try:
            import numpy as np

            audio_array = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        except ImportError:
            audio_array = None
        if audio_array is not None and not self.has_speech(audio_array):
            return Recognition("", None, no_speech=True)
        text = transcribe_openai(wav_bytes(audio_bytes, self._sample_rate), key=self._key, model=self.model,
                                 prompt=self._prompt, language=self._language, timeout=self._timeout,
                                 post_fn=self._post)
        return Recognition(text, None)

    # 古い差し替えの約束（`transcribe_with_confidence`）も満たす
    def transcribe_with_confidence(self, audio_bytes: bytes) -> tuple[str, Optional[float]]:
        result = self.recognize(audio_bytes)
        return result.text, result.confidence
