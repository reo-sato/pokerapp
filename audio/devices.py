"""audio/devices.py — 録音するマイクを名前で選ぶ

PortAudio のデバイスの番号は、起動のたびに Windows が返す並びで決まる。Bluetooth のマイク・ヘッドセットを
つなぐ・切ると並びが変わり、config の番号（`audio.device_id`）が別のマイクを指すことがある（店舗 2026-10-01）。
`audio.device_name` に名前（の一部）を書けば、起動のたびにその名前のマイクの番号を探して開く。優先順に「|」で
区切って複数書ける（例: `Wireless Mic Rx|DJI Mic Mini 2` = 受信機があればそれ、無ければ Bluetooth で直接）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional


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


def parse_device_names(value: Any) -> list[str]:
    """config `audio.device_name` の値 → 名前（の一部）の優先順のリスト。「|」「,」「、」で区切るかリストで書く。
    空なら []（番号 `audio.device_id` で選ぶ）。"""
    if isinstance(value, (list, tuple)):
        parts = [str(v) for v in value]
    elif isinstance(value, str):
        parts = re.split(r"[|,、]", value)
    else:
        return []
    return [p.strip() for p in parts if p.strip()]


def _field(device: Any, key: str) -> Any:
    return device.get(key) if isinstance(device, dict) else getattr(device, key)


def choose_device(devices: list, names: list[str]) -> Optional[Any]:
    """名前の優先順に、その名前を含むマイク（大文字・小文字は区別しない）。同じマイクが方式（MME・DirectSound・
    WASAPI・WDM-KS）ごとに並ぶので、16 kHz で開けるもの・若い番号（Windows では MME が先に並ぶ）を選ぶ。
    どの名前も見つからなければ None（別のマイクで録らない）。"""
    for name in names:
        key = name.casefold()
        hits = [d for d in devices if key in str(_field(d, "name")).casefold()]
        if hits:
            return min(hits, key=lambda d: (not _field(d, "rate_ok"), int(_field(d, "index"))))
    return None


def chosen_per_name(devices: list, names: list[str]) -> list[Any]:
    """名前ごとに選んだマイク（見つかった名前だけ、優先順。同じマイクは 1 回）。読み上げ集で 2 本を選ぶとき。"""
    out: list[Any] = []
    for name in names:
        device = choose_device(devices, [name])
        if device is not None and all(_field(device, "index") != _field(d, "index") for d in out):
            out.append(device)
    return out


def describe_names(names: list[str]) -> str:
    return " / ".join(f"「{n}」" for n in names)


# RDP の音声が接続元に回っていると、PC のマイクは MME などから見えず WDM-KS 方式だけに出る。WDM-KS では
# Bluetooth のマイクを開けない（店舗 2026-09-30: DJI Mic Mini 2 が「[Errno -9999] Unanticipated host error」）。
# 音声の再生先は接続元の端末の設定（保存した接続ごと）で、PC の側からは変えられない
RDP_AUDIO_FIX = ("接続元の設定で音声の再生を「リモート PC で再生」（Windows の「リモート デスクトップ接続」は"
                 "「リモート コンピューターで再生する」）にしてつなぎ直し")
RDP_AUDIO_HINT = ("マイクが WDM-KS 方式でしか見えていません。リモートデスクトップの音声が接続元の端末に回っていると"
                  "こうなり、WDM-KS では Bluetooth のマイクを開けません。" + RDP_AUDIO_FIX + "、マイクを探し直してください。")
WDM_KS_OPEN_HINT = "WDM-KS の番号は開けないことがあります。MME の番号を選んでください。"


def _api_of(device: Any) -> str:
    return str(device.get("api", "") if isinstance(device, dict) else getattr(device, "api", ""))


def only_wdm_ks(devices: list) -> bool:
    """録音できるデバイスが WDM-KS 方式だけか（= RDP の音声が接続元に回っている）。"""
    return bool(devices) and all("WDM-KS" in _api_of(d) for d in devices)


def open_error_hint(device: Any) -> str:
    """マイクを開けなかったときに添える一言（WDM-KS の番号なら MME を勧める）。"""
    return WDM_KS_OPEN_HINT if "WDM-KS" in _api_of(device) else ""
