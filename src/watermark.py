"""去水印 — 移除 Gemini 可見浮水印(V2 profile 反向 alpha 混合)

Gemini 在 3.5 世代換了浮水印 profile:同一顆四角星,但 alpha 剖面整個換掉
(舊版 α_max≈0.51,新版≈0.33/0.37),位置公式也跟著改。用舊 α 圖去扣新浮水印
會**扣過頭**,在圖上留下深色星形鬼影——那就是 2026-07 使用者看到的「黑星」;
偵測不到的則整顆白星留著。兩種症狀同一個根因:α 圖版本錯了。

做法(2026-07-30 以 .11 實圖 39 張 + 2 張 live 校準):

- 幾何:星心固定在 (w−120s, h−120s),s = 2 若短邊 ≥1536 否則 1。
  25 張高信心樣本中 16 張最佳偏移正好是 (0,0)、22 張在 ±2 內 → 不搜位置。
  尺寸固定 48s(實測:讓移除後星形輪廓能量最低的尺寸,27 張全數落在 48s)。
- 模型:obs = k·α·255 + (1−k·α)·bg(logo 是白色 —— V2 α 圖是單通道灰階,
  代表拍在黑底上的浮水印本身無彩度)。k 由最小平方解;**模型若正確 k 應 ≈ 1**,
  實測 27 張原始圖中位數 0.96、R² 0.85~0.93,20 張落在 0.90~1.10。
- 閘門:主判準 k∈[0.70,1.35] 且 R²≥0.45 且 rms≥2;背景估不準時(星壓在材質
  交界,inpaint 估背景會歪)改用**輪廓能量救援**——比較移除前後星形輪廓上的
  邊緣能量,比值 ≤0.85 且 k 在寬鬆範圍內就放行(這條不需要估背景)。
- 移除:orig = (obs − α·255)/(1−α)。α 已知且正確,不需要 inpaint 填補,
  所以不會有「填錯色把圖塗花」的風險。

實測(27 張原始 / 12 張已被舊管線扣壞 / 117 個無浮水印角落當負控制):
召回 25/27、無浮水印處誤判 0/117、誤觸已損傷圖 0/12、清過的圖再跑 0 次觸發。

α 圖出處:allenk/GeminiWatermarkTool(MIT)的 V2 profile 資產,
見 src/assets/gemini_v2_alpha_{36,96}.png。

對外介面維持不變:remove_watermark(input_path, output_path=None) -> str
"""
import logging
from pathlib import Path

import cv2
import numpy as np

logger = logging.getLogger(__name__)

_ASSETS = Path(__file__).parent / "assets"

_MARGIN = 120           # 星心離右/下邊界的距離(s=1)
_SIZE = 48              # 星的邊長(s=1);實測 27 張原始圖,讓「移除後星形輪廓
                        # 能量最低」的尺寸每一張都是 48s,與 allenk 的半尺寸
                        # profile 一致。用 R² 挑尺寸會選到 36/44 → 扣不乾淨、
                        # 暗部還會被扣成黑斑,所以這裡寫死不掃描。

# 主判準
_K_MAIN = (0.70, 1.35)  # 擬合出的 alpha 縮放;模型正確時應 ≈1
_R2_MIN = 0.45
_RMS_MIN = 2.0          # 星區平均偏離量,太小代表根本沒東西可除
# 輪廓能量救援(背景估不準時用,不依賴 inpaint 估背景)
_K_RESCUE = (0.50, 1.60)
_CONTOUR_MAX = 0.85     # 移除後/移除前 的輪廓邊緣能量比
_EDGE_MIN = 5.0         # 輪廓帶原本的邊緣能量下限(全平坦區比值不穩)

_alpha_src: tuple[np.ndarray, np.ndarray] | None = None
_alpha_cache: dict[int, np.ndarray] = {}


def _alpha(size: int) -> np.ndarray:
    """V2 星形 alpha 模板,縮放到指定尺寸。"""
    global _alpha_src
    if _alpha_src is None:
        small = cv2.imread(str(_ASSETS / "gemini_v2_alpha_36.png"), cv2.IMREAD_GRAYSCALE)
        large = cv2.imread(str(_ASSETS / "gemini_v2_alpha_96.png"), cv2.IMREAD_GRAYSCALE)
        if small is None or large is None:
            raise FileNotFoundError(f"缺少 V2 alpha 資產:{_ASSETS}")
        _alpha_src = (small.astype(np.float32) / 255.0, large.astype(np.float32) / 255.0)
    if size not in _alpha_cache:
        src = _alpha_src[1] if size >= 64 else _alpha_src[0]
        interp = cv2.INTER_AREA if size < src.shape[0] else cv2.INTER_CUBIC
        _alpha_cache[size] = cv2.resize(src, (size, size), interpolation=interp)
    return _alpha_cache[size]


def _imread(path: str):
    # 用 imdecode 而非 imread,避免非 ASCII 路徑在某些平台讀不到
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
        raise ValueError(f"imencode 失敗:{ext}")
    buf.tofile(path)


def _restore(roi: np.ndarray, a: np.ndarray) -> np.ndarray:
    """反向 alpha 混合:orig = (obs − α·255)/(1−α)。"""
    af = a[:, :, None]
    return np.clip((roi.astype(np.float32) - af * 255.0) / np.clip(1.0 - af, 0.05, 1.0),
                   0, 255).astype(np.uint8)


def _fit(img: np.ndarray, cx: int, cy: int, size: int) -> dict | None:
    """在星心 (cx,cy) 解 obs = k·α·255 + (1−k·α)·bg,回傳 k / R² / rms。"""
    x, y = cx - size // 2, cy - size // 2
    h, w = img.shape[:2]
    if x < 0 or y < 0 or x + size > w or y + size > h:
        return None
    a = _alpha(size)
    patch = img[y:y + size, x:x + size]
    roi = patch.astype(np.float32)
    mask = (a > 0.02).astype(np.uint8)
    bg = cv2.inpaint(patch, cv2.dilate(mask, np.ones((5, 5), np.uint8)),
                     5, cv2.INPAINT_TELEA).astype(np.float32)
    m = mask.astype(bool)
    u = (a[:, :, None] * (255.0 - bg))[m]
    d = (roi - bg)[m]
    denom = float((u * u).sum())
    if denom < 1e-6:
        return None
    k = float((d * u).sum() / denom)
    ss_tot = float((d ** 2).sum()) + 1e-6
    return dict(k=k, r2=1.0 - float(((d - k * u) ** 2).sum()) / ss_tot,
                rms=float(np.sqrt((d ** 2).mean())), xy=(x, y, size))


def _contour_energy(img: np.ndarray, cx: int, cy: int, size: int) -> dict | None:
    """星形輪廓上的邊緣能量:移除前 eb、移除後/前的比值 ratio。

    浮水印邊緣會在輪廓上留下能量,正確移除後顯著下降;沒有浮水印時
    「移除」等於憑空刻一顆星進去,能量反而上升。不需要估背景,所以在
    材質交界上仍可靠。
    """
    x, y = cx - size // 2, cy - size // 2
    h, w = img.shape[:2]
    if x < 0 or y < 0 or x + size > w or y + size > h:
        return None
    a = _alpha(size)
    roi = img[y:y + size, x:x + size]
    rec = _restore(roi, a)
    ga = np.abs(cv2.Sobel(a, cv2.CV_32F, 1, 0, 3)) + np.abs(cv2.Sobel(a, cv2.CV_32F, 0, 1, 3))
    band = ga > np.percentile(ga, 88)

    def energy(im):
        g = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY).astype(np.float32)
        e = np.abs(cv2.Sobel(g, cv2.CV_32F, 1, 0, 3)) + np.abs(cv2.Sobel(g, cv2.CV_32F, 0, 1, 3))
        return float(e[band].mean())

    eb = energy(roi)
    return dict(ratio=energy(rec) / (eb + 1e-6), eb=eb)


def _detect(img: np.ndarray) -> dict | None:
    """在 canonical 星心掃描尺寸,回傳過閘門的最佳候選(或 None)。"""
    h, w = img.shape[:2]
    s = 2 if min(w, h) >= 1536 else 1
    if min(w, h) < 2 * _MARGIN * s:  # 圖太小,幾何先驗不成立
        return None
    cx, cy = w - _MARGIN * s, h - _MARGIN * s
    size = _SIZE * s
    best = _fit(img, cx, cy, size)
    if best is None:
        logger.info("去水印:canonical 區取不到樣本,保留原圖")
        return None

    x, y, _ = best["xy"]
    if (_K_MAIN[0] <= best["k"] <= _K_MAIN[1]
            and best["r2"] >= _R2_MIN and best["rms"] >= _RMS_MIN):
        logger.info("去水印:k=%.2f R²=%.2f rms=%.1f region=(%d,%d,%d)",
                    best["k"], best["r2"], best["rms"], x, y, size)
        return best

    # 主判準沒過:可能是星壓在材質交界,inpaint 估背景歪掉 → 改看輪廓能量
    # (這條不需要估背景)。k 仍須落在寬鬆的物理範圍內才准救援。
    ce = _contour_energy(img, cx, cy, size)
    if (ce and _K_RESCUE[0] <= best["k"] <= _K_RESCUE[1]
            and ce["ratio"] <= _CONTOUR_MAX and ce["eb"] >= _EDGE_MIN):
        logger.info("去水印(輪廓救援):k=%.2f R²=%.2f 輪廓比=%.2f region=(%d,%d,%d)",
                    best["k"], best["r2"], ce["ratio"], x, y, size)
        return best

    logger.info(
        "去水印:未過閘門(k=%.2f R²=%.2f rms=%.1f%s region=(%d,%d,%d)),保留原圖",
        best["k"], best["r2"], best["rms"],
        f" 輪廓比={ce['ratio']:.2f}" if ce else "", x, y, size,
    )
    return None


def remove_watermark(input_path: str, output_path: str | None = None) -> str:
    """移除圖片的 Gemini 可見浮水印(偵測不到則原圖不動)。

    Args:
        input_path: 輸入圖片路徑
        output_path: 輸出路徑(預設覆蓋原檔)

    Returns:
        輸出路徑(失敗或無浮水印時回傳未改動的原檔路徑)
    """
    if output_path is None:
        output_path = input_path

    try:
        img = _imread(input_path)
        if img is None:
            logger.warning("去水印:讀不到圖 %s", input_path)
            return input_path

        f = _detect(img)
        if f is None:  # 不動的原因已在 _detect 內帶數據記錄
            return input_path

        x, y, size = f["xy"]
        out = img.copy()
        out[y:y + size, x:x + size] = _restore(img[y:y + size, x:x + size], _alpha(size))
        _imwrite(output_path, out)
        logger.info("去水印完成:%s", output_path)
        return output_path

    except Exception as e:
        logger.warning("去水印失敗:%s", e)
        return input_path
