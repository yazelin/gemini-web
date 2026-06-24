"""去水印 — 移除 Gemini 可見浮水印（白色 sparkle）

做法跟舊版一樣是 **reverse alpha blending**（數學還原、銳利、不模糊），
差別只在「定位點」改成動態偵測：

- 舊版用 _detect_config 寫死右下角 48/96 logo + 32/64 邊距 → 新版 Gemini
  (Gemini 3 / nano-banana-pro) 各種寬幅比例下浮水印不在那裡 → 去到錯位、留痕。
- 新版用 remove-ai-watermarks 的 NCC 偵測拿到實際位置/大小，
  還原仍走純反 alpha（remove_watermark_custom，不做 inpaint，所以不會糊）。

對外介面維持不變：remove_watermark(input_path, output_path=None) -> str
"""
import logging
from pathlib import Path

import cv2
import numpy as np
from remove_ai_watermarks.gemini_engine import GeminiEngine

logger = logging.getLogger(__name__)

# 偵測信心門檻：低於此值視為「沒有浮水印」，原圖不動。
# remove-ai-watermarks 內部誤判線 _SPARKLE_FP_CONF=0.65；真浮水印通常 >=0.74，
# 誤判（已清過/無浮水印的圖）約 0.40，取 0.6 落在兩者之間留安全邊際。
_MIN_CONFIDENCE = 0.6

_engine: GeminiEngine | None = None


def _get_engine() -> GeminiEngine:
    global _engine
    if _engine is None:
        _engine = GeminiEngine()
    return _engine


def _imread(path: str):
    # 用 imdecode 而非 imread，避免非 ASCII 路徑在某些平台讀不到
    data = np.fromfile(path, dtype=np.uint8)
    if data.size == 0:
        return None
    return cv2.imdecode(data, cv2.IMREAD_COLOR)


def _imwrite(path: str, img) -> None:
    ext = Path(path).suffix.lower() or ".png"
    params = []
    if ext in (".jpg", ".jpeg"):
        params = [cv2.IMWRITE_JPEG_QUALITY, 95]
    elif ext == ".webp":
        params = [cv2.IMWRITE_WEBP_QUALITY, 95]
    ok, buf = cv2.imencode(ext, img, params)
    if not ok:
        raise ValueError(f"imencode 失敗：{ext}")
    buf.tofile(path)


def remove_watermark(input_path: str, output_path: str | None = None) -> str:
    """移除圖片的 Gemini 可見浮水印（偵測不到則原圖不動）。

    Args:
        input_path: 輸入圖片路徑
        output_path: 輸出路徑（預設覆蓋原檔）

    Returns:
        輸出路徑（失敗或無浮水印時回傳未改動的原檔路徑）
    """
    if output_path is None:
        output_path = input_path

    try:
        img = _imread(input_path)
        if img is None:
            logger.warning("去水印：讀不到圖 %s", input_path)
            return input_path

        engine = _get_engine()
        det = engine.detect_watermark(img)

        if not (det.detected and det.confidence >= _MIN_CONFIDENCE):
            logger.info("去水印：未偵測到浮水印（conf=%.3f），保留原圖", det.confidence)
            return input_path

        logger.info("去水印：conf=%.3f, region=%s", det.confidence, det.region)
        # 純反 alpha（remove_watermark_custom），不做 inpaint → 銳利、不模糊
        result = engine.remove_watermark_custom(img, det.region)
        _imwrite(output_path, result)
        logger.info("去水印完成：%s", output_path)
        return output_path

    except Exception as e:
        logger.warning("去水印失敗：%s", e)
        return input_path
