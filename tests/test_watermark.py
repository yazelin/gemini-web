"""去水印模組測試"""
import pytest
from unittest.mock import patch, MagicMock
from src.watermark import remove_watermark, _find_gwt


class TestFindGwt:
    def test_finds_local_binary(self):
        """應找到 repo 內的 binary"""
        result = _find_gwt()
        # 如果 binary 存在就應該找到
        from pathlib import Path
        local = Path(__file__).parent.parent / "bin" / "GeminiWatermarkTool"
        if local.exists():
            assert result is not None
            assert "GeminiWatermarkTool" in result


class TestRemoveWatermark:
    @patch("src.watermark._find_gwt", return_value=None)
    def test_no_tool_returns_original(self, mock_find):
        """找不到工具時回傳原檔路徑"""
        result = remove_watermark("/tmp/test.png")
        assert result == "/tmp/test.png"

    @patch("src.watermark.subprocess.run")
    @patch("src.watermark._find_gwt", return_value="/usr/bin/GeminiWatermarkTool")
    def test_success(self, mock_find, mock_run):
        """成功去水印"""
        mock_run.return_value = MagicMock(returncode=0)
        result = remove_watermark("/tmp/input.png", "/tmp/output.png")
        assert result == "/tmp/output.png"
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert "--input" in args
        assert "--output" in args
        assert "--remove" in args

    @patch("src.watermark.subprocess.run")
    @patch("src.watermark._find_gwt", return_value="/usr/bin/GeminiWatermarkTool")
    def test_failure_returns_original(self, mock_find, mock_run):
        """工具失敗時回傳原檔"""
        mock_run.return_value = MagicMock(returncode=1, stderr="No watermark found")
        result = remove_watermark("/tmp/input.png", "/tmp/output.png")
        assert result == "/tmp/input.png"

    @patch("src.watermark.subprocess.run")
    @patch("src.watermark._find_gwt", return_value="/usr/bin/GeminiWatermarkTool")
    def test_timeout_returns_original(self, mock_find, mock_run):
        """超時回傳原檔"""
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="gwt", timeout=30)
        result = remove_watermark("/tmp/input.png")
        assert result == "/tmp/input.png"

    @patch("src.watermark.subprocess.run")
    @patch("src.watermark._find_gwt", return_value="/usr/bin/GeminiWatermarkTool")
    def test_default_output_overwrites(self, mock_find, mock_run):
        """不指定 output 時覆蓋原檔"""
        mock_run.return_value = MagicMock(returncode=0)
        remove_watermark("/tmp/input.png")
        args = mock_run.call_args[0][0]
        input_idx = args.index("--input")
        output_idx = args.index("--output")
        assert args[input_idx + 1] == "/tmp/input.png"
        assert args[output_idx + 1] == "/tmp/input.png"
