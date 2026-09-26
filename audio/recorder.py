from __future__ import annotations

import logging
import math
import queue
import threading
import time
import wave
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from audio.recognizer import WhisperTranscriber, is_implausibly_long, is_prompt_echo, parse_actions
from core.event_queue import EventQueue
from core.events import AudioEvent

logger = logging.getLogger(__name__)

# 無音判定の RMS 閾値（0〜32767 スケール）
_SILENCE_RMS_THRESHOLD = 300
# 音声チャンクの最大バッファ秒数（この秒数分溜まったら強制的に推論へ送る）
_MAX_BUFFER_SECONDS = 5.0
# 認識に回す最短の「有音」秒数（これ未満は咳・チップの音などとして捨てる）。店舗の実測で
# 「チェック」単独の発話が 0.3 秒に届かずに落ちていたため 0.15 秒にした（ADR-0061。
# config `audio.min_speech_sec` で変えられる）。
_MIN_BUFFER_SECONDS = 0.15
# 「短すぎて捨てた」を報告する下限（これ未満はチップの音などの一瞬の音として黙って捨てる）
_REPORT_DROP_MIN_SECONDS = 0.08
# 発話が始まる前の音も認識に回す秒数。有音ゲートを越える前の語頭（「ロッピャク」の「ロ」など小さい
# 子音）が切れないように（店舗の実測で短い語が崩れていた, 設計監査 2026-09-25）。
_PREROLL_SECONDS = 0.3
# 推論待ちがこの件数を超えたら WARN する（捨てはしない。認識が発話に追いつかないときは
# 遅れても全部処理する = 記録を落とさない方を取る, ADR-0061）。
_BACKLOG_WARN = 20

# 死活表示のレベル正規化に使う RMS 上限（これ以上は 1.0 に飽和。発話時の実測オーダー）
_HEALTH_LEVEL_FULL_RMS = 8000.0


@dataclass(frozen=True)
class Transcript:
    """1 発話の聞き取り結果（アクションとして読めなかったものも含む）。

    実機の音声テストで「何と聞こえたか」を見るためのもの（CLI の表示 / `tools/audio_check.py`）。
    """

    text: str
    confidence: Optional[float]
    events: tuple[AudioEvent, ...]          # アクションとして読めたもの（言った順。読めなければ空）
    audio_sec: float                       # 切り出した発話の長さ（秒）
    infer_sec: float                       # Whisper にかかった秒数
    utterance_start_ts: Optional[float]    # 話し始めの時刻（unix 秒）
    heard_at: float                        # 文字になった時刻（unix 秒）
    noise: bool = False                    # 雑音への幻聴（プロンプトの繰り返し）として捨てた（ADR-0063）
    audio_file: Optional[str] = None       # 保存した発話の音声（`audio_dir` があるとき, ファイル名）
    no_speech: bool = False                # 声が無い音（VAD）なので Whisper にかけなかった（text は空）


def describe_events(events) -> str:
    """1 発話から読めたアクションを 1 行で表す（複数なら「 / 」でつなぐ）。"""
    if not events:
        return describe_event(None)
    return " / ".join(describe_event(e) for e in events)


def describe_event(event: Optional[AudioEvent]) -> str:
    """聞き取り結果を 1 行で表す（ログ・CLI・audio_check で共通）。"""
    if event is None:
        return "アクションとして読めず"
    flags = event.parse_flags
    if "amount_only" in flags:
        action = "bet/raise"               # どちらかは engine が状態から決める
    elif "check_around" in flags:
        action = "check 全員"
    elif getattr(event, "hand_name", None):
        from core.showdown import HAND_NAMES_JA

        action = f"ハンド終了（{HAND_NAMES_JA.get(event.hand_name, event.hand_name)}）"
    else:
        action = event.action
    parts = [action]
    if event.amount:
        parts.append(str(event.amount))
    if event.seat is not None:
        parts.append(f"席{event.seat}")
    elif event.position:
        parts.append(event.position)
    shown = ["数字だけ" if f == "amount_only" else f for f in flags if f != "check_around"]
    if shown:
        parts.append("（" + "・".join(shown) + "）")
    return " ".join(parts)


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
        on_transcript: Optional[Callable[[Transcript], None]] = None,
        min_speech_sec: float = _MIN_BUFFER_SECONDS,
        on_dropped: Optional[Callable[[float], None]] = None,
        listen_gate: Optional[threading.Event] = None,
        speech_rms: float = _SILENCE_RMS_THRESHOLD,
        beam_size: int = 5,
        temperature_fallback: bool = False,
        audio_dir: Optional[Path] = None,
        vad_threshold: float = 0.5,
    ) -> None:
        """
        Args:
            transcriber: テスト用の差し替え点（None なら WhisperTranscriber を生成）。
                         `transcribe_with_confidence(bytes) -> (text, conf)` を持てばよい。
            on_transcript: 文字になった発話ごとに呼ぶ（アクションとして読めなかったものも）。
                         推論スレッドから呼ばれる。CLI の表示と `tools/audio_check.py` が使う。
            min_speech_sec: 認識に回す最短の有音秒数（config `audio.min_speech_sec`）。
            on_dropped: 短すぎて認識に回さなかった音ごとに有音秒数で呼ぶ（`audio_check listen`）。
            listen_gate: プレー中だけ set される Event（ADR-0063）。一度も set されていない間に話された
                         発話は認識に回さない（ハンドの間の会話で認識待ちがたまらないように）。None なら常に聞く。
            speech_rms: 有音とみなす RMS（config `audio.speech_rms`）。離れた席の会話を拾うなら上げる。
            beam_size / temperature_fallback / vad_threshold: `WhisperTranscriber` に渡す（config `audio.*`）。
            audio_dir: 認識に回した発話の音声を WAV で保存するフォルダ（config `audio.save_audio`）。
                         聞き違いの原因（語頭の切れ・音量・雑音）を店舗のデータで確かめるため。None なら保存しない。
        """
        super().__init__(daemon=True, name="AudioThread")
        self._audio_queue = audio_queue
        self._device_id = device_id
        self._sample_rate = sample_rate
        self._stop_event = stop_event or threading.Event()
        self._on_transcript = on_transcript
        self._min_speech_sec = min_speech_sec
        self._on_dropped = on_dropped
        self._listen_gate = listen_gate
        self._speech_rms = float(speech_rms)
        # プレー中でないため認識に回さなかった発話の数（ログ・テスト用）
        self.skipped = 0
        # 推論中の件数（0/1）と、発話を切り出している最中か。`backlog()` が待ちと合わせて返す。
        self._inferring = 0
        self._capturing = False
        # 推論中・切り出し中の発話の話し始め（`oldest_pending_start` が返す）
        self._inferring_start: Optional[float] = None
        self._capture_start: Optional[float] = None
        self._audio_dir = Path(audio_dir) if audio_dir is not None else None
        # 最後に声（有音ゲートを越える音）が入った時刻。プレー中に長く入らなければ engine がマイクの
        # 電池・受信機を疑って知らせる（ワイヤレスマイクの電池切れでハンドが丸ごと記録されなかった, 2026-09-25）。
        self.last_voice_at: Optional[float] = None
        self._transcriber = (
            transcriber if transcriber is not None
            else WhisperTranscriber(model_size=model_size, language=language,
                                    beam_size=beam_size, temperature_fallback=temperature_fallback,
                                    vad_threshold=vad_threshold)
        )
        # 推論待ちの (発話バイト列, 発話開始時刻)。None は worker 終了の sentinel。
        # 上限なし: 認識が遅れても発話を捨てない（プレーの切れ目で追いつく, ADR-0061）。
        self._chunk_queue: "queue.Queue[Optional[tuple[bytes, float]]]" = queue.Queue()
        # 死活表示（dashboard が読む。dict ごと差し替える = GIL で atomic、lock 不要）:
        #   state: starting | running | unavailable(pyaudio 無し) | error | stopped
        #   level: 直近チャンクの RMS を 0..1 に正規化 / last_chunk_at: unix 秒
        #   device_name: 開いたマイクの名前 / error: 開けなかった理由（CLI が起動時に表示する）
        self.health: dict = {"state": "starting", "level": 0.0, "last_chunk_at": None}
        self._device_name: Optional[str] = None

    def backlog(self) -> int:
        """まだアクションになっていない発話の数（推論待ち + 推論中）。

        CLI が `n` / `w` などを打たれたとき、先に言われた発話を追い越さないよう待つのに使う。
        """
        return self._chunk_queue.qsize() + self._inferring + (1 if self._capturing else 0)

    def oldest_pending_start(self) -> Optional[float]:
        """まだアクションになっていない発話のうち、一番早い話し始めの時刻（無ければ None）。

        engine が RFID の札の離脱を反映する前に、それより前に話された発話を先に反映するために使う
        （認識は数秒遅れる。ショーダウンで前に出した札をフォールドと取り違えないように）。
        """
        starts: list[float] = []
        with self._chunk_queue.mutex:
            starts.extend(item[1] for item in self._chunk_queue.queue if item is not None)
        for start in (self._inferring_start, self._capture_start):
            if start is not None:
                starts.append(start)
        return min(starts) if starts else None

    @property
    def asr_ready(self) -> bool:
        """音声認識モデルを読み込めたか（faster-whisper が無い・読み込みに失敗したら False）。"""
        return bool(getattr(self._transcriber, "ready", True))

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
            self._device_name = str(pa.get_device_info_by_index(self._device_id).get("name", ""))
        except Exception:  # 番号が範囲外など。open の失敗として下で扱う
            self._device_name = None
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
            self.health = {"state": "error", "level": 0.0, "last_chunk_at": None, "error": str(e)}
            pa.terminate()
            return
        logger.info("AudioThread started (device_id=%d %r, rate=%d)",
                    self._device_id, self._device_name, self._sample_rate)
        self.health = {"state": "running", "level": 0.0, "last_chunk_at": None,
                       "device_name": self._device_name}

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
            self._chunk_queue.put(None)  # worker へ終了 sentinel（たまっている発話を処理してから止まる）
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
        in_play = False  # この発話のあいだに一度でもプレー中だったか（ADR-0063）
        # 発話の立ち上がりの取りこぼしを防ぐ pre-roll（直前 `_PREROLL_SECONDS` 秒ぶんのチャンク）
        preroll: deque[bytes] = deque(
            maxlen=max(1, math.ceil(_PREROLL_SECONDS * self._sample_rate / chunk_size))
        )
        silence_chunks = 0
        silence_threshold_chunks = max(1, int(self._sample_rate / chunk_size * 0.5))  # 約0.5秒
        max_buffer_samples = int(_MAX_BUFFER_SECONDS * self._sample_rate)
        min_buffer_samples = int(self._min_speech_sec * self._sample_rate)
        report_drop_samples = int(_REPORT_DROP_MIN_SECONDS * self._sample_rate)

        def flush() -> None:
            nonlocal buffer, buffered_samples, voiced_samples, voiced
            nonlocal utterance_start_ts, silence_chunks, in_play
            # 最小長は「有音サンプル数」で判定する（末尾の無音でかさ増ししない）。
            if voiced and not in_play:
                # プレー中でない（ハンドの間の）発話は認識に回さない（ADR-0063）
                self.skipped += 1
                logger.debug("Skipped speech outside play (%.2f s)", buffered_samples / self._sample_rate)
            elif voiced and voiced_samples >= min_buffer_samples and utterance_start_ts is not None:
                self._enqueue_utterance(b"".join(buffer), utterance_start_ts)
            elif voiced and voiced_samples >= report_drop_samples:
                voiced_sec = voiced_samples / self._sample_rate
                logger.debug("Dropped a short sound (%.2f s voiced)", voiced_sec)
                if self._on_dropped is not None:
                    try:
                        self._on_dropped(voiced_sec)
                    except Exception:
                        logger.exception("on_dropped callback failed")
            buffer = []
            buffered_samples = 0
            voiced_samples = 0
            voiced = False
            utterance_start_ts = None
            silence_chunks = 0
            in_play = False
            self._capture_start = None

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
                "device_name": self._device_name,
            }

            is_voiced = rms >= self._speech_rms
            if is_voiced:
                self.last_voice_at = now
                silence_chunks = 0
                voiced_samples += len(data) // 2
                if not voiced:
                    # 発話の開始: pre-roll（直前のチャンク）から取り込み、開始時刻を記録。
                    voiced = True
                    utterance_start_ts = now - (len(data) // 2) / self._sample_rate
                    self._capture_start = utterance_start_ts
                    for chunk in preroll:
                        buffer.append(chunk)
                        buffered_samples += len(chunk) // 2
                    preroll.clear()
            else:
                silence_chunks += 1

            if voiced:
                if self._listen_gate is None or self._listen_gate.is_set():
                    in_play = True
                buffer.append(data)
                buffered_samples += len(data) // 2
                if (
                    silence_chunks >= silence_threshold_chunks
                    or buffered_samples >= max_buffer_samples
                ):
                    flush()
            self._capturing = voiced
            # 有音ゲート: 発話が始まるまでバッファは溜めない（無音を推論に送らない）。

            if not voiced:
                preroll.append(data)

        flush()  # 停止時に取り残しがあれば送る
        self._capturing = False

    def _enqueue_utterance(self, audio_bytes: bytes, utterance_start_ts: float) -> None:
        """発話チャンクを推論キューに積む（捨てない。たまりすぎたら警告だけ出す）。"""
        self._chunk_queue.put((audio_bytes, utterance_start_ts))
        waiting = self._chunk_queue.qsize()
        if waiting > _BACKLOG_WARN:
            logger.warning("聞き取りの処理待ちが %d 件たまっています（遅れても順に処理します）", waiting)

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
            self._inferring = 1
            self._inferring_start = utterance_start_ts
            try:
                self._process_chunk(audio_bytes, utterance_start_ts)
            finally:
                self._inferring = 0
                self._inferring_start = None

    def _process_chunk(
        self, audio_bytes: bytes, utterance_start_ts: Optional[float] = None
    ) -> None:
        """音声チャンクをテキストに変換し、アクションを検出して queue に送出する。

        文字になった発話はアクションとして読めなくても INFO で残し、`on_transcript` に渡す
        （実機の音声テストで「何と聞こえたか」を後から追えるように）。
        """
        try:
            started = time.time()
            audio_sec = len(audio_bytes) / 2 / self._sample_rate
            audio_file = self._save_audio(audio_bytes, utterance_start_ts or started)
            recognize = getattr(self._transcriber, "recognize", None)
            if callable(recognize):
                result = recognize(audio_bytes)
                text, confidence, no_speech = result.text, result.confidence, result.no_speech
            else:   # テストの差し替え（`transcribe_with_confidence` だけを持つ）
                text, confidence = self._transcriber.transcribe_with_confidence(audio_bytes)
                no_speech = False
            heard_at = time.time()
            infer_sec = heard_at - started
            if no_speech:
                # 声が無い音（札を混ぜる音など）は Whisper にかけていない。記録にだけ残す（CLI には出さない）。
                logger.info("声ではない音 %.1f 秒 — 聞き取りに回さず（VAD）", audio_sec)
                self._report(Transcript(
                    text="", confidence=None, events=(), audio_sec=audio_sec, infer_sec=infer_sec,
                    utterance_start_ts=utterance_start_ts, heard_at=heard_at, noise=True,
                    audio_file=audio_file, no_speech=True,
                ))
                return
            if not text:
                return
            noise = is_prompt_echo(text) or is_implausibly_long(text, audio_sec)
            # 続けて言った複数のアクションは言った順に分ける（ADR-0061）。雑音への幻聴は読まない（ADR-0063）。
            events = () if noise else tuple(parse_actions(
                text, confidence=confidence, utterance_start_ts=utterance_start_ts
            ))
            logger.info(
                "聞き取り: %r (confidence=%s, 推論 %.2f 秒) → %s",
                text, "-" if confidence is None else f"{confidence:.2f}", infer_sec,
                "雑音（聞き違い）として無視" if noise else describe_events(events),
            )
            self._report(Transcript(
                text=text, confidence=confidence, events=events,
                audio_sec=audio_sec,
                infer_sec=infer_sec, utterance_start_ts=utterance_start_ts,
                heard_at=heard_at, noise=noise, audio_file=audio_file,
            ))
            for event in events:
                self._audio_queue.put(event)
        except Exception:
            logger.exception("Error in _process_chunk (chunk size=%d bytes)", len(audio_bytes))

    def _report(self, transcript: Transcript) -> None:
        if self._on_transcript is None:
            return
        try:
            self._on_transcript(transcript)
        except Exception:
            logger.exception("on_transcript callback failed")

    def _save_audio(self, audio_bytes: bytes, started_at: float) -> Optional[str]:
        """認識に回す発話を WAV で保存し、ファイル名を返す（`audio_dir` が無ければ何もしない）。"""
        if self._audio_dir is None:
            return None
        name = f"{int(started_at * 1000)}.wav"
        try:
            self._audio_dir.mkdir(parents=True, exist_ok=True)
            with wave.open(str(self._audio_dir / name), "wb") as out:
                out.setnchannels(1)
                out.setsampwidth(2)
                out.setframerate(self._sample_rate)
                out.writeframes(audio_bytes)
        except OSError:
            logger.exception("発話の音声を保存できませんでした: %s", name)
            return None
        return name
