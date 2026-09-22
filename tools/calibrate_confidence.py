#!/usr/bin/env python3
"""tools/calibrate_confidence.py

Phase R5/F2 — 派生 confidence（`integration.engine.derive_confidence`, ADR-0009 §6 / D3）の
**重み較正ハーネス**（ADR-0033）。

golden fixtures（`tests/fixtures/reconstruction/`）の archetype と境界グリッドに対して confidence
サーフェスを評価し、較正が満たすべき **プロパティ**（順序単調性・閾値分離・合法性ゲート等）を検証する。
重みを変更したら本ハーネスを回して回帰を確認する（同じチェックを `tests/test_confidence_calibration.py`
が CI で実行する）。

使い方:
  python tools/calibrate_confidence.py            # サーフェス表示 + プロパティ検証（違反で exit 1）
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from integration.engine import (  # noqa: E402
    MISSING_WHISPER_CONF,
    REVIEW_THRESHOLD,
    SYNTH_FOLD_CONFIDENCE,
    derive_confidence,
)

# audio は rules-aware 経路では当該アクションにつき常に存在する（ADR-0009 §6）。
_AUDIO_ONLY = dict(
    apply_ok=True, audio_agree=True,
    rfid_present=False, rfid_agree=False, camera_present=False, camera_agree=False,
)


def _conf(**overrides: object) -> float:
    return derive_confidence(**{**_AUDIO_ONLY, **overrides})  # type: ignore[arg-type]


def check_properties() -> list[tuple[str, bool, str]]:
    """較正プロパティを検証し (name, ok, detail) の列を返す（ADR-0033 の較正スペック）。"""
    results: list[tuple[str, bool, str]] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, bool(ok), detail))

    # P1: 全グリッドで 0..1 に収まる。
    grid = [
        _conf(whisper_conf=w, apply_ok=ok, rfid_present=rp, rfid_agree=rp and ra,
              camera_present=cp, camera_agree=cp and ca)
        for w in (0.0, 0.3, 0.5, 0.7, 0.9, 1.0)
        for ok in (True, False)
        for rp in (False, True) for ra in (False, True)
        for cp in (False, True) for ca in (False, True)
    ]
    add("P1 bounds[0,1]", all(0.0 <= c <= 1.0 for c in grid),
        f"min={min(grid):.3f} max={max(grid):.3f}")

    # P2: audio-only は whisper_conf に対し単調増加。
    seq = [_conf(whisper_conf=w) for w in (0.0, 0.3, 0.5, 0.7, 0.9, 1.0)]
    add("P2 whisper monotonic", all(a < b for a, b in zip(seq, seq[1:])),
        " < ".join(f"{c:.3f}" for c in seq))

    # P3: ソース合意の質で順序づく（all agree, legal, whisper=0.9）。
    w = 0.9
    audio = _conf(whisper_conf=w)
    audio_cam = _conf(whisper_conf=w, camera_present=True, camera_agree=True)
    rfid_audio = _conf(whisper_conf=w, rfid_present=True, rfid_agree=True)
    all3 = _conf(whisper_conf=w, rfid_present=True, rfid_agree=True,
                 camera_present=True, camera_agree=True)
    add("P3 source ordering", audio < audio_cam < rfid_audio < all3,
        f"audio={audio:.3f} < audio+cam={audio_cam:.3f} < rfid+audio={rfid_audio:.3f} < all3={all3:.3f}")

    # P4: RFID corroboration > camera corroboration（base rfid > camera）。
    add("P4 rfid>camera corroboration", rfid_audio > audio_cam,
        f"rfid+audio={rfid_audio:.3f} > audio+cam={audio_cam:.3f}")

    # P5: 同席ソースの不一致は一致より低い。
    agree = _conf(whisper_conf=w, rfid_present=True, rfid_agree=True)
    disagree = _conf(whisper_conf=w, rfid_present=True, rfid_agree=False)
    add("P5 disagreement penalty", disagree < agree,
        f"disagree={disagree:.3f} < agree={agree:.3f}")

    # P6: 非合法（apply_ok=False）は合法より低く、高合意でも閾値未満に落ちる。
    legal = _conf(whisper_conf=w, rfid_present=True, rfid_agree=True)
    illegal = _conf(whisper_conf=w, rfid_present=True, rfid_agree=True, apply_ok=False)
    add("P6 illegal penalty < threshold", illegal < legal and illegal < REVIEW_THRESHOLD,
        f"illegal={illegal:.3f} < legal={legal:.3f}, threshold={REVIEW_THRESHOLD}")

    # P7: 閾値分離。良好 audio-only(whisper>=0.6) は非 review、低品質(<=0.5) は review。
    good = _conf(whisper_conf=0.6)
    poor = _conf(whisper_conf=0.5)
    add("P7 threshold separates audio quality",
        good >= REVIEW_THRESHOLD > poor,
        f"good(0.6)={good:.3f} >= {REVIEW_THRESHOLD} > poor(0.5)={poor:.3f}")

    # P8: 合成 silent-fold は常に閾値未満（sensor 観測なしの推定 ⇒ 必ず review）。
    add("P8 synth-fold below threshold", SYNTH_FOLD_CONFIDENCE < REVIEW_THRESHOLD,
        f"synth={SYNTH_FOLD_CONFIDENCE} < threshold={REVIEW_THRESHOLD}")

    # P9: 欠測 whisper（None → MISSING_WHISPER_CONF 補完, ADR-0033 追記 B3）は満点より
    # 厳密に低く、audio-only では review 側に落ちる（「情報が無いほど上がる」逆転の禁止）。
    missing = _conf(whisper_conf=MISSING_WHISPER_CONF)
    add("P9 missing-conf conservative",
        missing < _conf(whisper_conf=1.0) and missing < REVIEW_THRESHOLD,
        f"missing({MISSING_WHISPER_CONF})={missing:.3f} < full={_conf(whisper_conf=1.0):.3f}, "
        f"threshold={REVIEW_THRESHOLD}")

    return results


def report() -> str:
    lines = ["confidence calibration surface (ADR-0033)", "=" * 44,
             f"REVIEW_THRESHOLD={REVIEW_THRESHOLD}  SYNTH_FOLD={SYNTH_FOLD_CONFIDENCE}",
             "", "audio-only by whisper_conf:"]
    for w in (0.3, 0.5, 0.6, 0.7, 0.9, 1.0):
        c = _conf(whisper_conf=w)
        lines.append(f"  whisper={w:>3}: {c:.3f}  {'review' if c < REVIEW_THRESHOLD else 'ok'}")
    lines += ["", "properties:"]
    for name, ok, detail in check_properties():
        lines.append(f"  [{'PASS' if ok else 'FAIL'}] {name}  ({detail})")
    return "\n".join(lines)


def main() -> int:
    print(report())
    failed = [n for n, ok, _ in check_properties() if not ok]
    if failed:
        print(f"\nCALIBRATION FAILED: {len(failed)} property violation(s): {failed}")
        return 1
    print("\nall calibration properties hold.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
