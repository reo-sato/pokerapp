#!/usr/bin/env python3
"""tools/audio_check.py — マイクと音声認識の確認（店舗 PC での音声テストの前に, ADR-0060）。

`tools/probe_pcsc.py`（RFID）と対になる音声側の bring-up 診断。本番のハンドロガーと同じ
config（`audio.device_id` / `sample_rate` / `whisper_model` / `language`）と同じ経路
（`AudioThread` の発話切り出し → faster-whisper → `parse_action`）を使う。

サブコマンド:
  list    入力デバイスの一覧（番号・方式・16 kHz で開けるか）。config の `audio.device_id` に印
  level   入力レベルを表示する（しきい値を超えた = 発話として拾う）。マイクの位置・音量の確認
  listen  本番と同じ経路で聞き取り、発話ごとに「何と聞こえたか → どのアクションになったか」と
          かかった時間を表示する（初回は音声認識モデル ≈ 1.5 GB をダウンロード）
  bench   保存した発話の音声（`logs/audio/<セッション>/`）で、Whisper の CPU スレッド数（とビーム幅）ごとの
          聞き取りの時間を測る。いちばん速い設定と、それを config に入れるコマンドを表示する（マイク不要）

使用例:
  python tools/audio_check.py list
  python tools/audio_check.py level --seconds 15
  python tools/audio_check.py listen --seconds 120
  python tools/audio_check.py listen --device 1 --model small
  python tools/audio_check.py bench                 # スレッド 4 と 8 を比べる
  python tools/audio_check.py bench --threads 4,8,16 --count 20
  python tools/audio_check.py bench --threads 4,8 --joined 3   # 発話を 3 つずつまとめて聞き取る試しも
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import threading
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_JSON = _ROOT / "config.json"
_CONFIG_DEFAULT_JSON = _ROOT / "config_default.json"

# レベル表示の上限（AudioThread の死活表示と同じ正規化）
_LEVEL_FULL_RMS = 8000.0
_BAR_WIDTH = 20
# これ未満しか来ないなら「無音」とみなす（プライバシー設定・RDP の音声設定で 0 が続く）
_NEAR_SILENT_RMS = 50.0
_EXAMPLES = "例: 「ハンド開始」「レイズ 600」「コール」「チェック」「フォールド」「オールイン」"


# ――― config（読むだけ。config.json は作らない） ―――

def load_audio_config(config_path: str | Path | None = None) -> dict:
    """`audio` セクションを返す（明示パス > config.json > config_default.json）。"""
    if config_path is not None:
        target = Path(config_path)
    elif _CONFIG_JSON.exists():
        target = _CONFIG_JSON
    else:
        target = _CONFIG_DEFAULT_JSON
    return json.loads(Path(target).read_text(encoding="utf-8-sig")).get("audio", {})


def _import_pyaudio():
    try:
        import pyaudio  # type: ignore[import]
    except ImportError:
        print("PyAudio が入っていません（pip install pyaudio）。", file=sys.stderr)
        return None
    return pyaudio


# ――― list ―――

@dataclass
class InputDevice:
    index: int
    name: str
    api: str
    rate_ok: bool
    default: bool


# RDP の音声が接続元に回っていると、PC のマイクは MME などから見えず WDM-KS 方式だけに出る。WDM-KS では
# Bluetooth のマイクを開けない（店舗 2026-09-30: DJI Mic Mini 2 が「[Errno -9999] Unanticipated host error」）
RDP_AUDIO_HINT = ("マイクが WDM-KS 方式でしか見えていません。リモートデスクトップの音声が接続元の端末に回っていると"
                  "こうなり、WDM-KS では Bluetooth のマイクを開けません。接続元の設定で音声の再生を「リモート PC で再生」"
                  "（Windows の「リモート デスクトップ接続」は「リモート コンピューターで再生する」）にしてつなぎ直し、"
                  "マイクを探し直してください。")
WDM_KS_OPEN_HINT = "WDM-KS の番号は開けないことがあります。MME の番号を選んでください。"


def _api_of(device: Any) -> str:
    return str(device.get("api", "") if isinstance(device, dict) else getattr(device, "api", ""))


def only_wdm_ks(devices: list) -> bool:
    """録音できるデバイスが WDM-KS 方式だけか（= RDP の音声が接続元に回っている）。"""
    return bool(devices) and all("WDM-KS" in _api_of(d) for d in devices)


def open_error_hint(device: Any) -> str:
    """マイクを開けなかったときに添える一言（WDM-KS の番号なら MME を勧める）。"""
    return WDM_KS_OPEN_HINT if "WDM-KS" in _api_of(device) else ""


def list_input_devices(pa, pyaudio_mod, sample_rate: int) -> list[InputDevice]:
    """録音できるデバイス（入力チャンネルがあるもの）を番号順に返す。"""
    try:
        default_index: Optional[int] = int(pa.get_default_input_device_info()["index"])
    except Exception:  # 既定の入力が無い（マイク未接続・RDP でリダイレクトされていない等）
        default_index = None
    devices: list[InputDevice] = []
    for i in range(pa.get_device_count()):
        info = pa.get_device_info_by_index(i)
        if int(info.get("maxInputChannels", 0)) <= 0:
            continue
        try:
            api = str(pa.get_host_api_info_by_index(info["hostApi"])["name"])
        except Exception:
            api = "?"
        try:
            rate_ok = bool(pa.is_format_supported(
                sample_rate, input_device=i, input_channels=1,
                input_format=pyaudio_mod.paInt16,
            ))
        except ValueError:
            rate_ok = False
        devices.append(InputDevice(i, str(info.get("name", "")), api, rate_ok, i == default_index))
    return devices


def format_device_table(devices: list[InputDevice], configured: int, sample_rate: int) -> list[str]:
    khz = f"{sample_rate // 1000}kHz"
    lines = [f"  番号  {khz:5}  方式              名前"]
    for d in devices:
        marks = []
        if d.default:
            marks.append("既定")
        if d.index == configured:
            marks.append("← config の audio.device_id")
        lines.append(
            f"  {d.index:4d}  {'OK' if d.rate_ok else '不可':5}  {d.api:16}  {d.name}"
            + (f"  {' '.join(marks)}" if marks else "")
        )
    return lines


def _cmd_list(args: argparse.Namespace) -> int:
    pyaudio_mod = _import_pyaudio()
    if pyaudio_mod is None:
        return 1
    cfg = load_audio_config(args.config)
    rate = int(cfg.get("sample_rate", 16000))
    configured = int(cfg.get("device_id", 0))
    pa = pyaudio_mod.PyAudio()
    try:
        devices = list_input_devices(pa, pyaudio_mod, rate)
    finally:
        pa.terminate()
    if not devices:
        print("録音できるデバイスがありません。")
        print("  - マイクが PC に挿さっているか、Windows の「サウンド」→「録音」に出ているかを確認")
        print("  - RDP でつないでいるなら、リモートデスクトップの音声を「リモート コンピューターで再生」にして"
              "つなぎ直す（docs/troubleshooting.md）")
        return 1
    print("録音できるデバイス:")
    for line in format_device_table(devices, configured, rate):
        print(line)
    chosen = next((d for d in devices if d.index == configured), None)
    print()
    if only_wdm_ks(devices):
        print(RDP_AUDIO_HINT)
        print()
    if chosen is None:
        print(f"config の audio.device_id = {configured} は上の一覧にありません。")
    elif not chosen.rate_ok:
        print(f"config の audio.device_id = {configured} は {rate // 1000} kHz で開けません"
              "（同じマイクの「MME」の番号を選んでください）。")
    else:
        print(f"config の audio.device_id = {configured}（{chosen.name}）を使います。")
    print("番号を変えるとき: python tools/set_config.py audio.device_id <番号>")
    print("「16kHz 不可」の番号は選ばない（同じマイクが方式ごとに並ぶ。MME の番号が無難）。")
    return 0 if chosen is not None and chosen.rate_ok else 1


# ――― level ―――

@dataclass
class LevelStats:
    chunks: int = 0
    voiced: int = 0
    max_rms: float = 0.0

    @property
    def voiced_share(self) -> float:
        return self.voiced / self.chunks if self.chunks else 0.0


def level_bar(rms: float) -> str:
    filled = min(_BAR_WIDTH, int(round(rms / _LEVEL_FULL_RMS * _BAR_WIDTH)))
    return "█" * filled + "░" * (_BAR_WIDTH - filled)


def measure_level(
    read_chunk: Callable[[], bytes], sample_rate: int, chunk_size: int, seconds: float,
    threshold: float, on_line: Callable[[str], None], line_every_sec: float = 0.25,
) -> LevelStats:
    """`seconds` 秒ぶんのチャンクを読み、`line_every_sec` ごとに区間の最大レベルを 1 行出す。"""
    from audio.recorder import _calc_rms

    stats = LevelStats()
    total = max(1, int(seconds * sample_rate / chunk_size))
    per_line = max(1, int(line_every_sec * sample_rate / chunk_size))
    window_max = 0.0
    for n in range(1, total + 1):
        data = read_chunk()
        if not data:
            break
        rms = _calc_rms(data)
        stats.chunks += 1
        stats.max_rms = max(stats.max_rms, rms)
        if rms >= threshold:
            stats.voiced += 1
        window_max = max(window_max, rms)
        if n % per_line == 0:
            t = n * chunk_size / sample_rate
            mark = "発話" if window_max >= threshold else ""
            on_line(f"  {t:5.1f}s  {level_bar(window_max)}  {window_max:6.0f}  {mark}")
            window_max = 0.0
    return stats


def level_summary(stats: LevelStats, threshold: float) -> list[str]:
    lines = [f"最大 {stats.max_rms:.0f}（発話とみなす目安 {threshold:.0f} 以上）"
             f" / 発話の割合 {stats.voiced_share:.0%}"]
    if stats.max_rms < _NEAR_SILENT_RMS:
        lines.append("ほぼ無音です。Windows の設定 →「プライバシーとセキュリティ」→「マイク」で"
                     "「デスクトップ アプリがマイクにアクセスできるようにする」をオンに。"
                     "RDP でつないでいるなら音声の設定も確認（docs/troubleshooting.md）。")
    elif stats.max_rms < threshold:
        lines.append("声がしきい値に届いていません。マイクを口元に近づけるか、Windows の録音レベルを上げてください。")
    return lines


def _cmd_level(args: argparse.Namespace) -> int:
    pyaudio_mod = _import_pyaudio()
    if pyaudio_mod is None:
        return 1
    from audio.recorder import _SILENCE_RMS_THRESHOLD

    cfg = load_audio_config(args.config)
    rate = int(cfg.get("sample_rate", 16000))
    device = args.device if args.device is not None else int(cfg.get("device_id", 0))
    chunk = 1024
    pa = pyaudio_mod.PyAudio()
    try:
        try:
            stream = pa.open(format=pyaudio_mod.paInt16, channels=1, rate=rate, input=True,
                             input_device_index=device, frames_per_buffer=chunk)
        except (OSError, ValueError) as e:
            print(f"マイク（番号 {device}）を開けませんでした: {e}")
            print("番号は `python tools/audio_check.py list` で確かめてください。")
            return 1
        print(f"マイク（番号 {device}）の入力レベルを {args.seconds:.0f} 秒表示します。卓で話してください。")
        try:
            stats = measure_level(
                lambda: stream.read(chunk, exception_on_overflow=False), rate, chunk,
                args.seconds, float(_SILENCE_RMS_THRESHOLD), print,
            )
        except KeyboardInterrupt:
            stats = None
        finally:
            stream.stop_stream()
            stream.close()
    finally:
        pa.terminate()
    if stats is not None:
        print()
        for line in level_summary(stats, float(_SILENCE_RMS_THRESHOLD)):
            print(line)
    return 0


# ――― listen ―――

@dataclass
class ListenStats:
    heard: int = 0            # 文字になった発話
    actions: int = 0          # そこから読めたアクション（1 発話に複数あれば複数）
    unreadable: int = 0       # アクションとして読めなかった発話
    dropped: int = 0          # 短すぎて認識に回さなかった音
    no_speech: int = 0        # 声ではない音（VAD）として Whisper にかけなかった音
    infer_secs: list[float] = field(default_factory=list)

    def summary(self) -> str:
        dropped = f"・短すぎて認識しなかった音 {self.dropped} 件" if self.dropped else ""
        if self.no_speech:
            dropped += f"・声ではない音 {self.no_speech} 件"
        if not self.heard:
            return "聞き取れた発話はありませんでした。" + dropped
        avg = sum(self.infer_secs) / len(self.infer_secs)
        return (f"発話 {self.heard} 件 → アクション {self.actions} 件（読めなかった発話 {self.unreadable} 件）"
                f"・認識 平均 {avg:.1f} 秒 / 最大 {max(self.infer_secs):.1f} 秒" + dropped)


def format_transcript(t) -> str:
    """1 発話の表示（`audio.recorder.Transcript`）。"""
    from audio.recorder import describe_events

    clock = time.strftime("%H:%M:%S", time.localtime(t.heard_at))
    details = []
    if t.confidence is not None:
        details.append(f"信頼度 {t.confidence:.2f}")
    details.append(f"認識 {t.infer_sec:.1f} 秒")
    if t.utterance_start_ts is not None:
        details.append(f"話し始めから {t.heard_at - t.utterance_start_ts:.1f} 秒")
    if getattr(t, "no_speech", False):
        return (f"  {clock}  （声ではない音 {t.audio_sec:.1f} 秒 — Whisper にかけず。"
                "言葉なら audio.vad_threshold を下げる）")
    from audio.recorder import QUESTION_NOTE

    ear = getattr(t, "ear", None)
    if getattr(t, "ear_text", None):
        heard = f"第 2 の耳「{t.ear_text}」→ {describe_events(t.events)}"
    elif getattr(t, "noise", False):
        heard = "雑音（聞き違い）として無視"
    elif getattr(t, "question", False):
        heard = QUESTION_NOTE
    else:
        heard = describe_events(t.events)
    if ear is not None:
        details.append(f"第 2 の耳「{ear.get('text') or ''}」{ear.get('sec') or 0:.1f} 秒")
    return f"  {clock}  「{t.text}」→ {heard}（{'・'.join(details)}）"


def format_dropped(voiced_sec: float, now: float) -> str:
    clock = time.strftime("%H:%M:%S", time.localtime(now))
    return f"  {clock}  （短い音 {voiced_sec:.2f} 秒 — 認識に回さず。言葉なら audio.min_speech_sec を下げる）"


def _cmd_listen(args: argparse.Namespace) -> int:
    if _import_pyaudio() is None:
        return 1
    from audio.recorder import _MIN_BUFFER_SECONDS, AudioThread
    from core.event_queue import make_audio_queue

    cfg = load_audio_config(args.config)
    device = args.device if args.device is not None else int(cfg.get("device_id", 0))
    model = args.model or cfg.get("whisper_model", "medium")
    stats = ListenStats()
    lock = threading.Lock()

    def on_transcript(t) -> None:
        with lock:
            if getattr(t, "no_speech", False):
                stats.no_speech += 1
                print(format_transcript(t), flush=True)
                return
            stats.heard += 1
            stats.actions += len(t.events)
            stats.unreadable += 0 if t.events else 1
            stats.infer_secs.append(t.infer_sec)
            print(format_transcript(t), flush=True)

    def on_dropped(voiced_sec: float) -> None:
        with lock:
            stats.dropped += 1
            print(format_dropped(voiced_sec, time.time()), flush=True)

    print(f"音声認識モデル（{model}）を読み込んでいます…（初回はダウンロードで数分かかります）", flush=True)
    started = time.time()
    stop = threading.Event()
    min_speech = (args.min_speech if args.min_speech is not None
                  else float(cfg.get("min_speech_sec", _MIN_BUFFER_SECONDS)))
    from audio.second_ear import load_live

    second_ear, ear_message = load_live(Path(__file__).resolve().parent.parent, cfg)   # 本番と同じ聞き直し
    print(ear_message, flush=True)
    thread = AudioThread(
        audio_queue=make_audio_queue(), device_id=device,
        sample_rate=int(cfg.get("sample_rate", 16000)), model_size=model,
        language=cfg.get("language", "ja"), stop_event=stop, on_transcript=on_transcript,
        min_speech_sec=min_speech, on_dropped=on_dropped,
        speech_rms=float(cfg.get("speech_rms", 300)),
        beam_size=int(cfg.get("beam_size", 5)),
        temperature_fallback=bool(cfg.get("temperature_fallback", False)),
        vad_threshold=(args.vad_threshold if args.vad_threshold is not None
                       else float(cfg.get("vad_threshold", 0.5))),
        cpu_threads=int(cfg.get("cpu_threads", 0) or 0),
        second_ear=second_ear,
    )
    if not thread.asr_ready:
        error = getattr(thread._transcriber, "load_error", None)  # noqa: SLF001
        print(f"音声認識モデルを読み込めませんでした: {error}")
        return 1
    print(f"読み込み完了（{time.time() - started:.0f} 秒）")
    thread.start()
    deadline = time.time() + 5
    while thread.health.get("state") == "starting" and time.time() < deadline:
        time.sleep(0.05)
    health = thread.health
    if health.get("state") != "running":
        print(f"マイク（番号 {device}）を開けませんでした: {health.get('error', health.get('state'))}")
        print("番号は `python tools/audio_check.py list` で確かめてください。")
        stop.set()
        thread.join(timeout=3)
        return 1
    print(f"マイク: 番号 {device}（{health.get('device_name') or '?'}）")
    print(f"{args.seconds:.0f} 秒聞き取ります。{_EXAMPLES}（Ctrl+C で終了）")
    end = time.time() + args.seconds
    try:
        while time.time() < end:
            time.sleep(0.2)   # Event.wait は Windows で Ctrl+C が効かないことがある
    except KeyboardInterrupt:
        pass
    stop.set()
    thread.join(timeout=10)
    deadline = time.time() + 60   # 認識が遅れていても、聞いた発話は全部表示してから終える
    while thread.backlog() > 0 and time.time() < deadline:
        time.sleep(0.1)
    print()
    print(stats.summary())
    return 0


# ――― bench ―――

def default_threads() -> int:
    """物理コア数の見積もり（論理コアの半分、4 以上）。faster-whisper の既定は 4。"""
    return max(4, (os.cpu_count() or 8) // 2)


def _read_jsonl(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


Utterance = tuple[str, Path, str]          # (音声のファイル名, パス, ライブの書き起こし)


def pick_utterances(log_dir: Path, session: Optional[str], count: int) -> Optional[tuple[str, list[Utterance]]]:
    """測るのに使う発話: 音声が保存されていて聞き取りに回した（声だった）発話を、新しいセッションから順に
    `count` 個たまるまで集め（`session` があればそのセッションだけ）、全体に散らばるように `count` 個選ぶ。
    (セッションの表示, 発話の列)。"""
    found = []
    for transcripts in sorted(log_dir.glob("*.transcripts.jsonl")):
        sid = transcripts.name[: -len(".transcripts.jsonl")]
        if session and not sid.startswith(session):
            continue
        audio_dir = log_dir / "audio" / sid
        rows = [(r["audio_file"], audio_dir / r["audio_file"], r["text"]) for r in _read_jsonl(transcripts)
                if r.get("audio_file") and not r.get("no_speech") and (r.get("text") or "").strip()
                and (audio_dir / r["audio_file"]).is_file()]
        if rows:
            found.append((transcripts.stat().st_mtime, sid, rows))
    if not found:
        return None
    found.sort(key=lambda x: -x[0])
    pool: list[Utterance] = []
    used: list[str] = []
    for _, sid, rows in found:
        pool.extend(rows)
        used.append(sid[:8])
        if len(pool) >= count:
            break
    step = max(1, len(pool) // max(1, count))
    label = used[0] + (f" ほか {len(used) - 1} セッション" if len(used) > 1 else "")
    return label, pool[::step][:count]


def read_pcm16(path: Path, rate: int = 16000) -> bytes:
    """保存した発話の WAV を、ライブの聞き取りと同じ PCM16 モノラル（16 kHz）のバイト列にする。"""
    with wave.open(str(path), "rb") as src:
        channels, width, src_rate = src.getnchannels(), src.getsampwidth(), src.getframerate()
        data = src.readframes(src.getnframes())
    if (channels, width, src_rate) == (1, 2, rate):
        return data
    import numpy as np

    audio = np.frombuffer(data, dtype=np.int16).astype(np.float32)
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    if src_rate != rate and len(audio):
        n = max(1, round(len(audio) * rate / src_rate))
        audio = np.interp(np.linspace(0, len(audio) - 1, n), np.arange(len(audio)), audio)
    return np.clip(audio, -32768, 32767).astype(np.int16).tobytes()


@dataclass
class BenchResult:
    threads: int
    beam: int
    rounds: list[list[float]] = field(default_factory=list)   # 周ごとの、発話ごとの時間
    texts: list[str] = field(default_factory=list)            # 1 周目の書き起こし
    stable: bool = True                                       # 周によって書き起こしが変わらなかった

    @property
    def times(self) -> list[float]:
        return [t for r in self.rounds for t in r]

    @property
    def median(self) -> float:
        return statistics.median(self.times)

    @property
    def round_medians(self) -> list[float]:
        return [statistics.median(r) for r in self.rounds]

    def per_utterance(self) -> list[float]:
        """発話ごとの時間（周の中央値）。"""
        return [statistics.median(ts) for ts in zip(*self.rounds)]


# 最初のモデルは、測る前にこの数の発話で空回しする（起動直後の数回は遅い = ウォームアップ。店舗 PC で最初に
# 測った設定だけが 0.6〜1.8 秒遅く出た）。2 つ目からは 1 発話。
_WARMUP_FIRST = 3


def run_bench(
    utterances: list[Utterance], settings: list[tuple[int, int]],
    make_transcriber: Callable[[int, int], object], clock: Callable[[], float] = time.perf_counter,
    log: Callable[[str], None] = print, rounds: int = 2,
) -> list[BenchResult]:
    """設定（スレッド数, ビーム幅）ごとにモデルを読み込み、同じ発話を聞き取って時間を測る。

    順番の影響（起動直後の遅さ・あとから重くなる）を打ち消すため、`rounds` 周のうち 2 周目は逆の順で測る。
    """
    audio = [read_pcm16(path) for _, path, _ in utterances]
    results = {setting: BenchResult(*setting) for setting in settings}
    broken: set[tuple[int, int]] = set()
    total = rounds * len(settings)
    step = 0
    warmup = _WARMUP_FIRST
    for r in range(rounds):
        order = settings if r % 2 == 0 else list(reversed(settings))
        for setting in order:
            step += 1
            if setting in broken:
                continue
            transcriber = make_transcriber(*setting)
            if not getattr(transcriber, "ready", True):
                log(f"  スレッド {setting[0]}・ビーム {setting[1]}: モデルを読み込めませんでした"
                    f"（{getattr(transcriber, 'load_error', '')}）")
                broken.add(setting)
                continue
            for pcm in audio[:warmup]:
                transcriber.recognize(pcm)          # 空回し（数えない）
            warmup = 1
            times, texts = [], []
            for pcm in audio:
                started = clock()
                result = transcriber.recognize(pcm)
                times.append(clock() - started)
                texts.append(result.text)
            res = results[setting]
            res.rounds.append(times)
            if not res.texts:
                res.texts = texts
            elif texts != res.texts:
                res.stable = False
            log(f"  [{step}/{total}] スレッド {setting[0]}・ビーム {setting[1]}（{r + 1} 周目）: "
                f"中央値 {statistics.median(times):.2f} 秒")
    return [results[s] for s in settings if results[s].rounds]


def summary_lines(results: list[BenchResult], base: BenchResult) -> list[str]:
    lines = []
    for r in results:
        rounds = "・".join(f"{i + 1} 周目 {m:.2f}" for i, m in enumerate(r.round_medians))
        agree = sum(a == b for a, b in zip(base.texts, r.texts))
        same = "（基準）" if r is base else f" — 書き起こしは基準と {agree}/{len(r.texts)} 同じ"
        unstable = "（周によって書き起こしが変わった）" if not r.stable else ""
        lines.append(f"  スレッド {r.threads}・ビーム {r.beam}: 1 発話 中央値 {r.median:.2f} 秒（{rounds}）・"
                     f"最大 {max(r.times):.2f} 秒{same}{unstable}")
    return lines


def base_result(results: list[BenchResult], current_threads: int, current_beam: int) -> BenchResult:
    return next((r for r in results if (r.threads, r.beam) == (current_threads, current_beam)), results[0])


def recommend(results: list[BenchResult], current_threads: int, current_beam: int) -> list[str]:
    """いまの設定より 1 割以上速く（どの周でも速く）、書き起こしが同じ設定があれば、config に入れるコマンドを出す。"""
    if not results:
        return []
    base = base_result(results, current_threads, current_beam)
    lines = [f"いまの設定（スレッド {base.threads}・ビーム {base.beam}）: 1 発話 中央値 {base.median:.2f} 秒"]
    faster = [
        r for r in results
        if r is not base and r.stable and r.texts == base.texts and r.median <= 0.9 * base.median
        and all(a < b for a, b in zip(r.round_medians, base.round_medians))
    ]
    if not faster:
        lines.append("書き起こしを変えずにはっきり速くなる設定はありませんでした（いまのままで）。")
        return lines
    best = min(faster, key=lambda r: r.median)
    lines.append(f"いちばん速いのはスレッド {best.threads}・ビーム {best.beam}（中央値 {best.median:.2f} 秒 = "
                 f"いまの {best.median / base.median * 100:.0f}%、書き起こしは同じ）。config に入れるなら:")
    tool = _ROOT / "tools" / "set_config.py"
    if best.threads != base.threads:
        lines.append(f"  {sys.executable} {tool} audio.cpu_threads {best.threads}")
    if best.beam != base.beam:
        lines.append(f"  {sys.executable} {tool} audio.beam_size {best.beam}")
    return lines


# まとめて聞き取るときに発話の間に入れる無音（秒）
_JOIN_GAP_SEC = 1.0


def _action_list(text: str) -> list[tuple[str, int]]:
    from audio.recognizer import parse_actions

    return [(e.action, e.amount) for e in parse_actions(text)]


def run_joined(
    utterances: list[Utterance], base: BenchResult, group: int, make_transcriber: Callable[[int, int], object],
    clock: Callable[[], float] = time.perf_counter, rate: int = 16000,
) -> Optional[dict]:
    """待っている発話を `group` 個ずつ（間に 1 秒の無音を入れて）まとめて 1 回で聞き取ったときの時間と、
    読んだアクションが 1 つずつ聞き取ったときと同じかを測る（Whisper は短い発話でも 30 秒の窓を処理するので、
    まとめれば 1 回ぶんで済む）。"""
    if group < 2 or len(utterances) < group:
        return None
    transcriber = make_transcriber(base.threads, base.beam)
    if not getattr(transcriber, "ready", True):
        return None
    audio = [read_pcm16(path) for _, path, _ in utterances]
    transcriber.recognize(audio[0])
    gap = b"\x00\x00" * int(rate * _JOIN_GAP_SEC)
    single = base.per_utterance()
    joined_times, single_times, same, groups = [], [], 0, 0
    examples = []
    for i in range(0, len(audio) - group + 1, group):
        idx = list(range(i, i + group))
        started = clock()
        text = transcriber.recognize(gap.join(audio[j] for j in idx)).text
        joined_times.append(clock() - started)
        single_times.append(sum(single[j] for j in idx))
        expected = [a for j in idx for a in _action_list(base.texts[j])]
        got = _action_list(text)
        groups += 1
        if got == expected:
            same += 1
        elif len(examples) < 3:
            examples.append((" ／ ".join(base.texts[j] for j in idx), text))
    return {
        "group": group, "groups": groups, "same": same, "examples": examples,
        "joined_per_utterance": statistics.median(joined_times) / group,
        "single_per_utterance": statistics.median(single_times) / group,
    }


def joined_lines(joined: Optional[dict]) -> list[str]:
    if not joined:
        return []
    lines = [
        f"まとめて {joined['group']} 発話ずつ聞き取ると: 1 発話あたり {joined['joined_per_utterance']:.2f} 秒"
        f"（1 つずつだと {joined['single_per_utterance']:.2f} 秒）・読んだアクションが 1 つずつと同じ"
        f" {joined['same']}/{joined['groups']} 組",
    ]
    for single, joined_text in joined["examples"]:
        lines.append(f"  違った組: 1 つずつ「{single}」 → まとめて「{joined_text}」")
    return lines


def _parse_ints(raw: str) -> list[int]:
    return [int(x) for x in raw.replace(" ", "").split(",") if x]


def _make_whisper(model: str, language: str, vad_threshold: float) -> Callable[[int, int], object]:
    from audio.recognizer import WhisperTranscriber

    def make(threads: int, beam: int):
        return WhisperTranscriber(model_size=model, language=language, beam_size=beam,
                                  vad_threshold=vad_threshold, cpu_threads=threads)

    return make


def _cmd_bench(args: argparse.Namespace, make_transcriber=None) -> int:
    cfg = load_audio_config(args.config)
    log_dir = Path(args.log_dir) if args.log_dir else _ROOT / "logs"
    picked = pick_utterances(log_dir, args.session, args.count)
    if picked is None:
        print(f"保存した発話の音声がありません（{log_dir / 'audio'}）。config の audio.save_audio を true にして"
              "記録したセッションが要ります。")
        return 1
    label, utterances = picked
    current_threads = int(cfg.get("cpu_threads", 0) or 0) or 4
    current_beam = int(cfg.get("beam_size", 5))
    threads = _parse_ints(args.threads) if args.threads else sorted({current_threads, 4, default_threads()})
    beams = _parse_ints(args.beam) if args.beam else [current_beam]
    settings = [(t, b) for b in beams for t in threads]
    if (current_threads, current_beam) not in settings:
        settings.insert(0, (current_threads, current_beam))       # いまの設定を基準として必ず測る
    model = args.model or cfg.get("whisper_model", "medium")
    print(f"計測: セッション {label} の発話 {len(utterances)} 個・モデル {model}・この PC の論理コア {os.cpu_count()}")
    print(f"（本番のロガーを閉じてから。設定ごとにモデルを読み込み、順番を入れ替えて {args.rounds} 周測ります）")
    make = make_transcriber or _make_whisper(model, cfg.get("language", "ja"), float(cfg.get("vad_threshold", 0.5)))
    results = run_bench(utterances, settings, make, rounds=args.rounds)
    if not results:
        return 1
    base = base_result(results, current_threads, current_beam)
    print()
    for line in summary_lines(results, base):
        print(line)
    print()
    for line in recommend(results, current_threads, current_beam):
        print(line)
    if args.joined:
        joined = run_joined(utterances, base, args.joined, make)
        if joined:
            print()
            for line in joined_lines(joined):
                print(line)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="マイクと音声認識の確認（音声テストの前に）")
    parser.add_argument("--config", default=None,
                        help="config パス（既定: config.json があればそれ、無ければ config_default.json）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="録音できるデバイスの一覧（config の audio.device_id に印）")
    p_list.set_defaults(func=_cmd_list)

    p_level = sub.add_parser("level", help="入力レベルを表示（発話として拾えるか）")
    p_level.add_argument("--device", type=int, default=None, help="デバイス番号（既定: config）")
    p_level.add_argument("--seconds", type=float, default=15.0, help="表示する秒数（既定 15）")
    p_level.set_defaults(func=_cmd_level)

    p_listen = sub.add_parser("listen", help="本番と同じ経路で聞き取り、認識結果を表示")
    p_listen.add_argument("--device", type=int, default=None, help="デバイス番号（既定: config）")
    p_listen.add_argument("--seconds", type=float, default=120.0, help="聞き取る秒数（既定 120）")
    p_listen.add_argument("--model", default=None,
                          help="音声認識モデル（既定: config の audio.whisper_model。例 small / medium）")
    p_listen.add_argument("--min-speech", type=float, default=None,
                          help="認識に回す最短の有音秒数（既定: config の audio.min_speech_sec、無ければ 0.15）")
    p_listen.add_argument("--vad-threshold", type=float, default=None,
                          help="声か（VAD）の閾値。0 で使わない（既定: config の audio.vad_threshold、無ければ 0.5）")
    p_listen.set_defaults(func=_cmd_listen)

    p_bench = sub.add_parser("bench", help="保存した発話で、Whisper のスレッド数ごとの聞き取りの時間を測る")
    p_bench.add_argument("--threads", default=None,
                         help="比べるスレッド数（カンマ区切り。既定: 4 と この PC の物理コア数の見積もり）")
    p_bench.add_argument("--beam", default=None, help="比べるビーム幅（カンマ区切り。既定: config の audio.beam_size）")
    p_bench.add_argument("--count", type=int, default=10, help="使う発話の数（既定 10。足りなければ前のセッションからも）")
    p_bench.add_argument("--session", default=None, help="セッション ID（先頭でよい。既定: 新しいものから）")
    p_bench.add_argument("--model", default=None, help="音声認識モデル（既定: config の audio.whisper_model）")
    p_bench.add_argument("--log-dir", default=None, help="logs フォルダ（既定: アプリの logs/）")
    p_bench.add_argument("--rounds", type=int, default=2, help="何周測るか（2 周目は逆の順。既定 2）")
    p_bench.add_argument("--joined", type=int, default=0,
                         help="発話を N 個ずつまとめて 1 回で聞き取る試しもする（いまの設定で。例 3）")
    p_bench.set_defaults(func=_cmd_bench)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
