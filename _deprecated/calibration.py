from __future__ import annotations

import logging
from typing import Optional

import cv2

from core.config import load_config, save_config

logger = logging.getLogger(__name__)


def run_calibration(device_id: int = 0) -> bool:
    """インタラクティブ ROI キャリブレーション（FR-05）。

    カメラプレビューを表示し、マウスドラッグで各席のベットエリアを矩形指定する。
    指定した ROI を config.json の camera.roi セクションに保存する。

    操作方法:
        マウスドラッグ : ROI 矩形を描画
        r             : 最後に登録した ROI を取り消す
        s             : 保存して終了
        q / ESC       : 保存せずに終了

    Returns:
        保存して終了した場合 True、キャンセルの場合 False。
    """
    cap = cv2.VideoCapture(device_id)
    if not cap.isOpened():
        logger.error("Cannot open camera: device_id=%d", device_id)
        print(f"カメラを開けません (device_id={device_id})。")
        return False

    rois: list[dict] = []      # [{"x", "y", "w", "h"}, ...] 席番号はインデックス+1
    drawing = False
    start_x = start_y = 0
    preview_rect: Optional[tuple[int, int, int, int]] = None  # ドラッグ中プレビュー

    def on_mouse(event: int, x: int, y: int, flags: int, param: object) -> None:
        nonlocal drawing, start_x, start_y, preview_rect
        if event == cv2.EVENT_LBUTTONDOWN:
            drawing = True
            start_x, start_y = x, y
            preview_rect = None
        elif event == cv2.EVENT_MOUSEMOVE and drawing:
            preview_rect = (
                min(x, start_x), min(y, start_y),
                abs(x - start_x), abs(y - start_y),
            )
        elif event == cv2.EVENT_LBUTTONUP:
            drawing = False
            if preview_rect and preview_rect[2] > 10 and preview_rect[3] > 10:
                px, py, pw, ph = preview_rect
                rois.append({"x": px, "y": py, "w": pw, "h": ph})
                print(f"席{len(rois)} ROI 登録: x={px} y={py} w={pw} h={ph}")
            preview_rect = None

    cv2.namedWindow("ROI Calibration", cv2.WINDOW_NORMAL)
    cv2.setMouseCallback("ROI Calibration", on_mouse)

    print("=== ROI キャリブレーション ===")
    print("各席のベットエリアをマウスドラッグで指定してください。")
    print("  [r]: 最後の ROI を取り消し  [s]: 保存して終了  [q/ESC]: キャンセル")

    saved = False
    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                continue
            display = frame.copy()

            # 登録済み ROI をオーバーレイ表示
            for i, roi in enumerate(rois):
                x, y, w, h = roi["x"], roi["y"], roi["w"], roi["h"]
                cv2.rectangle(display, (x, y), (x + w, y + h), (0, 255, 0), 2)
                cv2.putText(
                    display, f"Seat {i + 1}",
                    (x, max(y - 5, 10)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2,
                )

            # ドラッグ中の矩形をプレビュー表示
            if preview_rect:
                px, py, pw, ph = preview_rect
                cv2.rectangle(display, (px, py), (px + pw, py + ph), (255, 100, 0), 2)

            # ヘルプテキスト
            cv2.putText(
                display,
                f"Seats: {len(rois)}  [r]=undo  [s]=save  [q]=quit",
                (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2,
            )

            cv2.imshow("ROI Calibration", display)
            key = cv2.waitKey(1) & 0xFF

            if key == ord("r"):
                if rois:
                    removed = rois.pop()
                    print(f"席{len(rois) + 1} ROI 取り消し: {removed}")
                else:
                    print("取り消す ROI がありません。")
            elif key == ord("s"):
                _save_rois(rois)
                print(f"{len(rois)} 席の ROI を config.json に保存しました。")
                saved = True
                break
            elif key in (ord("q"), 27):  # q または ESC
                print("キャリブレーションをキャンセルしました。")
                break
    finally:
        cap.release()
        cv2.destroyAllWindows()

    return saved


def _save_rois(rois: list[dict]) -> None:
    """ROI リストを config.json の camera.roi セクションに書き込む。"""
    config = load_config()
    config.setdefault("camera", {})["roi"] = {
        f"seat_{i + 1}": roi for i, roi in enumerate(rois)
    }
    save_config(config)
    logger.info("Saved %d ROIs to config.json", len(rois))
