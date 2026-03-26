"""去水印 — Reverse Alpha Blending 移除 Gemini 可見水印

基於 https://github.com/VimalMollyn/Gemini-Watermark-Remover-Python
原始演算法：https://github.com/journey-ad/gemini-watermark-remover

公式：original = (watermarked - alpha * logo) / (1 - alpha)
"""
import logging
from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)

# alpha map 檔案位置
_ASSETS_DIR = Path(__file__).parent / "assets"

# 快取
_ALPHA_MAPS: dict[int, np.ndarray] = {}


def _load_alpha_map(size: int) -> np.ndarray:
    """載入 alpha map（從水印擷取圖計算）"""
    if size in _ALPHA_MAPS:
        return _ALPHA_MAPS[size]

    bg_path = _ASSETS_DIR / f"bg_{size}.png"
    if not bg_path.exists():
        raise FileNotFoundError(f"Alpha map 不存在：{bg_path}")

    bg_img = Image.open(bg_path).convert("RGB")
    bg_array = np.array(bg_img, dtype=np.float32)
    # 取 RGB 通道最大值，正規化到 [0, 1]
    alpha_map = np.max(bg_array, axis=2) / 255.0
    _ALPHA_MAPS[size] = alpha_map
    return alpha_map


def _detect_config(width: int, height: int) -> dict:
    """根據圖片尺寸決定水印大小和邊距

    Gemini 規則：
    - 寬高都 > 1024：96x96 logo，64px 邊距
    - 否則：48x48 logo，32px 邊距
    """
    if width > 1024 and height > 1024:
        return {"logo_size": 96, "margin": 64}
    return {"logo_size": 48, "margin": 32}


def remove_watermark(input_path: str, output_path: str | None = None) -> str:
    """移除圖片右下角 Gemini 水印

    Args:
        input_path: 輸入圖片路徑
        output_path: 輸出路徑（預設覆蓋原檔）

    Returns:
        輸出路徑（失敗時回傳原檔路徑）
    """
    if output_path is None:
        output_path = input_path

    try:
        img = Image.open(input_path).convert("RGB")
        width, height = img.size
        config = _detect_config(width, height)
        logo_size = config["logo_size"]
        margin = config["margin"]

        # 水印位置（右下角）
        x = width - margin - logo_size
        y = height - margin - logo_size

        if x < 0 or y < 0:
            logger.info("圖片太小，跳過去水印")
            return input_path

        logger.info(
            "去水印：%dx%d, logo=%dx%d, pos=(%d,%d)",
            width, height, logo_size, logo_size, x, y,
        )

        # 載入 alpha map
        alpha_map = _load_alpha_map(logo_size)

        # Reverse Alpha Blending
        img_array = np.array(img, dtype=np.float32)
        ALPHA_THRESHOLD = 0.002
        MAX_ALPHA = 0.99
        LOGO_VALUE = 255.0

        for row in range(logo_size):
            for col in range(logo_size):
                alpha = alpha_map[row, col]
                if alpha < ALPHA_THRESHOLD:
                    continue
                alpha = min(alpha, MAX_ALPHA)
                one_minus_alpha = 1.0 - alpha
                for c in range(3):
                    watermarked = img_array[y + row, x + col, c]
                    original = (watermarked - alpha * LOGO_VALUE) / one_minus_alpha
                    img_array[y + row, x + col, c] = max(0, min(255, round(original)))

        result = Image.fromarray(img_array.astype(np.uint8), "RGB")

        # 儲存（保持原格式品質）
        ext = Path(input_path).suffix.lower()
        if ext in (".jpg", ".jpeg"):
            result.save(output_path, quality=95)
        else:
            result.save(output_path)

        logger.info("去水印完成：%s", output_path)
        return output_path

    except Exception as e:
        logger.warning("去水印失敗：%s", e)
        return input_path
