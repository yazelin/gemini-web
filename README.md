# Gemini Image

使用 Playwright 自動化 Gemini 網頁版生成含繁體中文文字的圖片。提供 **CLI 工具**和 **HTTP API** 兩種使用方式。

自動移除 Gemini 可見水印（使用 [GeminiWatermarkTool](https://github.com/allenk/GeminiWatermarkTool)）。

## 安裝

### 一行安裝（推薦）

```bash
uv tool install git+https://github.com/yazelin/gemini-image.git && playwright install chromium
```

或用 pipx：

```bash
pipx install git+https://github.com/yazelin/gemini-image.git && playwright install chromium
```

安裝完成後 `gemini-image` 指令全域可用。

### 從原始碼安裝

```bash
git clone https://github.com/yazelin/gemini-image.git
cd gemini-image
bash scripts/setup.sh
```

## 首次登入 Google

首次使用需手動登入一次，之後 session 自動持久化：

```bash
gemini-image login
```

在彈出的瀏覽器中登入 Google 帳號，確認進入 Gemini 頁面，按 Enter 關閉。

## 使用方式

### CLI 工具

```bash
# 生成圖片
gemini-image generate "A cute cat sitting on a windowsill" -o cat.png

# 生成圖片 + 自動去水印
gemini-image generate "A poster with text '歡迎光臨'" -o poster.png --no-watermark

# 查看說明
gemini-image --help
gemini-image generate --help
```

### HTTP API

```bash
# 啟動 API 服務
gemini-image serve

# 或直接用 uvicorn
uv run uvicorn src.main:app --host 0.0.0.0 --port 8070
```

#### POST /api/generate

```bash
curl -X POST http://localhost:8070/api/generate \
  -H "Content-Type: application/json" \
  -d '{"prompt": "A poster with text 歡迎來到台北, modern design"}'
```

回傳：

```json
{
  "success": true,
  "images": ["data:image/png;base64,..."],
  "prompt": "...",
  "elapsed_seconds": 27.8
}
```

API 模式會自動去水印。

#### GET /api/health

```json
{
  "status": "ok",
  "browser_alive": true,
  "logged_in": true,
  "queue_size": 0,
  "uptime_seconds": 3600
}
```

#### POST /api/new-chat

手動重置 Gemini 對話（除錯用）。

### systemd 服務部署

```bash
# 確認 .env 中 HEADLESS=true
sudo bash scripts/install-service.sh
```

## AI Agent 整合

### 作為 MCP 工具

在你的 AI agent 中呼叫 CLI：

```bash
gemini-image generate "detailed prompt here" -o /path/to/output.png --no-watermark
```

### 作為 HTTP API

```python
import httpx
resp = httpx.post("http://localhost:8070/api/generate", json={"prompt": "..."}, timeout=200)
data = resp.json()
if data["success"]:
    images = data["images"]  # base64 list
```

## 環境變數

| 變數 | 說明 | 預設 |
|------|------|------|
| `HEADLESS` | 無頭模式（首次登入設 false） | `false` |
| `PROFILE_DIR` | 瀏覽器 session 目錄 | `profiles` |
| `GEMINI_URL` | Gemini 網址 | `https://gemini.google.com/app` |
| `PORT` | API 服務埠 | `8070` |
| `DEFAULT_TIMEOUT` | 生圖超時秒數 | `180` |
| `QUEUE_MAX_SIZE` | 最大排隊數 | `10` |
| `HEARTBEAT_INTERVAL` | 心跳檢查間隔秒數 | `300` |

## 去水印

使用 [GeminiWatermarkTool](https://github.com/allenk/GeminiWatermarkTool)（反向 Alpha 混合 + AI 降噪）移除 Gemini 右下角可見水印。

- API 模式：自動去水印
- CLI 模式：加 `--no-watermark` 參數
- Binary 位於 `bin/GeminiWatermarkTool`

注意：不可見的 SynthID 浮水印無法移除。

## 已知限制

- 一次只能處理一個生圖請求（其他排隊）
- Google 登入過期需手動重新登入（`gemini-image login`）
- Gemini 改版可能導致 DOM selector 失效，需手動更新 `src/selectors.py`
- 違反 Google 服務條款，帳號有被封風險
- 生圖耗時約 20-60 秒，視 Gemini 伺服器負載而定

## 開發

```bash
uv sync --extra dev
uv run pytest -v
```
