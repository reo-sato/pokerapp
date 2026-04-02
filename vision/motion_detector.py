from __future__ import annotations

import logging
from dataclasses import dataclass, field

import cv2
import numpy as np

logger = logging.getLogger(__name__)

# 300フレームごとに背景モデルをリセット（環境光変化への対策）
_BG_RESET_INTERVAL = 300
# デフォルト連続検出フレーム数（FR-08: 誤検出防止）
_DEFAULT_CONSECUTIVE = 3
# イベント発火後のクールダウン（同席の連続発火防止） @ 20fps ≒ 0.75秒
_COOLDOWN_FRAMES = 15


@dataclass
class ROI:
    """席ごとのベットエリア関心領域。"""

    seat: int
    x: int
    y: int
    w: int
    h: int


class MotionDetector:
    """MOG2 背景差分を使って各席 ROI のチップ動作を検出する。

    FR-06: ROI ごとにフレーム間差分を計算してチップ前進を検出する。
    FR-08: 連続フレームで動きが閾値を超えた場合のみイベントを発火する。
    """

    def __init__(
        self,
        motion_threshold: int = 2000,
        consecutive_threshold: int = _DEFAULT_CONSECUTIVE,
    ) -> None:
        """
        Args:
            motion_threshold: ROI 内で動きありと判定する差分ピクセル数の閾値。
            consecutive_threshold: イベント発火に必要な連続検出フレーム数。
        """
        self._motion_threshold = motion_threshold
        self._consecutive_threshold = consecutive_threshold
        self._mog2 = self._make_mog2()
        self._frame_count: int = 0
        # 席ごとの連続検出フレーム数
        self._consecutive: dict[int, int] = {}
        # 席ごとのクールダウン残フレーム数
        self._cooldown: dict[int, int] = {}

    @staticmethod
    def _make_mog2() -> cv2.BackgroundSubtractorMOG2:
        return cv2.createBackgroundSubtractorMOG2(
            history=500, varThreshold=16, detectShadows=False
        )

    def process_frame(self, frame: np.ndarray, rois: list[ROI]) -> list[int]:
        """フレームを処理し、動作検出された席番号のリストを返す。

        Args:
            frame: BGR フレーム (H×W×3 uint8)。
            rois: 検出対象の ROI リスト。

        Returns:
            このフレームで初めてイベント発火した席番号のリスト。
            連続閾値に達していない席は含まれない（FR-08）。
        """
        self._frame_count += 1

        # 300フレームごとに背景モデルをリセット（照明変化対策）
        if self._frame_count % _BG_RESET_INTERVAL == 0:
            logger.debug("Resetting MOG2 background model at frame %d", self._frame_count)
            self._mog2 = self._make_mog2()

        # フレーム全体に MOG2 を適用してフォアグラウンドマスクを取得
        fg_mask = self._mog2.apply(frame)

        triggered: list[int] = []
        for roi in rois:
            seat = roi.seat

            # クールダウン中はスキップ（同席の連続発火を防ぐ）
            if self._cooldown.get(seat, 0) > 0:
                self._cooldown[seat] -= 1
                continue

            # ROI 領域を切り出してフォアグラウンドピクセル数を計算
            roi_mask = fg_mask[roi.y : roi.y + roi.h, roi.x : roi.x + roi.w]
            diff_pixels = int(cv2.countNonZero(roi_mask))

            if diff_pixels > self._motion_threshold:
                self._consecutive[seat] = self._consecutive.get(seat, 0) + 1
                if self._consecutive[seat] >= self._consecutive_threshold:
                    triggered.append(seat)
                    self._consecutive[seat] = 0
                    self._cooldown[seat] = _COOLDOWN_FRAMES
                    logger.info(
                        "Motion detected: seat=%d diff_pixels=%d", seat, diff_pixels
                    )
            else:
                # 動きが収まったらカウントをリセット
                self._consecutive[seat] = 0

        return triggered

    @property
    def frame_count(self) -> int:
        return self._frame_count
