# Gemini Image

使用 Playwright 自動化 Gemini 網頁版生成含繁體中文文字的圖片。提供 **CLI 工具**和 **HTTP API** 兩種使用方式。

自動移除 Gemini 可見水印（Reverse Alpha Blending，基於 [gemini-watermark-remover](https://github.com/journey-ad/gemini-watermark-remover)）。

## 安裝

> ⚠️ **不要用 `pip install`** — pip 不會建立隔離環境，會導致 Playwright 安裝失敗。請用 uv 或 pipx。

```bash
# 1. 安裝 uv（如果還沒有）
curl -LsSf https://astral.sh/uv/install.sh | sh              # macOS / Linux
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"   # Windows

# 2. 安裝 gemini-image
uv tool install gemini-image && gemini-image install
```

備選方式（效果相同）：
```bash
pipx install gemini-image && gemini-image install
```

`gemini-image install` 會安裝 Chromium 瀏覽器（Playwright）。

### 已經用 pip 裝過？

```bash
pip uninstall gemini-image -y
uv tool install gemini-image && gemini-image install
```

## 首次登入 Google

```bash
gemini-image login
```

在彈出的瀏覽器中登入 Google 帳號，確認進入 Gemini 頁面，按 Enter 關閉。登入狀態存在 `~/.gemini-image/profiles/`，之後不需要重新登入。

## 使用方式

### CLI 工具

```bash
# 生成圖片（自動 headless）
gemini-image generate "A cute cat sitting on a windowsill" -o cat.png

# 生成 + 去水印
gemini-image generate "A poster with text '歡迎光臨'" -o poster.png --no-watermark

# 詳細 log
gemini-image generate "畫一隻柴犬" -o shiba.png --no-watermark -v
```

Prompt 不含「畫」「draw」「generate」等關鍵字時，會自動加上 `Generate an image:` 前綴。

### HTTP API

```bash
# 啟動服務
gemini-image serve
# 或
gemini-image serve --host 0.0.0.0 --port 8070
```

API 模式自動去水印、自動下載原尺寸圖片。

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

使用 **Reverse Alpha Blending** 演算法，數學精確還原被水印覆蓋的像素：

```
original = (watermarked - alpha × logo) / (1 - alpha)
```

- 純 Python 實作（Pillow + NumPy），跨平台
- 大圖（寬高 > 1024）：96×96 水印，64px 邊距
- 小圖：48×48 水印，32px 邊距
- API 模式自動去水印
- CLI 模式加 `--no-watermark`
- 不可見的 SynthID 浮水印無法移除

基於 [gemini-watermark-remover](https://github.com/journey-ad/gemini-watermark-remover) 和 [Python 版本](https://github.com/VimalMollyn/Gemini-Watermark-Remover-Python)。

## AI Agent 整合

讓你的 AI Agent 能呼叫 `gemini-image` 生圖。

### 步驟

1. 安裝 `gemini-image`（見上方安裝說明，**必須用 uv 或 pipx，不要用 pip**）
2. 把 `AGENTS.md` 加入你的 AI Agent 的上下文：
   - **Gemini CLI**：本倉庫已包含 `GEMINI.md`，Gemini CLI 會自動讀取
   - **Claude Code**：複製 `.claude/skills/generate-image.md` 到你的專案 `.claude/skills/` 或全域 `~/.claude/skills/`
   - **Cursor / Windsurf**：把 `AGENTS.md` 內容加入你的 rules 設定
   - **其他 Agent**：讓 Agent 讀取 `AGENTS.md` 作為系統指引

### CLI 呼叫

```bash
gemini-image generate "detailed english prompt" -o /path/to/output.png --no-watermark
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
| `PROFILE_DIR` | 瀏覽器 session 目錄 | `~/.gemini-image/profiles` |
| `GEMINI_URL` | Gemini 網址 | `https://gemini.google.com/app` |
| `PORT` | API 服務埠 | `8070` |
| `DEFAULT_TIMEOUT` | 生圖超時秒數 | `180` |
| `QUEUE_MAX_SIZE` | 最大排隊數 | `10` |

## 從原始碼安裝

```bash
git clone https://github.com/yazelin/gemini-image.git
cd gemini-image
bash scripts/setup.sh
```

## 開發

```bash
uv sync --extra dev
uv run pytest -v
```

## 已知限制

- 一次只能處理一個生圖請求（其他排隊等待）
- Google 登入過期需手動重新登入（`gemini-image login`）
- Gemini 改版可能導致 DOM selector 失效，需更新 `src/selectors.py`
- 違反 Google 服務條款，帳號有被封風險
- 生圖耗時約 30-120 秒，視 Gemini 伺服器負載而定

## 授權

MIT License
