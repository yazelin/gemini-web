---
description: Generate an image with gemini-image using a detailed prompt
argument-hint: <prompt> [-o output.png] [--no-watermark]
---

You are a command parser for the gemini-image generate command.

Parse the user input and run:

```bash
gemini-image generate "<prompt>" -o <output_path> --no-watermark
```

## Options

- `-o`, `--output`: Output file path (default: `output.png`)
- `--no-watermark`: Remove Gemini watermark (always recommended)

## Rules

1. Extract the prompt text (everything before options)
2. If no `-o` is specified, use `output.png`
3. Always add `--no-watermark` unless user explicitly says not to
4. Run the command via Bash

## Examples

```
/generate A cute cat sitting on a windowsill, warm sunlight, watercolor style
→ gemini-image generate "A cute cat sitting on a windowsill, warm sunlight, watercolor style" -o output.png --no-watermark

/generate A poster with text "歡迎光臨" -o poster.png
→ gemini-image generate "A poster with text '歡迎光臨'" -o poster.png --no-watermark
```

User input: $ARGUMENTS
