"""去水印 — 移除 Gemini 可見浮水印(色彩自適應四角星)

2026-07 起 Gemini 的可見浮水印改版:同一顆四角星,但從「白色 alpha 疊加」
變成「取樣周圍顏色的半透明疊加」——白、灰、黑、橘、洋紅都出現過,
不透明度也拉高(核心近全遮)。remove-ai-watermarks 的白色模板 NCC 因此
大量失效(conf 掉到 0.2~0.57 → 全數保留原圖),少數勉強過門檻的再用
「白色假設」反 alpha 又會過度扣色,留下黑色星形殘塊。

新做法(自家實作,套件只借 α 星形模板):

- 位置先驗:星心固定在 (w−120s, h−120s),s=1|2(2026-07-30 以 .11 實圖
  39 張校準,無一例外);只在 canonical ±8s px、模板尺寸 40~60s 內搜尋,
  永不碰圖片其他區域。
- 偵測=擬合:對每個候選 (x,y,size) 解線性模型
      obs = k·α·c + (1−k·α)·orig
  (c=RGB 常數色、k=不透明度縮放、α=星形模板、orig 用 Telea inpaint 估計),
  在高斯模糊域算 R²(星是平滑訊號,網點/筆觸紋理是噪音)。
- 閘門:R² ≥ 0.70、k∈[0.4,2.8]、c 各通道 ∈[−40,296]、效果量 ≥ 5、
  |NCC| ≥ 0.3(可見度:趨近 0 代表星色與背景幾乎同色,人眼看不到,不動)、
  硬邊界比例 ≤ 0.12(背景估不準就不動,見下)。
- 還原:反混合 orig = (obs − k·α·c)/(1−k·α);不透明核心 (1−k·α < 0.35)
  資訊已被蓋掉,改用 inpaint 背景填。

已知品質限制(2026-07-30 高倍實測):星的**輪廓線常有殘留**——借來的 α 模板
形狀與新版浮水印的實際 α 剖面對不上,邊緣扣不乾淨。內部已清掉,平坦背景上
仍看得出一圈細線。要根治得重估 α 剖面或改成「遮罩 + 學習式 inpaint」。

對外介面維持不變:remove_watermark(input_path, output_path=None) -> str
"""
import logging
from pathlib import Path

import cv2
import numpy as np

logger = logging.getLogger(__name__)

# 偵測閘門(2026-07-30 以 39 張 .11 實圖校準:召回 37/39,
# 兩張漏的星本身幾乎不可見;無星貼圖 0 誤傷)
_R2_MIN = 0.70          # 模糊域擬合解釋力
_K_RANGE = (0.4, 2.8)   # 不透明度縮放(k·α_max ≈ 0.2~1.4)
_C_RANGE = (-40.0, 296.0)  # 擬合出的浮水印色,允許輕微出界
_EFFECT_MIN = 5.0       # 星區平均偏離量(太小=沒東西可除)
_NCC_MIN = 0.3          # 可見度:|NCC| 低於此值人眼看不到,不動
_CORE_KEEP = 0.35       # 1−k·α 低於此值視為不透明核心 → inpaint 填
_BLUR_SIGMA = 2.5

# 背景可靠度:星區底下若有硬邊界(髮際、綠幕/角色交界),Telea inpaint 估出的
# 背景會把鄰邊顏色拉進來 → 反混合+核心填補會在圖上留灰黑污漬。那比留著浮水印
# 糟得多,所以「背景估不準就不動」。(2026-07-30 live 實例:貼圖白衣被塗灰)
_EDGE_GRAD = 40.0       # Sobel 梯度視為硬邊界的門檻
_EDGE_FRAC_MAX = 0.12   # 星罩內硬邊界像素佔比上限

_MARGIN = 120           # 星心離右/下邊界距離(s=1)
_JITTER = 8             # canonical 位置容差(px, s=1)
_SIZE_LO, _SIZE_HI = 40, 60  # 模板尺寸範圍(s=1)

_alpha_maps: dict[int, np.ndarray] | None = None


def _get_alpha_maps() -> dict[int, np.ndarray]:
    """星形 α 模板(借自 remove-ai-watermarks;星的形狀沒變,變的是上色)。"""
    global _alpha_maps
    if _alpha_maps is None:
        from remove_ai_watermarks.gemini_engine import GeminiEngine

        engine = GeminiEngine()
        _alpha_maps = {
            size: engine.get_interpolated_alpha(size).astype(np.float32)
            for size in range(32, 129, 4)
        }
    return _alpha_maps


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


def _fit_region(img: np.ndarray, x: int, y: int, size: int) -> dict | None:
    """在 (x,y,size) 擬合 obs = k·α·c + (1−k·α)·orig,回傳擬合結果與還原圖塊。"""
    h, w = img.shape[:2]
    if x < 0 or y < 0 or x + size > w or y + size > h:
        return None
    tpl = _get_alpha_maps()[size]
    roi = img[y:y + size, x:x + size].astype(np.float32)
    mask8 = (tpl > 0.02).astype(np.uint8)
    big = cv2.dilate(mask8, np.ones((5, 5), np.uint8))
    bg = cv2.inpaint(img[y:y + size, x:x + size], big, 5, cv2.INPAINT_TELEA).astype(np.float32)

    # 模糊域擬合:星是平滑訊號,壓掉網點/筆觸高頻噪音
    roi_s = cv2.GaussianBlur(roi, (0, 0), _BLUR_SIGMA)
    bg_s = cv2.GaussianBlur(bg, (0, 0), _BLUR_SIGMA)
    m = mask8.astype(bool)
    al = tpl[m]
    n = al.size
    # 未知數 [aB,aG,aR,k],a_ch = k·c_ch:d_ch = a_ch·α − k·α·bg_ch
    A = np.zeros((n * 3, 4), np.float32)
    rhs = np.empty(n * 3, np.float32)
    d = roi_s[m] - bg_s[m]
    for ch in range(3):
        A[ch * n:(ch + 1) * n, ch] = al
        A[ch * n:(ch + 1) * n, 3] = -al * bg_s[m][:, ch]
        rhs[ch * n:(ch + 1) * n] = d[:, ch]
    sol, *_ = np.linalg.lstsq(A, rhs, rcond=None)
    a_bgr = sol[:3]
    k = float(sol[3])
    ss_tot = float(np.sum(rhs ** 2)) + 1e-6
    r2 = 1.0 - float(np.sum((rhs - A @ sol) ** 2)) / ss_tot
    effect = float(np.mean(np.abs(rhs)))
    c_bgr = a_bgr / (k if abs(k) > 1e-6 else 1e-6)

    # 背景可靠度:星罩底下的硬邊界比例(高 → inpaint 估背景會拉錯色)
    bg_gray = cv2.cvtColor(bg_s.astype(np.uint8), cv2.COLOR_BGR2GRAY).astype(np.float32)
    grad = np.abs(cv2.Sobel(bg_gray, cv2.CV_32F, 1, 0, 3)) + np.abs(
        cv2.Sobel(bg_gray, cv2.CV_32F, 0, 1, 3)
    )
    edge_frac = float((grad[m] > _EDGE_GRAD).mean())

    # 可見度(極性無關 NCC):B/G/R 各算一次取最大——灰階會漏掉
    # 亮度差小但色度差明顯的星(白星在粉膚、灰星在橘底)
    t0 = tpl - tpl.mean()
    t_norm = float(np.sqrt((t0 ** 2).sum()))
    ncc = 0.0
    for ch in range(3):
        g0 = roi_s[:, :, ch] - roi_s[:, :, ch].mean()
        v = float((t0 * g0).sum() / (t_norm * np.sqrt((g0 ** 2).sum()) + 1e-6))
        if abs(v) > abs(ncc):
            ncc = v

    # 還原(原解析度域):反混合 + 不透明核心用背景填
    af = tpl[:, :, None]
    keep = 1.0 - k * af
    rec = (roi - af * a_bgr[None, None, :]) / np.clip(keep, 0.15, 1.0)
    rec = np.where(keep < _CORE_KEEP, bg, rec)
    rec = np.clip(rec, 0, 255).astype(np.uint8)
    return dict(r2=r2, k=k, c=c_bgr, effect=effect, ncc=ncc, edge_frac=edge_frac,
                rec=rec, xy=(x, y, size))


def _sane(f: dict | None) -> bool:
    """模型合理性(選候選用):參數在物理可能範圍內。"""
    return (
        f is not None
        and _K_RANGE[0] <= f["k"] <= _K_RANGE[1]
        and f["effect"] >= _EFFECT_MIN
        and f["edge_frac"] <= _EDGE_FRAC_MAX
        and all(_C_RANGE[0] <= v <= _C_RANGE[1] for v in f["c"])
    )


def _detect(img: np.ndarray) -> dict | None:
    """在 canonical 位置附近找浮水印。

    先選再驗:候選點只用模型合理性挑出唯一贏家,R²/可見度閘門只驗贏家。
    (若對每個候選都套全部閘門,405 個點裡總有紋理湊巧全過 → 無星圖誤傷;
    真星永遠是視窗內的最強訊號,所以贏家非星即紋理,驗贏家就夠。)
    """
    h, w = img.shape[:2]
    s = 2 if min(w, h) >= 1536 else 1
    if min(w, h) < 2 * _MARGIN:  # 圖太小,canonical 先驗不成立
        return None
    cx, cy = w - _MARGIN * s, h - _MARGIN * s
    best = None
    for size in _get_alpha_maps():
        if not (_SIZE_LO * s <= size <= _SIZE_HI * s):
            continue
        half = size // 2
        for dx in range(-_JITTER * s, _JITTER * s + 1, 2 * s):
            for dy in range(-_JITTER * s, _JITTER * s + 1, 2 * s):
                f = _fit_region(img, cx + dx - half, cy + dy - half, size)
                if not _sane(f):
                    continue
                key = f["r2"] * min(f["effect"], 12.0)
                if best is None or key > best["r2"] * min(best["effect"], 12.0):
                    best = f
    if best is None:
        logger.info("去水印:canonical 區無合理候選,保留原圖")
        return None
    if best["r2"] < _R2_MIN or abs(best["ncc"]) < _NCC_MIN:
        x, y, size = best["xy"]
        c = best["c"]
        logger.info(
            "去水印:候選未過閘門(r2=%.2f k=%.2f ncc=%.2f eff=%.1f edge=%.2f "
            "c=(%.0f,%.0f,%.0f) region=(%d,%d,%d)),保留原圖",
            best["r2"], best["k"], best["ncc"], best["effect"], best["edge_frac"],
            c[2], c[1], c[0], x, y, size,
        )
        return None
    return best


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
        if f is None:  # keep 原因已在 _detect 內帶數據記錄
            return input_path

        x, y, size = f["xy"]
        c = f["c"]
        logger.info(
            "去水印:r2=%.2f k=%.2f ncc=%.2f edge=%.2f c=(%.0f,%.0f,%.0f) region=(%d,%d,%d)",
            f["r2"], f["k"], f["ncc"], f["edge_frac"], c[2], c[1], c[0], x, y, size,
        )
        out = img.copy()
        out[y:y + size, x:x + size] = f["rec"]
        _imwrite(output_path, out)
        logger.info("去水印完成:%s", output_path)
        return output_path

    except Exception as e:
        logger.warning("去水印失敗:%s", e)
        return input_path
