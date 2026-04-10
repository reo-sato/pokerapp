"""vision モジュールの単体テスト。

CameraThread・calibration は実カメラ依存のため対象外。
MotionDetector と parse_roi_config は合成フレームで検証する。
"""
from __future__ import annotations

import numpy as np
import pytest

from vision.motion_detector import MotionDetector, ROI
from vision.camera import parse_roi_config


# ――― ヘルパー ―――

def _black_frame(h: int = 480, w: int = 640) -> np.ndarray:
    """全黒の BGR フレームを返す（動きなし）。"""
    return np.zeros((h, w, 3), dtype=np.uint8)


def _white_roi_frame(roi: ROI, h: int = 480, w: int = 640) -> np.ndarray:
    """ROI 内だけ白い BGR フレームを返す（動きあり）。"""
    frame = _black_frame(h, w)
    frame[roi.y : roi.y + roi.h, roi.x : roi.x + roi.w] = 255
    return frame


ROI1 = ROI(seat=1, x=100, y=100, w=100, h=80)


# ――― MotionDetector ―――

class TestMotionDetector:
    def test_no_motion_returns_empty(self):
        """静止フレームを連続供給してもイベントは発火しない。"""
        det = MotionDetector(motion_threshold=100, consecutive_threshold=3)
        for _ in range(10):
            result = det.process_frame(_black_frame(), [ROI1])
        assert result == []

    def test_motion_fires_after_consecutive_frames(self):
        """ROI 内に動きが consecutive_threshold フレーム連続すればイベントが発火する。"""
        det = MotionDetector(motion_threshold=100, consecutive_threshold=3)

        # MOG2 に背景を十分学習させる（50 フレーム以上が推奨）
        for _ in range(50):
            det.process_frame(_black_frame(), [ROI1])

        # 動きのあるフレームを consecutive_threshold - 1 フレーム供給 → まだ発火しない
        active = _white_roi_frame(ROI1)
        for _ in range(2):
            result = det.process_frame(active, [ROI1])
        assert result == []

        # consecutive_threshold フレーム目で発火
        result = det.process_frame(active, [ROI1])
        assert 1 in result

    def test_cooldown_prevents_refiring(self):
        """イベント発火直後のクールダウン中は同席のイベントが発火しない。"""
        det = MotionDetector(motion_threshold=100, consecutive_threshold=3)

        for _ in range(50):
            det.process_frame(_black_frame(), [ROI1])

        active = _white_roi_frame(ROI1)
        # 3 フレーム分動かしてイベントを発火させる
        for _ in range(3):
            det.process_frame(active, [ROI1])

        # クールダウン中は発火しない
        fired_after_cooldown = []
        for _ in range(5):
            fired_after_cooldown.extend(det.process_frame(active, [ROI1]))
        assert fired_after_cooldown == []

    def test_consecutive_resets_on_calm(self):
        """動きが止まると連続カウントがリセットされる。"""
        det = MotionDetector(motion_threshold=100, consecutive_threshold=3)

        for _ in range(50):
            det.process_frame(_black_frame(), [ROI1])

        active = _white_roi_frame(ROI1)

        # 2 フレーム動かす（閾値未満）
        det.process_frame(active, [ROI1])
        det.process_frame(active, [ROI1])

        # 静止フレームでリセット
        det.process_frame(_black_frame(), [ROI1])

        # もう一度 2 フレームだけ動かしてもイベントは発火しない
        result_1 = det.process_frame(active, [ROI1])
        result_2 = det.process_frame(active, [ROI1])
        assert result_1 == [] and result_2 == []

    def test_bg_reset_does_not_crash(self):
        """300 フレーム超でも背景モデルリセットがクラッシュしない。"""
        det = MotionDetector(motion_threshold=100, consecutive_threshold=3)
        for _ in range(305):
            det.process_frame(_black_frame(), [ROI1])
        assert det.frame_count == 305

    def test_empty_rois_returns_empty(self):
        """ROI リストが空なら常に空リストを返す。"""
        det = MotionDetector()
        result = det.process_frame(_black_frame(), [])
        assert result == []


# ――― parse_roi_config ―――

class TestParseRoiConfig:
    def test_valid_config(self):
        cfg = {
            "seat_1": {"x": 10, "y": 20, "w": 100, "h": 80},
            "seat_2": {"x": 200, "y": 20, "w": 100, "h": 80},
        }
        rois = parse_roi_config(cfg)
        assert len(rois) == 2
        seats = [r.seat for r in rois]
        assert seats == [1, 2]  # 昇順ソートされている

    def test_invalid_key_is_skipped(self):
        """不正なキーはスキップされる。"""
        cfg = {
            "seat_1": {"x": 10, "y": 20, "w": 100, "h": 80},
            "bad_key": {"x": 0, "y": 0, "w": 50, "h": 50},
        }
        rois = parse_roi_config(cfg)
        # "bad_key".split("_")[-1] = "key" → int("key") で ValueError → スキップ
        assert len(rois) == 1
        assert rois[0].seat == 1

    def test_missing_field_is_skipped(self):
        """必須フィールドが欠けているエントリはスキップされる。"""
        cfg = {
            "seat_1": {"x": 10, "y": 20, "w": 100},  # h が欠けている
            "seat_2": {"x": 200, "y": 20, "w": 100, "h": 80},
        }
        rois = parse_roi_config(cfg)
        assert len(rois) == 1
        assert rois[0].seat == 2

    def test_empty_config(self):
        assert parse_roi_config({}) == []
