from __future__ import annotations

import logging
import threading
import time
from typing import Optional

import cv2
import numpy as np

from core.event_queue import EventQueue
from core.events import CameraEvent
from vision.motion_detector import MotionDetector, ROI

logger = logging.getLogger(__name__)


def parse_roi_config(roi_config: dict) -> list[ROI]:
    """config.json の camera.roi セクションを ROI リストに変換する。

    期待するキー形式: "seat_1", "seat_2", ...
    値: {"x": int, "y": int, "w": int, "h": int}
    """
    rois: list[ROI] = []
    for key, val in roi_config.items():
        try:
            seat = int(key.split("_")[-1])
            rois.append(ROI(seat=seat, x=val["x"], y=val["y"], w=val["w"], h=val["h"]))
        except (ValueError, KeyError) as e:
            logger.warning("Invalid ROI config for %r: %s", key, e)
    return sorted(rois, key=lambda r: r.seat)


class CameraThread(threading.Thread):
    """カメラから映像を取得し、動体検出した席番号を camera_queue に送出するデーモンスレッド。

    FR-07: 動体検出により「アクションあり」と判定した席番号をイベントキューに送出する。
    FR-09: showdown_mode=True のとき EasyOCR によるカードスキャンを試みる（補助機能）。
    """

    def __init__(
        self,
        camera_queue: EventQueue,
        device_id: int | str = 0,
        roi_config: Optional[dict] = None,
        fps: int = 20,
        motion_threshold: int = 2000,
        stop_event: Optional[threading.Event] = None,
    ) -> None:
        super().__init__(daemon=True, name="CameraThread")
        self._camera_queue = camera_queue
        self._device_id = device_id
        self._rois = parse_roi_config(roi_config or {})
        self._fps = fps
        self._stop_event = stop_event or threading.Event()
        self._detector = MotionDetector(motion_threshold=motion_threshold)
        self._showdown_mode = False
        # GUI デバッグ表示用の最新フレーム（Phase 4 で利用）
        self._latest_frame: Optional[np.ndarray] = None
        self._frame_lock = threading.Lock()

    def set_showdown_mode(self, enabled: bool) -> None:
        """ショーダウン時に OCR スキャンを有効化する（FR-09）。"""
        self._showdown_mode = enabled
        logger.debug("Showdown mode: %s", enabled)

    def update_rois(self, roi_config: dict) -> None:
        """ROI を動的に更新する（キャリブレーション後に呼ぶ）。"""
        self._rois = parse_roi_config(roi_config)
        logger.info("ROIs updated: %d seats configured", len(self._rois))

    def get_latest_frame(self) -> Optional[np.ndarray]:
        """デバッグ表示用の最新フレームを返す（スレッドセーフ）。"""
        with self._frame_lock:
            return self._latest_frame.copy() if self._latest_frame is not None else None

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        cap = cv2.VideoCapture(self._device_id)
        if not cap.isOpened():
            logger.error("Cannot open camera: device_id=%s", self._device_id)
            return

        cap.set(cv2.CAP_PROP_FPS, self._fps)
        frame_interval = 1.0 / self._fps
        logger.info(
            "CameraThread started (device_id=%s, fps=%d, rois=%d)",
            self._device_id, self._fps, len(self._rois),
        )

        try:
            while not self._stop_event.is_set():
                t0 = time.monotonic()

                ret, frame = cap.read()
                if not ret:
                    logger.warning("Failed to read frame, retrying...")
                    time.sleep(0.1)
                    continue

                # 最新フレームを保持（GUI デバッグ用）
                with self._frame_lock:
                    self._latest_frame = frame

                # ROI が設定されている場合のみ動体検出を実行
                if self._rois:
                    triggered = self._detector.process_frame(frame, self._rois)
                    for seat in triggered:
                        self._camera_queue.put(CameraEvent(
                            seat=seat,
                            timestamp=time.time(),
                            # showdown_mode のときのみフレームを添付（OCR 用）
                            frame=frame.copy() if self._showdown_mode else None,
                        ))

                # FR-09: ショーダウン時に OCR を試みる（補助機能・精度保証なし）
                if self._showdown_mode:
                    self._try_ocr(frame)

                # 目標フレームレートに合わせてスリープ
                elapsed = time.monotonic() - t0
                sleep_time = frame_interval - elapsed
                if sleep_time > 0:
                    time.sleep(sleep_time)

        finally:
            cap.release()
            logger.info("CameraThread stopped")

    def _try_ocr(self, frame: np.ndarray) -> None:
        """ショーダウン時にボードカードの OCR を試みる（FR-09 補助機能）。

        EasyOCR が未インストールの場合は何もしない。
        精度は保証しない。検出結果はデバッグログにのみ出力する。
        """
        try:
            import easyocr  # type: ignore[import]
        except ImportError:
            return
        try:
            reader = easyocr.Reader(["en"], verbose=False)
            results = reader.readtext(frame, detail=0)
            if results:
                logger.debug("OCR (showdown): %s", results)
        except Exception:
            logger.debug("OCR failed (non-critical)", exc_info=True)
