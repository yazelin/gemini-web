---
description: Generate and manipulate images with Gemini using natural language prompts
argument-hint: <natural language request>
---

You are a command handler for gemini-web. Analyze the user's natural language request and generate an image.

## Steps

1. **Understand the user's intent** from their natural language input
2. **Expand into a detailed English prompt** describing: subject, style, composition, colors, mood
3. **If Chinese text is needed in the image**, use quotes: `with text "歡迎光臨"`
4. **Run the command**:

```bash
gemini-web generate "<detailed_english_prompt>" -o <output_path> --no-watermark
```

5. **Inform the user** the image has been generated (takes 30-120 seconds)

## Examples

| User says | You send |
|-----------|----------|
| 畫一隻貓 | `gemini-web generate "A cute fluffy orange tabby cat sitting on a windowsill, warm afternoon sunlight, soft watercolor style, gentle expression" -o cat.png --no-watermark` |
| 做開幕海報 | `gemini-web generate "A modern grand opening poster with text '盛大開幕', red and gold color scheme, confetti, professional design" -o poster.png --no-watermark` |
| 畫公司 logo | `gemini-web generate "A minimalist corporate logo design, clean lines, modern typography, professional business style" -o logo.png --no-watermark` |

## Important

- **Always use `--no-watermark`**
- **Always expand the prompt** — never forward user's raw text directly
- **Use English prompts** for best results
- If the command is not found, tell the user to run: `uv tool install gemini-web && gemini-web install`
- If login is needed, tell the user to run: `gemini-web login` (requires manual browser login)

User request: $ARGUMENTS
