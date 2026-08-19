"""tests/test_confidence_calibration.py

Phase R5/F2 (ADR-0033): 派生 confidence の重み較正をプロパティで回帰ロックする。

`tools/calibrate_confidence.py` の較正スペック（順序単調性・閾値分離・合法性ゲート等）を CI で固定し、
重み（_CONF_W_A / _CONF_W_Q / _CONF_BASE / _CONF_L_PENALTY / REVIEW_THRESHOLD）の意図しない drift を
検知する。golden fixtures の archetype 値とも整合させる。
"""
from __future__ import annotations

import pytest

from integration.engine import REVIEW_THRESHOLD, derive_confidence
from tools.calibrate_confidence import check_properties

_AUDIO_ONLY = dict(
    apply_ok=True, audio_agree=True,
    rfid_present=False, rfid_agree=False, camera_present=False, camera_agree=False,
)


def _conf(**overrides):
    return derive_confidence(**{**_AUDIO_ONLY, **overrides})


@pytest.mark.parametrize("name,ok,detail", check_properties())
def test_calibration_property_holds(name: str, ok: bool, detail: str):
    """較正プロパティ P1〜P9 が成立する（ADR-0033 + B3 追記）。"""
    assert ok, f"{name} violated: {detail}"


def test_all_properties_pass_overall():
    failed = [n for n, ok, _ in check_properties() if not ok]
    assert not failed, f"calibration property violations: {failed}"


def test_golden_archetype_values_stable():
    """golden fixtures に対応する代表 confidence が較正後も安定（drift 監視）。"""
    # call-amount-from-state: audio-only, whisper≈0.7 → 0.448（閾値超え=非 review）。
    assert _conf(whisper_conf=0.7) == pytest.approx(0.448, abs=0.001)
    assert _conf(whisper_conf=0.7) >= REVIEW_THRESHOLD
    # out-of-turn-rfid call: rfid+audio agree, whisper≈0.9 → 0.888。
    rfid_audio = _conf(whisper_conf=0.9, rfid_present=True, rfid_agree=True)
    assert rfid_audio == pytest.approx(0.897, abs=0.01)


def test_review_threshold_is_between_synth_and_good_audio():
    """REVIEW_THRESHOLD は synth-fold(0.3) と良好 audio-only の間に位置する（分離点）。"""
    from integration.engine import SYNTH_FOLD_CONFIDENCE
    assert SYNTH_FOLD_CONFIDENCE < REVIEW_THRESHOLD <= _conf(whisper_conf=0.6)


def test_p9_missing_conf_is_conservative():
    """B3 (ADR-0033 追記): whisper 欠測（None）は満点補完しない。

    欠測既定 MISSING_WHISPER_CONF は audio-only で REVIEW_THRESHOLD 未満 = 欠測 audio
    単独のアクションは必ず review 側に倒れる。"""
    from integration.engine import MISSING_WHISPER_CONF
    missing = _conf(whisper_conf=MISSING_WHISPER_CONF)
    assert missing < _conf(whisper_conf=1.0)
    assert missing < REVIEW_THRESHOLD
