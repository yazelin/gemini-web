"""去水印 — 呼叫 GeminiWatermarkTool CLI 移除可見水印"""
import logging
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# binary 路徑（repo 內 bin/ 目錄）
_BIN_DIR = Path(__file__).parent.parent / "bin"
_GWT_NAME = "GeminiWatermarkTool"


def _find_gwt() -> str | None:
    """找到 GeminiWatermarkTool 執行檔"""
    # 優先用 repo 內的 binary
    local = _BIN_DIR / _GWT_NAME
    if local.exists() and local.is_file():
        return str(local)
    # 其次找 PATH 中的
    found = shutil.which(_GWT_NAME)
    return found


def remove_watermark(input_path: str, output_path: str | None = None, denoise: str = "ai") -> str:
    """移除圖片可見水印

    Args:
        input_path: 輸入圖片路徑
        output_path: 輸出路徑（預設覆蓋原檔）
        denoise: 去噪方法（ai/ns/telea/soft/off）

    Returns:
        成功回傳輸出路徑，失敗回傳錯誤訊息
    """
    gwt = _find_gwt()
    if not gwt:
        logger.warning("GeminiWatermarkTool 未安裝，跳過去水印")
        return input_path  # 找不到工具就回傳原檔

    if output_path is None:
        output_path = input_path

    cmd = [
        gwt,
        "--no-banner",
        "--input", input_path,
        "--output", output_path,
        "--remove",
        "--denoise", denoise,
        "--quiet",
    ]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            logger.info("去水印完成：%s", output_path)
            return output_path
        else:
            # returncode != 0 可能是偵測不到水印（正常情況）
            logger.info("去水印工具回傳 code %d：%s", result.returncode, result.stderr.strip())
            return input_path  # 回傳原檔
    except subprocess.TimeoutExpired:
        logger.warning("去水印超時")
        return input_path
    except Exception as e:
        logger.warning("去水印失敗：%s", e)
        return input_path
