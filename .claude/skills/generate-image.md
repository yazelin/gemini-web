---
name: generate-image
description: 使用 Gemini 生成圖片（含繁體中文文字），自動去水印
---

# 生成圖片

使用 `gemini-image` CLI 工具生成圖片。

## 使用時機

- 用戶要求畫圖、生圖、做海報、設計圖片
- 需要生成含繁體中文文字的圖片

## 步驟

1. **理解用戶意圖**，擴寫為詳細英文 prompt
2. **執行生圖指令**：

```bash
gemini-image generate "<detailed_english_prompt>" -o <output_path> --no-watermark
```

3. **確認結果**，告知用戶圖片已生成

## Prompt 撰寫規則

- 使用英文（效果最好）
- 描述：主體、風格、構圖、色彩、氛圍
- 中文文字用引號：`with text "歡迎光臨"`
- 不要原封不動轉發用戶的話

### 範例

| 用戶說 | 你送的 prompt |
|--------|--------------|
| 畫一隻貓 | A cute fluffy cat sitting peacefully, warm soft lighting, digital art style, gentle expression |
| 做開幕海報 | A modern grand opening poster with text "盛大開幕", red and gold, confetti, professional design |
| 畫公司 logo | A minimalist corporate logo design, clean lines, modern typography, professional business style |

## 注意事項

- 耗時 30-120 秒，請提前告知用戶需要等待
- `--no-watermark` 始終加上
- 如果失敗，檢查 `gemini-image health`
