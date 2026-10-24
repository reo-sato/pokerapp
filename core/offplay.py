"""core/offplay.py

聞き流した（ハンドの外の）発話の書き起こし `<sid>.offplay.jsonl` を、推定器の入力の書き起こしに足す（2026-10-24）。

ライブがハンドを早く閉じると次の配布まで聞き取りを止める（ADR-0063）。`audio.save_audio` ならそのあいだの発話の音声を
`logs/audio/<sid>/off_<話し始めの ms>.wav` に残し、`tools/offplay_audio.py` がライブと同じ経路で書き起こす。
推定器の内容の指紋（`integration.estimator.CONTENT_FILES`）に入る = 入力の読み方が変われば指紋も変わる。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

OFFPLAY_SUFFIX = ".offplay.jsonl"
OFFPLAY_PREFIX = "off_"


def offplay_start(name: str) -> Optional[float]:
    """`off_<ms>.wav` → 話し始めの時刻（秒）。形が違えば None。"""
    if not (name.startswith(OFFPLAY_PREFIX) and name.endswith(".wav")):
        return None
    digits = name[len(OFFPLAY_PREFIX):-len(".wav")]
    return int(digits) / 1000.0 if digits.isdigit() else None


def merge_offplay(transcripts: list[dict], offplay: list[dict]) -> list[dict]:
    """書き起こしの記録に、聞き流した発話の書き起こしを足す（話し始めの時刻の順。同じ時刻はライブの行を残す）。"""
    if not offplay:
        return transcripts
    seen = {r.get("utterance_start_ts") for r in transcripts}
    extra = [r for r in offplay if r.get("utterance_start_ts") is not None and r["utterance_start_ts"] not in seen]
    return sorted(transcripts + extra, key=lambda r: (r.get("utterance_start_ts") is None,
                                                      r.get("utterance_start_ts") or 0.0))


def read_rows(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue                    # 電源断で途中の行が壊れても残りを使う
    return rows
