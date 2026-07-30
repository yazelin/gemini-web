"""去水印模組測試

- 乾淨圖(canonical 區沒有星)原檔不動,不誤刮
- 合成的 V2 浮水印(白色 logo、α 取自我們自己的資產)偵測得到且移除得乾淨
- 合成在「非 canonical 位置」的星不該被碰(幾何先驗的邊界)
- 讀不到 / 壞檔時 fallback 回原路徑、不丟例外
"""
import numpy as np
from pathlib import Path
from PIL import Image

from src.watermark import _alpha, remove_watermark


def _base_image(w=1408, h=768, seed=7):
    """有紋理的底圖(不是純漸層,避免測試過於樂觀)。"""
    rng = np.random.default_rng(seed)
    arr = np.zeros((h, w, 3), dtype=np.float32)
    arr[:, :, 0] = np.linspace(60, 190, w, dtype=np.float32)[None, :]
    arr[:, :, 1] = 120
    arr[:, :, 2] = np.linspace(180, 70, h, dtype=np.float32)[:, None]
    arr += rng.normal(0, 4, arr.shape)
    return np.clip(arr, 0, 255).astype(np.uint8)


def _stamp(arr, cx, cy, size=48, k=1.0):
    """依實際模型疊上浮水印:obs = k·α·255 + (1−k·α)·orig。"""
    a = _alpha(size)
    x, y = cx - size // 2, cy - size // 2
    roi = arr[y:y + size, x:x + size].astype(np.float32)
    af = np.clip(k * a, 0.0, 1.0)[:, :, None]
    arr[y:y + size, x:x + size] = np.clip(af * 255.0 + (1 - af) * roi, 0, 255).astype(np.uint8)
    return (x, y, size)


def _save(arr, path):
    Image.fromarray(arr[:, :, ::-1], "RGB").save(path)   # arr 視為 BGR


class TestNoWatermark:
    def test_clean_image_untouched(self, tmp_path):
        """乾淨圖:回傳原路徑,且位元組完全沒變。"""
        p = str(tmp_path / "clean.png")
        _save(_base_image(), p)
        before = Path(p).read_bytes()
        assert remove_watermark(p) == p
        assert Path(p).read_bytes() == before

    def test_clean_image_with_output_path(self, tmp_path):
        """乾淨圖指定 output:不產生誤刮的輸出檔。"""
        p = str(tmp_path / "clean.png")
        _save(_base_image(), p)
        assert remove_watermark(p, str(tmp_path / "out.png")) == p


class TestV2Watermark:
    def test_removed_and_residual_small(self, tmp_path):
        """canonical 位置的 V2 浮水印:移除後與原圖的殘差應大幅下降。"""
        clean = _base_image()
        marked = clean.copy()
        h, w = clean.shape[:2]
        x, y, size = _stamp(marked, w - 120, h - 120)
        p = str(tmp_path / "marked.png")
        _save(marked, p)

        assert remove_watermark(p) == p
        out = np.asarray(Image.open(p))[:, :, ::-1].astype(np.float32)
        reg = (slice(y, y + size), slice(x, x + size))
        before = float(np.mean(np.abs(marked[reg].astype(np.float32) - clean[reg])))
        after = float(np.mean(np.abs(out[reg] - clean[reg])))
        assert after < before * 0.25, f"殘差沒降下來:before={before:.1f} after={after:.1f}"

    def test_large_image_uses_scaled_geometry(self, tmp_path):
        """短邊 ≥1536 的大圖:星心在 (w−240, h−240),尺寸也放大。"""
        clean = _base_image(2048, 2048, seed=11)
        marked = clean.copy()
        x, y, size = _stamp(marked, 2048 - 240, 2048 - 240, size=96)
        p = str(tmp_path / "large.png")
        _save(marked, p)

        assert remove_watermark(p) == p
        out = np.asarray(Image.open(p))[:, :, ::-1].astype(np.float32)
        reg = (slice(y, y + size), slice(x, x + size))
        before = float(np.mean(np.abs(marked[reg].astype(np.float32) - clean[reg])))
        after = float(np.mean(np.abs(out[reg] - clean[reg])))
        assert after < before * 0.25, f"殘差沒降下來:before={before:.1f} after={after:.1f}"

    def test_star_away_from_canonical_is_left_alone(self, tmp_path):
        """幾何先驗:非 canonical 位置的星不在守備範圍,原檔不動。

        (刻意的取捨——只碰固定那一格,絕不在圖片其他地方亂刮。)
        """
        marked = _base_image()
        _stamp(marked, 400, 300)
        p = str(tmp_path / "offspot.png")
        _save(marked, p)
        before = Path(p).read_bytes()
        assert remove_watermark(p) == p
        assert Path(p).read_bytes() == before


class TestFallback:
    def test_nonexistent_file(self):
        assert remove_watermark("/tmp/nonexistent_xyz.png") == "/tmp/nonexistent_xyz.png"

    def test_corrupt_file(self, tmp_path):
        bad = tmp_path / "bad.png"
        bad.write_bytes(b"not an image")
        assert remove_watermark(str(bad)) == str(bad)
