# Gemini Image

> **快速安裝：** `uv tool install gemini-web && gemini-web install`

使用 Playwright 自動化 Gemini 網頁版，提供**圖片生成**和**文字對話**功能。支援 **CLI 工具**和 **HTTP API** 兩種使用方式。

自動移除 Gemini 可見水印（V2 profile 反向 alpha 混合，純 OpenCV、無額外依賴）。

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
`/api/chat`、`/api/chat-file`、`/api/generate`、`/api/edit` 就都要帶 `x-goog-api-key`，沒帶回 403；一把金鑰都沒設過時維持開放（本機開發）。
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

#### POST /api/chat-file（上傳檔案問問題）

上傳一個檔案再問問題，回**文字**。跟 `/api/edit` 的差別只有一個：那支要的是圖，這支要的是字。
音訊、PDF、文件都可以，Gemini 網頁吃得下的就吃得下。

```bash
curl -X POST http://localhost:8070/api/chat-file \
  -H "Content-Type: application/json" \
  -d "{\"prompt\": \"這段音樂裡有沒有人聲？聽到哪些樂器？\",
       \"file\": \"$(base64 -w0 song.mp3)\",
       \"filename\": \"song.mp3\"}"
```

回傳跟 `/api/chat` 一樣的 `{"success", "text", "prompt", "elapsed_seconds"}`。

| 欄位 | 說明 |
|---|---|
| `file` | data URL（`data:audio/mpeg;base64,xxx`）或純 base64，最大 20 MB |
| `filename` | **一定要帶對副檔名。** Gemini 靠副檔名判型別，帶錯會被當成不明檔案，回你「請提供音檔」那種假成功 |
| `model` | 選填。留空用當下模型；音訊判讀建議指定較強的模型，Flash-Lite 聽力不可靠 |
| `timeout` | 預設 240 秒。音訊比文字慢 |

上傳完成的判斷同時認兩種訊號：圖片的 `blob:` 預覽，或畫面上出現檔名的卡片。
兩個都沒等到就直接回 `upload_timeout`，不會硬送——沒有附件的送出會拿到一句
「請提供檔案」，那種假成功最難查。

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

### 健檢

沒事的時候跑一下，看服務有沒有在偷偷爛掉（唯讀、只用標準函式庫）：

```bash
python3 scripts/checkup.py
```

一次看完服務狀態、記憶體有沒有在漏、worker 是否就緒、近 7 天成功率、
最近的失敗明細，以及自癒機制有沒有一直在救火。

> 為什麼要看這些：worker 可能「health 全綠但全滅」——輸入框都在、
> prompt 送得出去，卻再也等不到回應。這種卡法只有成功率看得出來。
> 記憶體那欄則是盯 `page.route` 造成的洩漏有沒有復發（見 PR #28）。

### 金絲雀（每天自動跑）

`checkup.py` 要有人想到才會跑。金絲雀是**主動**的：每天早上 08:00 真的生一張圖，
確認瀏覽器路還活著，壞了就開一張帶 `canary` label 的 GitHub issue。

```bash
python3 scripts/canary.py            # 檢查，壞了才開 issue
python3 scripts/canary.py --dry-run  # 只檢查
```

排在格莉奇日記那班（22:10）之前，壞掉當天還來得及處理。

> **判死活只能看 `admin.db` 那筆的 `via` 是不是 `browser`。** 2026-08-07 出圖全滅
> 卻五天沒人發現，是因為每一層都讓故障看起來像正常：`/api/generate` 失敗會被
> 付費官方 API 靜默頂替（curl 照樣 200 拿到圖）、`/api/health` 只看輸入框和工具鈕
> 所以全綠、消費端出圖失敗會安靜退回純文字而 workflow 仍然 success。
> 三層剛好疊在一起，外面完全看不出來。

## 去水印

**反向 alpha 混合**，數學還原、不做生成式修補（`src/watermark.py`，純 OpenCV）。

Gemini 在 3.5 世代換了浮水印 profile：同一顆四角星，但 alpha 剖面整個換掉
（舊版 α_max≈0.51，新版≈0.33/0.37）。**用舊 α 圖去扣新浮水印會扣過頭，在圖上留下
深色星形鬼影**——那就是 2026-07 使用者回報的「黑星」；偵測不到的則整顆白星留著。
兩種症狀同一個根因。

- **幾何**：星心固定在 (w−120s, h−120s)、邊長 48s，s = 2 若短邊 ≥1536 否則 1。
  只碰這一格，絕不在圖片其他地方動手
- **模型**：`obs = k·α·255 + (1−k·α)·orig`（logo 是白色）。k 用最小平方解，
  **模型正確時 k 應 ≈ 1** —— 實測 27 張原始圖中位數 0.96、R² 0.85~0.93
- **閘門**：k∈[0.70,1.35] 且 R²≥0.45；背景估不準時（星壓在材質交界）改用
  **輪廓能量救援**——比較移除前後星形輪廓的邊緣能量，比值 ≤0.85 才放行
- **移除**：`orig = (obs − α·255)/(1−α)`。α 已知且正確，不需要 inpaint 填補，
  所以沒有「填錯色把圖塗花」的風險
- 純 CPU、離線、每張約 0.1 秒；API 模式自動去水印，CLI 模式加 `--no-watermark`

**實測**（.11 實圖：27 張原始 / 12 張已被舊管線扣壞 / 117 個無浮水印角落當負控制）：

| 指標 | 結果 |
|---|---|
| 召回（原始浮水印） | 25/27 |
| 無浮水印處誤判 | 0/117 |
| 誤觸已損傷圖 | 0/12 |
| 清乾淨的圖再跑一次 | 0 次觸發 |

已知限制：極限放大（7 倍以上）於純色背景仍看得到約 2~3 個色階的輪廓殘影
（k 估計誤差所致，1:1 檢視看不出來）；被舊版扣壞的黑星救不回來，需重生。
不可見的 SynthID 浮水印無法移除（需 GPU + 擴散模型，本服務不處理）。

α 圖出處：[allenk/GeminiWatermarkTool](https://github.com/allenk/GeminiWatermarkTool)（MIT）
的 V2 profile 資產，見 `src/assets/gemini_v2_alpha_{36,96}.png`。

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
| `CORS_ALLOW_ORIGINS` | 允許跨來源呼叫 API 的瀏覽器前端 origin（逗號分隔，如 `https://yazelin.github.io,http://localhost:8765`）；空＝不送 CORS header | 空 |

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

### POST /api/video

產生影片（Veo）。走的是 Gemini 網頁工具選單裡的「建立影片」，跟生圖同一條瀏覽器路，
所以帳號要有這個功能才行（工具選單裡看得到「建立影片」那一項）。

```bash
curl -X POST http://localhost:8070/api/video \
  -H "Content-Type: application/json" -H "x-goog-api-key: YOUR_KEY" \
  -d '{"prompt": "一隻橘貓在夜市裡跳舞，霓虹燈，慢動作"}'
```

| 欄位 | 預設 | 說明 |
|---|---|---|
| `prompt` | — | 影片描述 |
| `timeout` | `600` | 這一支的秒數上限。Veo 要數分鐘，別照生圖的 60 秒設 |

回 `{"success", "video", "mime", "prompt", "elapsed_seconds"}`，`video` 是 base64 的 mp4。

**同步等到影片出來才回**，跟 `/api/generate` 一樣的形狀。Veo 慢很多，呼叫端的
連線 timeout 要跟著放寬；真的卡到再改成 job 式。

**影片請求只會派給做得到的帳號。** Veo 只給付費層，而多帳號部署裡不見得每個都有
（Google One 家庭群組分享的方案，帳號畫面上仍可能顯示「升級」按鈕，**所以不能用
外觀判斷**）。服務會實際打開工具選單看「建立影片」那一項在不在，結果快取起來：

```bash
curl -H "x-goog-api-key: YOUR_KEY" http://localhost:8070/api/capabilities
# {"video": {"0": false, "1": true, "2": null, "3": true}, "video_capable": [1, 3]}

# 換了帳號或選單改版之後強制重測
curl -X POST -H "x-goog-api-key: YOUR_KEY" http://localhost:8070/api/capabilities/refresh
```

`null` 是「探測時頁面卡住」，不是「沒能力」——下次還會再試。一個都沒有時
`/api/video` 回 **503** 並說明原因，不會白跑一趟。

### POST /api/music

產生音樂。走工具選單裡的「創作音樂」，跟影片同一條通用流程，只有選單項與結果元素不同。

```bash
curl -X POST http://localhost:8070/api/music \
  -H "Content-Type: application/json" -H "x-goog-api-key: YOUR_KEY" \
  -d '{"prompt": "輕快的烏克麗麗，適合寫程式時聽"}'
```

回 `{"success", "audio", "mime", "prompt", "elapsed_seconds"}`，`audio` 是 base64 音檔。

能力偵測與影片共用同一套：`/api/capabilities` 會同時報 `video` 與 `music` 兩種，
派工也各自只挑做得到的帳號。

#### 音樂參考檔（試過，不行）

`/api/music` 收得下 `file`（base64）與 `filename` 兩個可選欄位，會把音檔掛到
Create music 的輸入框上。**掛得上去，但 Lyria 不拿它當參考**，所以實務上等於白花
四五秒，正常不要用；留著這條路只是為了 Gemini 改版後好重測。

2026-08-22 實測：附了參考音檔，prompt 明講「以我上傳的這段音樂為基礎，保留它的
旋律走向，改編成慵懶的爵士鋼琴三重奏版本」，服務 200 回一首 30 秒的曲子。

驗法（這招可以複用在任何「有沒有真的參考到」的問題上）：把參考檔與產出檔中間插
一秒靜音接成一個檔，丟 `/api/chat-file` 問**比對題**——前後兩段的主旋律走向是否
有可辨識的關聯、調性與速度是否接近、後段是不是前段的改編版本，並且明講「沒關聯
就直說，不要勉強找關聯」。回答三題全否：「兩者為完全不同的音樂作品」。

（憑證在 admin DB：music `b50af238cad0` → 比對 chat_file `8fdd93194f13`。
問開放題如「這段音樂參考了什麼」它會自己編，一定要問比對題。）
