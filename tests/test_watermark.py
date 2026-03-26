"""去水印模組測試（Reverse Alpha Blending）"""
import pytest
import numpy as np
from pathlib import Path
from PIL import Image, ImageDraw
from src.watermark import remove_watermark, _detect_config, _load_alpha_map


class TestDetectConfig:
    def test_large_image(self):
        """寬高都 > 1024 → 96x96"""
        config = _detect_config(2816, 1536)
        assert config["logo_size"] == 96
        assert config["margin"] == 64

    def test_small_image(self):
        """寬或高 <= 1024 → 48x48"""
        config = _detect_config(1024, 559)
        assert config["logo_size"] == 48
        assert config["margin"] == 32

    def test_exact_boundary(self):
        """1024x1024 邊界 → 48x48（不是 >）"""
        config = _detect_config(1024, 1024)
        assert config["logo_size"] == 48


class TestLoadAlphaMap:
    def test_load_48(self):
        alpha = _load_alpha_map(48)
        assert alpha.shape == (48, 48)
        assert alpha.max() <= 1.0
        assert alpha.min() >= 0.0

    def test_load_96(self):
        alpha = _load_alpha_map(96)
        assert alpha.shape == (96, 96)

    def test_cache(self):
        a1 = _load_alpha_map(48)
        a2 = _load_alpha_map(48)
        assert a1 is a2  # 同一個物件（快取）


class TestRemoveWatermark:
    def _make_image(self, tmp_path, w=2816, h=1536):
        """建立測試圖片，右下角加白色模擬水印"""
        img = Image.new("RGB", (w, h), (100, 80, 60))
        draw = ImageDraw.Draw(img)
        config = _detect_config(w, h)
        x = w - config["margin"] - config["logo_size"]
        y = h - config["margin"] - config["logo_size"]
        # 畫白色方塊模擬水印
        draw.rectangle([x, y, x + config["logo_size"], y + config["logo_size"]], fill=(240, 240, 240))
        path = str(tmp_path / "test.png")
        img.save(path)
        return path

    def test_removes_watermark(self, tmp_path):
        """應成功處理並回傳輸出路徑"""
        input_path = self._make_image(tmp_path)
        output_path = str(tmp_path / "output.png")
        result = remove_watermark(input_path, output_path)
        assert result == output_path
        assert Path(output_path).exists()

    def test_default_overwrites(self, tmp_path):
        """不指定 output 時覆蓋原檔"""
        input_path = self._make_image(tmp_path)
        result = remove_watermark(input_path)
        assert result == input_path

    def test_modifies_pixels(self, tmp_path):
        """處理後右下角像素應該改變"""
        input_path = self._make_image(tmp_path)
        before = np.array(Image.open(input_path))

        output_path = str(tmp_path / "output.png")
        remove_watermark(input_path, output_path)
        after = np.array(Image.open(output_path))

        # 右下角區域應該不同
        config = _detect_config(2816, 1536)
        x = 2816 - config["margin"] - config["logo_size"]
        y = 1536 - config["margin"] - config["logo_size"]
        region_before = before[y:y+config["logo_size"], x:x+config["logo_size"]]
        region_after = after[y:y+config["logo_size"], x:x+config["logo_size"]]
        assert not np.array_equal(region_before, region_after)

    def test_small_image(self, tmp_path):
        """小圖也應該處理"""
        input_path = self._make_image(tmp_path, 800, 600)
        output_path = str(tmp_path / "small_output.png")
        result = remove_watermark(input_path, output_path)
        assert result == output_path

    def test_nonexistent_file(self):
        """不存在的檔案回傳原路徑"""
        result = remove_watermark("/tmp/nonexistent_xyz.png")
        assert result == "/tmp/nonexistent_xyz.png"

    def test_real_image(self):
        """用真實 Gemini 圖片測試（如果存在）"""
        real_path = "/home/ct/下載/Gemini_Generated_Image_wiho71wiho71wiho.png"
        if not Path(real_path).exists():
            pytest.skip("真實測試圖片不存在")
        result = remove_watermark(real_path, "/tmp/test_real_clean.png")
        assert result == "/tmp/test_real_clean.png"
        assert Path(result).stat().st_size > 0
