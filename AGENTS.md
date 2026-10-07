# gemini-web — AI Agent 使用指引

本工具讓 AI Agent 能透過 Gemini 網頁版進行文字對話、上傳檔案問問題、生圖、改圖、做音樂（含自動去水印）。

## 前置需求

確認 `gemini-web` 已安裝且已登入：

```bash
gemini-web health  # API 模式：檢查服務是否運行
# 或
which gemini-web   # CLI 模式：確認指令存在
```

## 連線與金鑰

- 服務網址預設 `http://localhost:8070`（`gemini-web serve`）；別人架的服務用對方給的網址，例如 `https://example.com/gemini-web`。
- 服務只要設過任何一把金鑰，每個請求都要帶 `x-goog-api-key: <金鑰>` 標頭，沒帶回 403。金鑰從環境變數讀（例如 `GEMINI_API_KEY`），**不要寫進程式碼或 commit**。
- 長時間的工作（生圖、音樂）建議用排隊版：`POST /api/jobs` 拿 `job_id`，再 `GET /api/jobs/{job_id}` 輪詢。

| 端點 | 用途 |
|---|---|
| `POST /api/chat` | 文字對話 |
| `POST /api/chat-file` | 上傳檔案（base64）再問問題 |
| `POST /api/generate` | 生圖 |
| `POST /api/edit` | 給參考圖改圖 |
| `POST /api/music` | 做音樂，可選 `length`、`vocals`、`genre`（值不在清單內回 422 並列出可用值） |
| `POST /api/video` | 做影片（只有開得出「建立影片」的帳號能用，先看 `/api/capabilities`） |
| `POST /api/jobs`、`GET /api/jobs/{job_id}` | 排隊版 |
| `POST /v1beta/models/{model}:generateContent` | 相容官方 Gemini API，`google-genai` SDK 把 `base_url` 指過來就能用 |
| `GET /api/health`、`GET /api/capabilities` | 服務狀態、各帳號能做什麼 |

每個端點的欄位與範例見 README。

## 使用方式

### 文字對話

```bash
# CLI
gemini-web chat "<prompt>"

# HTTP API
curl -X POST http://localhost:8070/api/chat \
  -H "x-goog-api-key: $GEMINI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "<prompt>"}'
```

回傳 JSON：`{"success": true, "text": "..."}`

### 圖片生成

```bash
# CLI
gemini-web generate "<prompt>" -o <output_path> --no-watermark

# HTTP API
curl -X POST http://localhost:8070/api/generate \
  -H "x-goog-api-key: $GEMINI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "<prompt>"}'
```

回傳 JSON：`{"success": true, "images": ["data:image/png;base64,..."]}`

### 參數說明

- `<prompt>`：圖片描述，**建議用英文**效果最好
- `-o`：輸出檔案路徑（支援 .png / .jpg）
- `--no-watermark`：移除 Gemini 水印（建議始終加上）
- 文字對話耗時約 5-30 秒，圖片生成約 30-120 秒

## Prompt 撰寫規則

**重要：不要原封不動轉發使用者的請求。** 你應該：

1. 理解使用者意圖
2. 擴寫為詳細的英文 prompt
3. 描述主體、風格、構圖、色彩、氛圍
4. 如果需要圖片中出現中文文字，用引號標明：`with text "歡迎光臨"`

### 範例

使用者說「畫一隻貓」→ 你應該送：
```
A cute fluffy orange tabby cat sitting on a windowsill, warm afternoon sunlight streaming in, cozy atmosphere, soft watercolor illustration style, gentle expression with bright curious eyes
```

使用者說「做一張開幕海報」→ 你應該送：
```
A modern grand opening poster design with bold typography showing text "盛大開幕" at the top, celebratory confetti and ribbons, red and gold color scheme, professional marketing design, clean layout
```

## 錯誤處理

| 錯誤 | 意義 | 建議 |
|------|------|------|
| `content_blocked` | Gemini 拒絕生成 | 換一個不涉及敏感內容的 prompt |
| `no_image` | 沒有生成圖片 | prompt 更具體，確認包含「draw」「generate」等關鍵字 |
| `no_response` | 文字對話無回應 | 稍後再試 |
| `timeout` | 生成超時 | 稍後再試 |
| `browser_error` | 瀏覽器問題 | 檢查 `gemini-web health` |

## 安裝（如果尚未安裝）

> ⚠️ **不要用 `pip install`** — 必須用 uv 或 pipx，否則 Playwright 安裝會失敗。

```bash
# 1. 安裝 uv（如果還沒有）
# macOS / Linux:
curl -LsSf https://astral.sh/uv/install.sh | sh
# Windows PowerShell:
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"

# 2. 安裝 gemini-web + Chromium
uv tool install gemini-web && gemini-web install

# 3. 登入 Google（⚠️ 此步驟需要人類手動操作，會彈出瀏覽器）
gemini-web login
```

如果之前用 pip 裝過，先移除：`pip uninstall gemini-web -y`
