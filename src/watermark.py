"""去水印 — 自動下載並呼叫 GeminiWatermarkTool CLI 移除可見水印"""
import io
import logging
import os
import platform
import shutil
import stat
import subprocess
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)

_GWT_NAME = "GeminiWatermarkTool"
_GWT_VERSION = "v0.2.6"
_GWT_REPO = "allenk/GeminiWatermarkTool"

# 快取目錄：~/.gemini-image/bin/
_CACHE_DIR = Path.home() / ".gemini-image" / "bin"

# 平台 → release asset 名稱對照
_PLATFORM_ASSETS = {
    "linux": "GeminiWatermarkTool-Linux-x64.zip",
    "darwin": "GeminiWatermarkTool-macOS-Universal.zip",
    "windows": "GeminiWatermarkTool-Windows-x64.zip",
}


def _get_platform() -> str:
    """取得平台名稱"""
    system = platform.system().lower()
    if system == "linux":
        return "linux"
    elif system == "darwin":
        return "darwin"
    elif system == "windows":
        return "windows"
    return system


def _get_exe_name() -> str:
    """取得執行檔名稱（Windows 需要 .exe）"""
    if _get_platform() == "windows":
        return f"{_GWT_NAME}.exe"
    return _GWT_NAME


def _download_gwt() -> str | None:
    """從 GitHub Releases 下載對應平台的 GeminiWatermarkTool"""
    plat = _get_platform()
    asset_name = _PLATFORM_ASSETS.get(plat)
    if not asset_name:
        logger.warning("不支援的平台：%s", plat)
        return None

    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    exe_path = _CACHE_DIR / _get_exe_name()

    url = f"https://github.com/{_GWT_REPO}/releases/download/{_GWT_VERSION}/{asset_name}"
    logger.info("下載去水印工具：%s", url)

    try:
        import httpx
        resp = httpx.get(url, follow_redirects=True, timeout=60)
        resp.raise_for_status()

        # 解壓 zip
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            # 找到執行檔
            exe_names = [_GWT_NAME, f"{_GWT_NAME}.exe"]
            for name in zf.namelist():
                basename = Path(name).name
                if basename in exe_names:
                    data = zf.read(name)
                    exe_path.write_bytes(data)
                    # 設定可執行權限（Linux/macOS）
                    if plat != "windows":
                        exe_path.chmod(exe_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
                    logger.info("去水印工具已安裝：%s", exe_path)
                    return str(exe_path)

        logger.warning("zip 中找不到執行檔")
        return None
    except Exception as e:
        logger.warning("下載去水印工具失敗：%s", e)
        return None


def _find_gwt() -> str | None:
    """找到 GeminiWatermarkTool 執行檔（必要時自動下載）"""
    exe_name = _get_exe_name()

    # 1. 快取目錄
    cached = _CACHE_DIR / exe_name
    if cached.exists() and cached.is_file():
        return str(cached)

    # 2. PATH 中
    found = shutil.which(_GWT_NAME)
    if found:
        return found

    # 3. 自動下載
    return _download_gwt()


def remove_watermark(input_path: str, output_path: str | None = None, denoise: str = "ai") -> str:
    """移除圖片可見水印

    Args:
        input_path: 輸入圖片路徑
        output_path: 輸出路徑（預設覆蓋原檔）
        denoise: 去噪方法（ai/ns/telea/soft/off）

    Returns:
        輸出路徑（失敗時回傳原檔路徑）
    """
    gwt = _find_gwt()
    if not gwt:
        logger.warning("GeminiWatermarkTool 未安裝且無法下載，跳過去水印")
        return input_path

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
            logger.info("去水印工具回傳 code %d：%s", result.returncode, result.stderr.strip())
            return input_path
    except subprocess.TimeoutExpired:
        logger.warning("去水印超時")
        return input_path
    except Exception as e:
        logger.warning("去水印失敗：%s", e)
        return input_path
