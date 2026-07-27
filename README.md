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

**金鑰**：只要設過任何一把金鑰（`.env` 的 `API_KEYS` 或 admin webui 現場發的動態 key），
`/api/chat`、`/api/generate`、`/api/edit` 就都要帶 `x-goog-api-key`，沒帶回 403；一把金鑰都沒設過時維持開放（本機開發）。
公開反代出去的服務請務必發 key —— 瀏覽器路燒的是登入帳號的**訂閱配額**，不是免費資源。
建議一個 consumer 發一把（admin → API Keys），History 才分得出哪個專案吃掉多少。

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

#### POST /api/edit（參考圖編輯 / image-to-image）

帶一張參考圖 + prompt，回傳編輯後的圖。`reference_image` 接受 data URL 或純 base64。

```bash
curl -X POST http://localhost:8070/api/edit \
  -H "Content-Type: application/json" \
  -d '{"prompt": "redraw as a 3x3 sticker sheet", "reference_image": "data:image/png;base64,..."}'
```

回傳同 `/api/generate`：`{ "success": true, "images": ["data:image/png;base64,..."], ... }`。

#### 官方 Gemini API fallback（付費、快、穩）

瀏覽器路不另外按張計費（吃的是登入帳號的訂閱配額），但單線、較慢（~40s）且偶爾因 Gemini 改版而脆。可設定「瀏覽器失敗時
自動頂替」的官方 Gemini Developer API（`generativelanguage`，~10s、按張計費）。適用於
`/api/edit` 與 `/api/generate`。

| 環境變數 | 說明 |
|---|---|
| `GEMINI_OFFICIAL_API_KEY` | AI Studio 的 Gemini API key（`?key=`）。**未設 = 完全不啟用付費路** |
| `GEMINI_OFFICIAL_MODEL` | 影像模型 id，預設 `gemini-3.1-flash-image-preview` |
| `GEMINI_OFFICIAL_MODE` | `off` / `fallback`（瀏覽器失敗才頂上，預設）/ `primary`（一律直接走官方、跳過瀏覽器） |

- 加 `?official=1` query 可強制單次走官方路（測試 / 急用）。
- **安全**：付費官方路**一律要求帶有效 `API_KEYS` 金鑰**（`x-goog-api-key`）。沒帶 key 的
  呼叫端只會走免費瀏覽器路，**無法觸發付費 API** —— 防止公開端點被拿來燒你的帳單。

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
| `WORKER_COUNT` | 併行 worker 數（每個 worker = 一個瀏覽器 + 一個獨立 Google 帳號） | `1` |
| `API_KEYS` | API 金鑰（逗號分隔多組；完全沒設過任何 key 時 `/api/*` 維持開放） | 無 |
| `GEMINI_OFFICIAL_API_KEY` | 官方 Gemini API key（付費 fallback；未設=不啟用） | 無 |
| `GEMINI_OFFICIAL_MODEL` | 官方 fallback 影像模型 id | `gemini-3.1-flash-image-preview` |
| `GEMINI_OFFICIAL_MODE` | `off` / `fallback` / `primary` | `fallback` |
| `ADMIN_USERNAME` | Admin webui 登入帳號 | `admin` |
| `ADMIN_PASSWORD` | Admin webui 登入密碼 | `change-me`（**上線前務必改**） |
| `ADMIN_SESSION_SECRET` | Admin session cookie 簽章密鑰 | `dev-only-session-secret`（**上線前務必改**） |
| `ADMIN_URL_PREFIX` | Admin webui 反代路徑前綴（如 `/gemini-web`） | 空 |
| `GENERATED_DIR` | Admin History 頁落地圖片的目錄（API 回應仍是 base64，不受影響） | `~/.gemini-web/generated` |
| `IMAGE_RETENTION_DAYS` | 落地圖片保留天數，超過的下次生圖時順便清 | `7` |

## 多帳號 worker pool

`WORKER_COUNT=N` 會開 N 個瀏覽器，各自吃自己的 profile 目錄（`profiles`、`profiles-1`、
`profiles-2`…），也就是 N 個獨立的 Google 帳號。每個帳號一次處理一個請求，所以併行度就是 N，
而且用量分散在多個訂閱額度上。

加帳號：`gemini-web login -w <N>`（會開有頭瀏覽器，需要桌面環境或 X forwarding），
然後把 `WORKER_COUNT` 調大重啟。實測每個 worker 常駐約 0.6–0.7 GB 記憶體。

Admin Overview 可即時切派工模式，設定存在 DB、重啟沿用：

| 模式 | 行為 | 什麼時候用 |
|---|---|---|
| `round-robin`（預設） | 每筆請求換下一個 worker | 用量平均攤在各帳號，誰都不會先撞到上限 |
| `spillover` | 固定用 worker 0，其餘只在它忙碌時才動 | 想把備用帳號留著 |

**同一個 Gemini 網頁在不同帳號會有 UI 變體**（選單項目不同、第一次上傳要先按同意條款、
出圖是 blob 還是下載鈕）。所以「只有某一個 worker 壞掉」是正常現象，不要先懷疑登入 ——
Overview 的 24 小時成功率就是為了讓這件事一眼看得出來。

## Admin webui

`/admin`（比照 [codex-image-service](https://github.com/yazelin/codex-image-service) 同一套操作習慣）：登入（含 remember-me）、Overview（worker 健康度／**近 24 小時各 worker 成功率**／排隊中+執行中/uptime／近期活動）、API Keys（現場發放/停用/刪除動態 key，`.env` 的 `API_KEYS` 仍有效、唯讀顯示）、Test（手動測 generate/edit/chat，含參考圖上傳）、History（近 500 筆請求，含哪把 key、哪個 worker、輸出圖連結；prompt 與 error 直接顯示前 90 字摘要，不必逐筆點開）。

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

- 併行度 = `WORKER_COUNT`（每個 worker 一次一個請求，其餘排隊）；單帳號時就是一次一件
- Google 登入過期需手動重新登入（`gemini-web login`）
- Gemini 改版可能導致 DOM selector 失效，需更新 `src/selectors.py`
- **同一個 Gemini 網頁在不同帳號會有 UI 變體**（選單多寡、上傳是否多一層子選單、
  出圖是 blob 還是下載鈕）。worker 之間只有一個壞掉時先往這個方向想，不要先懷疑登入；
  上傳流程已同時支援「點一次就開檔案對話框」與「再展開一層子選單」兩種帳號
- 違反 Google 服務條款，帳號有被封風險
- 生圖耗時約 30-120 秒，視 Gemini 伺服器負載而定

## 授權

MIT License
