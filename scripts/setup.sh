#!/usr/bin/env bash
# Gemini Image 一鍵安裝腳本
# 用法：bash scripts/setup.sh
set -euo pipefail

echo "=== Gemini Image 安裝 ==="
echo ""

# 1. 檢查 Python
if ! command -v python3 &>/dev/null; then
    echo "錯誤：需要 Python 3.11+，請先安裝"
    exit 1
fi

PY_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "✓ Python ${PY_VERSION}"

# 2. 檢查/安裝 uv
if ! command -v uv &>/dev/null; then
    echo "安裝 uv 套件管理器..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi
echo "✓ uv $(uv --version 2>/dev/null || echo 'installed')"

# 3. 安裝 Python 依賴
echo "安裝依賴..."
uv sync
echo "✓ Python 依賴安裝完成"

# 4. 安裝 Playwright Chromium
echo "安裝 Chromium 瀏覽器..."
uv run playwright install chromium
echo "✓ Chromium 安裝完成"

# 5. 安裝 Playwright 系統依賴（需要 sudo）
echo ""
echo "安裝系統依賴（需要 sudo 權限）..."
echo "如果跳過，可能會在啟動時遇到缺少函式庫的錯誤。"
read -p "要安裝系統依賴嗎？(Y/n) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Nn]$ ]]; then
    uv run playwright install-deps chromium
    echo "✓ 系統依賴安裝完成"
else
    echo "⚠ 跳過系統依賴安裝"
fi

# 6. 建立 .env
if [ ! -f .env ]; then
    cp .env.example .env
    echo "✓ 已建立 .env（從 .env.example 複製）"
else
    echo "✓ .env 已存在"
fi

# 7. 檢查去水印工具
if [ -f bin/GeminiWatermarkTool ]; then
    echo "✓ GeminiWatermarkTool $(bin/GeminiWatermarkTool --version 2>/dev/null || echo 'found')"
else
    echo "⚠ bin/GeminiWatermarkTool 不存在，去水印功能將停用"
    echo "  手動下載：https://github.com/allenk/GeminiWatermarkTool/releases"
fi

echo ""
echo "=== 安裝完成 ==="
echo ""
echo "下一步："
echo "  1. 登入 Google（首次必須）："
echo "     uv run gemini-image login"
echo ""
echo "  2. 使用 CLI 生圖："
echo "     uv run gemini-image generate \"A cute cat\" -o cat.png --no-watermark"
echo ""
echo "  3. 或啟動 API 服務："
echo "     修改 .env 中 HEADLESS=true"
echo "     uv run gemini-image serve"
echo ""
