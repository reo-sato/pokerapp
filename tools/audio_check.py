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

使用例:
  python tools/audio_check.py list
  python tools/audio_check.py level --seconds 15
  python tools/audio_check.py listen --seconds 120
  python tools/audio_check.py listen --device 1 --model small
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

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
    infer_secs: list[float] = field(default_factory=list)

    def summary(self) -> str:
        dropped = f"・短すぎて認識しなかった音 {self.dropped} 件" if self.dropped else ""
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
    heard = "雑音として無視" if getattr(t, "noise", False) else describe_events(t.events)
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
    thread = AudioThread(
        audio_queue=make_audio_queue(), device_id=device,
        sample_rate=int(cfg.get("sample_rate", 16000)), model_size=model,
        language=cfg.get("language", "ja"), stop_event=stop, on_transcript=on_transcript,
        min_speech_sec=min_speech, on_dropped=on_dropped,
        speech_rms=float(cfg.get("speech_rms", 300)),
        beam_size=int(cfg.get("beam_size", 5)),
        temperature_fallback=bool(cfg.get("temperature_fallback", False)),
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
    p_listen.set_defaults(func=_cmd_listen)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
