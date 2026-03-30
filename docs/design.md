# Gemini Image API 設計規格

## 概述

獨立的圖片生成 API 服務，使用 Playwright 瀏覽器自動化操作 Gemini 網頁版（Nano Banana / Gemini 3 Pro Image），生成含繁體中文文字的圖片，供多個內部系統透過 HTTP API 呼叫。

## 需求

- **使用場景**：多個內部系統共用（CTOS-Lite、其他專案）
- **併發量**：低量（< 10 張/小時）
- **圖片用途**：混合（社群圖文、文件插圖等），由呼叫端決定 prompt
- **認證**：無認證，內網部署不對外暴露
- **回傳格式**：Base64 JSON
- **選擇 Gemini 的原因**：唯一能正確渲染繁體中文文字的圖片生成模型

## 技術棧

| 元件 | 技術 |
|------|------|
| API 框架 | Python FastAPI |
| 瀏覽器自動化 | Playwright（Chromium） |
| 套件管理 | uv + hatchling |
| 部署 | systemd 服務 |

## 目錄結構

```
gemini-web-api/
├── src/
│   ├── main.py              # FastAPI 入口、lifespan 管理
│   ├── config.py            # 環境變數設定
│   ├── browser.py           # Playwright 瀏覽器管理（啟動、stealth、session）
│   ├── gemini.py            # Gemini 頁面互動（輸入、等待、擷取圖片）
│   ├── selectors.py         # DOM CSS selector 集中管理
│   └── queue.py             # asyncio 請求佇列
├── profiles/                # 瀏覽器 session 持久化目錄
├── pyproject.toml
├── .env.example
└── Dockerfile               # 可選
```

## 架構

```
POST /api/generate {"prompt": "..."}
    ↓
asyncio.Queue（排隊，maxsize=10）
    ↓
單一 Worker
    ↓
browser.py — 持久化 Chromium context（已登入 Gemini）
    ↓
gemini.py — 輸入 prompt → 送出 → 等待圖片生成 → 從 DOM 擷取
    ↓
回傳 {"images": ["data:image/png;base64,..."]}
```

## 瀏覽器管理（browser.py）

### 啟動策略

- 使用 `chromium.launch_persistent_context()` 將 Google 登入狀態存在 `profiles/` 目錄
- 首次啟動：`HEADLESS=false`，手動登入 Google，之後改 `HEADLESS=true`
- 服務啟動時自動開瀏覽器，服務關閉時自動清理

### Stealth 防偵測

參考 Project Golem 的 BrowserLauncher：

- 偽裝 `navigator.webdriver` 為 false
- 設定真實 User-Agent、語言（zh-TW）、時區（Asia/Taipei）
- 偽裝 WebGL vendor/renderer
- 擋掉不必要的資源（字體、追蹤腳本）減少流量

### Session 保活

- 定時心跳（每 5 分鐘檢查頁面是否還活著）
- 偵測到 Google 登入過期 → log 警告，API 回傳 503
- 不自動重新登入（安全考量，需人工介入）

### 頁面管理

- 單一分頁複用，每次生圖完點擊「新對話」重置
- 避免開太多分頁導致記憶體膨脹

## Gemini 頁面互動（gemini.py）

### 互動流程

1. 確認頁面就緒（輸入框可用）
2. 在輸入框輸入 prompt
3. 按 Enter 或點擊送出按鈕
4. 等待回應完成（偵測「停止生成」按鈕消失）
5. 從回應區域擷取圖片元素
6. 將圖片轉為 base64
7. 點擊「新對話」重置狀態

### 圖片擷取策略

Gemini 生成的圖片可能以幾種形式出現：

- `<img src="data:image/...">`（直接 base64）
- `<img src="https://...">`（遠端 URL）
- `<canvas>` 元素

統一用 `page.evaluate()` 在瀏覽器端把圖片繪製到 canvas → 匯出 base64，不管原始格式都能處理。

### 容錯機制

| 情況 | 處理 |
|------|------|
| Gemini 拒絕生圖（內容審查） | 偵測拒絕訊息文字，回傳 `error: "content_blocked"` |
| 回應超時（預設 60 秒） | 回傳 408 `error: "timeout"` |
| DOM 結構變了找不到元素 | 回傳 502 `error: "browser_error"` + 詳細 log |
| Gemini 回了文字沒有圖 | 回傳 `error: "no_image"`，附帶 Gemini 的文字回應 |

## DOM Selector 管理（selectors.py）

所有 CSS selector 集中管理，Gemini 改版時只需更新此檔案：

```python
SELECTORS = {
    "input": "div[contenteditable='true']",
    "send": "button[aria-label='Send']",
    "response": "div.response-container",
    "images": "img.generated-image",
    "new_chat": "button[aria-label='New chat']",
    "stop": "button[aria-label='Stop']",
}
```

注意：以上為示意值，實際 selector 需在開發時對照真實 Gemini DOM 確認。

## 請求佇列（queue.py）

- `asyncio.Queue(maxsize=10)` 排隊緩衝
- 超過 10 個排隊 → 429 Too Many Requests
- 單一 worker 循環取任務 → 操作瀏覽器 → 回傳結果
- 每個請求帶 timeout，佇列等待超過 timeout 回 408

## API 介面

### POST /api/generate

```json
// Request
{
    "prompt": "畫一張台北101的夜景海報，標題寫「歡迎來到台北」",
    "timeout": 60
}

// Response 成功
{
    "success": true,
    "images": ["data:image/png;base64,..."],
    "prompt": "畫一張...",
    "elapsed_seconds": 12.3
}

// Response 失敗
{
    "success": false,
    "error": "content_blocked",
    "message": "Gemini 拒絕生成此內容"
}
```

### GET /api/health

```json
{
    "status": "ok",
    "browser_alive": true,
    "logged_in": true,
    "queue_size": 2,
    "uptime_seconds": 3600
}
```

### POST /api/new-chat

手動重置 Gemini 對話（除錯用），回傳 `{"success": true}`。

## 環境變數

```bash
# 瀏覽器
HEADLESS=false              # 首次登入設 false，之後改 true
PROFILE_DIR=profiles        # 瀏覽器 session 目錄
GEMINI_URL=https://gemini.google.com/app

# Stealth
STEALTH_LANGUAGE=zh-TW,zh,en-US,en
STEALTH_TIMEZONE=Asia/Taipei

# 服務
HOST=0.0.0.0
PORT=8070
QUEUE_MAX_SIZE=10
DEFAULT_TIMEOUT=60

# 心跳
HEARTBEAT_INTERVAL=300      # 秒
```

## 部署

### 首次啟動（手動登入）

```bash
cd ~/SDD/gemini-web-api
uv sync
playwright install chromium
HEADLESS=false uv run uvicorn src.main:app --port 8070
# 在彈出的瀏覽器中手動登入 Google
# 登入完成後關閉服務，修改 .env 中 HEADLESS=true
```

### 正式運行

```bash
uv run uvicorn src.main:app --host 0.0.0.0 --port 8070
```

### systemd 服務

提供 `scripts/install-service.sh`，跟 CTOS-Lite 同樣模式。

### 部署位置

```
~/SDD/gemini-web-api/     # 獨立 repo，獨立部署
~/SDD/ctos-lite/            # 透過內網 HTTP 呼叫 gemini-web-api
```

## 已知風險與限制

1. **Google ToS 違規** — 瀏覽器自動化操作 Gemini 違反使用條款，帳號有被封風險
2. **DOM 會變** — Gemini 改版時 selector 需要手動更新，服務可能突然壞掉
3. **登入過期** — Google session 過期需人工重新登入，無法自動化
4. **單一瀏覽器瓶頸** — 同時只能處理一個生圖請求，其他排隊等待
5. **圖片擷取不保證** — Gemini 回傳圖片的 DOM 結構可能變化
6. **內容審查** — Gemini 對人像、版權角色等有嚴格限制，部分 prompt 會被拒絕
