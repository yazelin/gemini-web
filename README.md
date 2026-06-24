# Gemini Image

> **快速安裝：** `uv tool install gemini-web && gemini-web install`

使用 Playwright 自動化 Gemini 網頁版，提供**圖片生成**和**文字對話**功能。支援 **CLI 工具**和 **HTTP API** 兩種使用方式。

自動移除 Gemini 可見水印（NCC 動態偵測 + 反 alpha，基於 [remove-ai-watermarks](https://github.com/wiltodelta/remove-ai-watermarks)）。

## 安裝

> ⚠️ **不要用 `pip install`** — pip 不會建立隔離環境，會導致 Playwright 安裝失敗。請用 uv 或 pipx。

```bash
# 1. 安裝 uv（如果還沒有）
curl -LsSf https://astral.sh/uv/install.sh | sh              # macOS / Linux
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"   # Windows

# 2. 安裝 gemini-web
uv tool install gemini-web && gemini-web install
```

備選方式（效果相同）：
```bash
pipx install gemini-web && gemini-web install
```

`gemini-web install` 會安裝 Chromium 瀏覽器（Playwright）。

### 已經用 pip 裝過？

```bash
pip uninstall gemini-web -y
uv tool install gemini-web && gemini-web install
```

## 首次登入 Google

```bash
gemini-web login
```

在彈出的瀏覽器中登入 Google 帳號，確認進入 Gemini 頁面，按 Enter 關閉。登入狀態存在 `~/.gemini-web/profiles/`，之後不需要重新登入。

## 使用方式

### CLI 工具

```bash
# 文字對話
gemini-web chat "解釋量子力學"

# 生成圖片（自動 headless）
gemini-web generate "A cute cat sitting on a windowsill" -o cat.png

# 生成 + 去水印
gemini-web generate "A poster with text '歡迎光臨'" -o poster.png --no-watermark

# 詳細 log
gemini-web generate "畫一隻柴犬" -o shiba.png --no-watermark -v
```

Prompt 不含「畫」「draw」「generate」等關鍵字時，會自動加上 `Generate an image:` 前綴。

### HTTP API

```bash
# 啟動服務
gemini-web serve
# 或
gemini-web serve --host 0.0.0.0 --port 8070
```

API 模式自動去水印、自動下載原尺寸圖片。

#### POST /api/chat

```bash
curl -X POST http://localhost:8070/api/chat \
  -H "Content-Type: application/json" \
  -d '{"prompt": "解釋量子力學"}'
```

回傳：

```json
{
  "success": true,
  "text": "量子力學是...",
  "prompt": "...",
  "elapsed_seconds": 8.3
}
```

#### POST /api/generate

```bash
curl -X POST http://localhost:8070/api/generate \
  -H "Content-Type: application/json" \
  -d '{"prompt": "畫一張台北101海報"}'
```

回傳：

```json
{
  "success": true,
  "images": ["data:image/png;base64,..."],
  "prompt": "...",
  "elapsed_seconds": 45.2
}
```

#### POST /v1beta/models/{model}:generateContent（Google GenAI API 相容）

完全相容 `google-genai` SDK 格式，可做為 Google Gemini API 的 drop-in replacement。

```bash
# 文字對話
curl -X POST "http://localhost:8070/v1beta/models/gemini-2.5-flash:generateContent?key=YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{"contents": [{"parts": [{"text": "什麼是量子力學"}]}]}'

# 圖片生成
curl -X POST "http://localhost:8070/v1beta/models/gemini-2.5-flash:generateContent?key=YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{"contents": [{"parts": [{"text": "a cute cat"}]}], "generationConfig": {"responseMimeType": "image/png"}}'
```

回傳格式與 Google API 完全一致：

```json
{
  "candidates": [{
    "content": {
      "parts": [{"text": "量子力學是..."}],
      "role": "model"
    },
    "finishReason": "STOP"
  }]
}
```

搭配 `google-genai` SDK 使用：

```python
from google import genai
client = genai.Client(
    api_key="YOUR_KEY",
    http_options={"api_version": "v1beta", "base_url": "http://localhost:8070"}
)
response = client.models.generate_content(model="gemini-2.5-flash", contents="Hello")
print(response.text)
```

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
sudo bash scripts/install-service.sh
```

## 去水印

使用維護中的 [remove-ai-watermarks](https://github.com/wiltodelta/remove-ai-watermarks) 套件，
以 **NCC（Normalized Cross-Correlation）動態偵測** 找出可見浮水印位置後再反 alpha 還原。

- 動態偵測，不寫死位置/大小 —— 新版 Gemini（Gemini 3 / nano-banana-pro）各種長寬比都吃
- 信心門檻 0.6：偵測不到浮水印的圖原檔不動，不會在無浮水印圖上誤刮
- 純 CPU、離線、每張約 0.5 秒
- API 模式自動去水印
- CLI 模式加 `--no-watermark`
- 不可見的 SynthID 浮水印無法移除（需 GPU + 擴散模型，本服務不處理）

> 舊版用寫死右下角位置 + 固定 alpha map 的反 alpha；新版 Gemini 改了輸出比例後會去到錯位、
> 留下痕跡，故改用動態偵測的套件。

## AI Agent 整合

讓你的 AI Agent 能用 `/gemini-web` 指令生圖。

### 安裝（一行搞定）

> ⚠️ **不要用 `pip install`**，必須用 uv 或 pipx。

```bash
uv tool install gemini-web && gemini-web install
```

`gemini-web install` 會自動：
1. 安裝 Chromium 瀏覽器（Playwright）
2. 偵測 Claude Code（`~/.claude/`）→ 安裝 slash commands
3. 偵測 Gemini CLI（`~/.gemini/`）→ 安裝 slash commands

安裝後可用：`/gemini-web <自然語言描述>`、`/generate <英文 prompt>`、`/chat <提問>`

### 登入（需人工操作）

```bash
gemini-web login
```

會彈出瀏覽器，手動登入 Google 帳號後按 Enter 關閉。登入狀態存在 `~/.gemini-web/profiles/`。

### 支援的 AI Agent

| Agent | 自動支援 | 說明 |
|-------|:--------:|------|
| **Claude Code** | ✓ | install 自動安裝 commands 到 `~/.claude/commands/gemini-web/` |
| **Gemini CLI** | ✓ | install 自動安裝 commands 到 `~/.gemini/commands/gemini-web/` |
| **Cursor / Windsurf** | — | 把 `AGENTS.md` 內容加入 rules 設定 |
| **其他 Agent** | — | 讓 Agent 讀取 `AGENTS.md` 作為系統指引 |

### CLI 呼叫

```bash
gemini-web generate "detailed english prompt" -o /path/to/output.png --no-watermark
```

### HTTP API

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
| `HEADLESS` | 無頭模式 | `false`（CLI generate 強制 `true`） |
| `PROFILE_DIR` | 瀏覽器 session 目錄 | `~/.gemini-web/profiles` |
| `GEMINI_URL` | Gemini 網址 | `https://gemini.google.com/app` |
| `PORT` | API 服務埠 | `8070` |
| `DEFAULT_TIMEOUT` | 生圖超時秒數 | `180` |
| `QUEUE_MAX_SIZE` | 最大排隊數 | `10` |
| `API_KEYS` | API 金鑰（逗號分隔多組，空 = 不驗證） | 無 |

## 從原始碼安裝

```bash
git clone https://github.com/yazelin/gemini-web.git
cd gemini-web
bash scripts/setup.sh
```

## 開發

```bash
uv sync --extra dev
uv run pytest -v
```

## 已知限制

- 一次只能處理一個請求（生圖或對話，其他排隊等待）
- Google 登入過期需手動重新登入（`gemini-web login`）
- Gemini 改版可能導致 DOM selector 失效，需更新 `src/selectors.py`
- 違反 Google 服務條款，帳號有被封風險
- 生圖耗時約 30-120 秒，視 Gemini 伺服器負載而定

## 授權

MIT License
