"""Gemini 頁面 DOM selector 集中管理

Gemini 改版時只需更新此檔案的 selector 值。
實際值需在開發時開啟 Gemini 頁面用 DevTools 確認。
"""

SELECTORS = {
    # 輸入框 — Gemini 使用 contenteditable div 或 rich text editor
    "input": "div.ql-editor[contenteditable='true']",

    # 送出按鈕
    "send": "button.send-button, button[aria-label='Send message']",

    # 回應區域 — 最後一個回應訊息容器
    "response": "message-content",

    # 生成的圖片 — 回應區域內的 img 標籤
    "images": "message-content img",

    # 新對話按鈕
    "new_chat": "button[aria-label='New chat']",

    # 停止生成按鈕（用來偵測生成是否完成）
    "stop_generating": "button[aria-label='Stop generating']",
}
