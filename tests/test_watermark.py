"""去水印模組測試

去水印演算法本身由 remove-ai-watermarks 套件負責（並有其自身測試），
這裡只測「我們這層 wrapper」的合約：
- 偵測不到浮水印的圖（乾淨圖）原檔不動
- 讀不到 / 壞檔時 fallback 回原路徑、不丟例外
- output_path 行為（預設覆蓋、指定則寫出）
"""
import numpy as np
from pathlib import Path
from PIL import Image

from src.watermark import remove_watermark


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
