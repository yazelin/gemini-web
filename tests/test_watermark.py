"""去水印模組測試

- 乾淨圖(canonical 區無星)原檔不動,不誤刮
- 合成的色彩自適應星(灰/彩色、半透明)偵測得到且移除後殘差大幅下降
- 讀不到 / 壞檔時 fallback 回原路徑、不丟例外
- output_path 行為(預設覆蓋、指定則寫出)
"""
import numpy as np
from pathlib import Path
from PIL import Image

from src.watermark import _get_alpha_maps, remove_watermark


def _make_plain_image(tmp_path, w=1408, h=768, name="plain.png"):
    """純漸層圖、無浮水印 → 偵測信心應低於門檻。"""
    arr = np.zeros((h, w, 3), dtype=np.uint8)
    arr[:, :, 0] = np.linspace(30, 200, w, dtype=np.uint8)[None, :]
    arr[:, :, 1] = 80
    arr[:, :, 2] = np.linspace(200, 30, h, dtype=np.uint8)[:, None]
    path = str(tmp_path / name)
    Image.fromarray(arr, "RGB").save(path)
    return path


class TestNoWatermark:
    def test_clean_image_returns_input_untouched(self, tmp_path):
        """乾淨圖：回傳原路徑、且檔案位元組完全沒變（不亂刮）。"""
        input_path = _make_plain_image(tmp_path)
        before = Path(input_path).read_bytes()
        result = remove_watermark(input_path)
        assert result == input_path
        assert Path(input_path).read_bytes() == before

    def test_clean_image_with_output_path(self, tmp_path):
        """乾淨圖指定 output：偵測不到 → 回原路徑，不產生誤刮的輸出檔。"""
        input_path = _make_plain_image(tmp_path)
        output_path = str(tmp_path / "out.png")
        result = remove_watermark(input_path, output_path)
        assert result == input_path


def _stamp_star(arr, color, k=1.6, size=48):
    """在 canonical 位置 (w−120, h−120) 疊一顆色彩自適應星:
    obs = k·α·c + (1−k·α)·orig(同 Gemini 2026-07 新版浮水印的混合模型)。"""
    h, w = arr.shape[:2]
    tpl = _get_alpha_maps()[size]
    x = w - 120 - size // 2
    y = h - 120 - size // 2
    roi = arr[y:y + size, x:x + size].astype(np.float32)
    af = np.clip(k * tpl[:, :, None], 0.0, 1.0)
    blended = af * np.array(color, np.float32) + (1.0 - af) * roi
    arr[y:y + size, x:x + size] = np.clip(blended, 0, 255).astype(np.uint8)
    return (x, y, size)


class TestAdaptiveStar:
    def _run(self, tmp_path, color):
        rng = np.random.default_rng(7)
        arr = np.zeros((768, 1408, 3), dtype=np.uint8)
        arr[:, :, 0] = np.linspace(60, 190, 1408, dtype=np.uint8)[None, :]
        arr[:, :, 1] = 120
        arr[:, :, 2] = np.linspace(180, 70, 768, dtype=np.uint8)[:, None]
        arr = np.clip(arr.astype(np.int16) + rng.integers(-6, 7, arr.shape), 0, 255).astype(np.uint8)
        clean = arr.copy()
        x, y, size = _stamp_star(arr, color)
        path = str(tmp_path / "marked.png")
        Image.fromarray(arr[:, :, ::-1], "RGB").save(path)  # arr 當 BGR 存

        result = remove_watermark(path)
        assert result == path
        out = np.asarray(Image.open(path))[:, :, ::-1].astype(np.float32)
        region = (slice(y, y + size), slice(x, x + size))
        before = float(np.mean(np.abs(arr[region].astype(np.float32) - clean[region])))
        after = float(np.mean(np.abs(out[region] - clean[region])))
        assert after < before * 0.45, f"殘差沒有明顯下降:before={before:.1f} after={after:.1f}"

    def test_gray_star_removed(self, tmp_path):
        """灰色半透明星(新版主流變體)偵測得到且移除。"""
        self._run(tmp_path, (150, 150, 150))

    def test_colored_star_removed(self, tmp_path):
        """彩色星(取樣周圍色的變體,如橘/洋紅)也移除得掉。"""
        self._run(tmp_path, (40, 90, 230))


class TestFallback:
    def test_nonexistent_file(self):
        """不存在的檔案回傳原路徑、不丟例外。"""
        result = remove_watermark("/tmp/nonexistent_xyz.png")
        assert result == "/tmp/nonexistent_xyz.png"

    def test_corrupt_file(self, tmp_path):
        """壞檔（非圖片）回傳原路徑、不丟例外。"""
        bad = tmp_path / "bad.png"
        bad.write_bytes(b"not an image")
        result = remove_watermark(str(bad))
        assert result == str(bad)
