# Gemini Image — AI Agent 使用指引

本工具讓 AI Agent 能生成含繁體中文文字的圖片，並自動去除水印。

## 前置需求

確認 `gemini-image` 已安裝且已登入：

```bash
gemini-image health  # API 模式：檢查服務是否運行
# 或
which gemini-image   # CLI 模式：確認指令存在
```

## 使用方式

### CLI 模式（推薦用於 Agent Tool）

```bash
gemini-image generate "<prompt>" -o <output_path> --no-watermark
```

- `<prompt>`：圖片描述，**建議用英文**效果最好
- `-o`：輸出檔案路徑（支援 .png / .jpg）
- `--no-watermark`：移除 Gemini 水印（建議始終加上）
- 耗時約 30-120 秒

### HTTP API 模式

```bash
curl -X POST http://localhost:8070/api/generate \
  -H "Content-Type: application/json" \
  -d '{"prompt": "<prompt>"}'
```

回傳 JSON：`{"success": true, "images": ["data:image/png;base64,..."]}`

## Prompt 撰寫規則

**重要：不要原封不動轉發用戶的請求。** 你應該：

1. 理解用戶意圖
2. 擴寫為詳細的英文 prompt
3. 描述主體、風格、構圖、色彩、氛圍
4. 如果需要圖片中出現中文文字，用引號標明：`with text "歡迎光臨"`

### 範例

用戶說「畫一隻貓」→ 你應該送：
```
A cute fluffy orange tabby cat sitting on a windowsill, warm afternoon sunlight streaming in, cozy atmosphere, soft watercolor illustration style, gentle expression with bright curious eyes
```

用戶說「做一張開幕海報」→ 你應該送：
```
A modern grand opening poster design with bold typography showing text "盛大開幕" at the top, celebratory confetti and ribbons, red and gold color scheme, professional marketing design, clean layout
```

## 錯誤處理

| 錯誤 | 意義 | 建議 |
|------|------|------|
| `content_blocked` | Gemini 拒絕生成 | 換一個不涉及敏感內容的 prompt |
| `no_image` | 沒有生成圖片 | prompt 更具體，確認包含「draw」「generate」等關鍵字 |
| `timeout` | 生成超時 | 稍後再試 |
| `browser_error` | 瀏覽器問題 | 檢查 `gemini-image health` |

## 安裝（如果尚未安裝）

```bash
uv tool install gemini-image && gemini-image install
gemini-image login  # 需要有桌面環境，手動登入 Google
```
