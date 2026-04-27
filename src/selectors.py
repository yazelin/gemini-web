"""Gemini 頁面 DOM selector 集中管理

Gemini 改版時只需更新此檔案的 selector 值。
最後校準日期：2026-04-27
"""

# API model name → Gemini 網頁版模式名稱
MODEL_MODE_MAP = {
    # Flash（快捷）
    "gemini-2.5-flash": "快捷",
    "gemini-3-flash": "快捷",
    "gemini-3-flash-preview": "快捷",
    "gemini-3.1-flash": "快捷",
    "gemini-3.1-flash-preview": "快捷",
    "gemini-3.1-flash-image-preview": "快捷",
    "flash": "快捷",
    # Thinking（思考型）
    "gemini-2.5-flash-thinking": "思考型",
    "gemini-3-flash-thinking": "思考型",
    "thinking": "思考型",
    # Pro
    "gemini-3-pro": "Pro",
    "gemini-3-pro-preview": "Pro",
    "gemini-3-pro-image-preview": "Pro",
    "gemini-3.1-pro": "Pro",
    "gemini-3.1-pro-preview": "Pro",
    "pro": "Pro",
}

# 圖片生成失敗時的 fallback model（Pro → Flash）
IMAGE_FALLBACK_MAP = {
    "gemini-3-pro-image-preview": "gemini-3.1-flash-image-preview",
    "gemini-3.1-pro": "gemini-3.1-flash",
    "gemini-3-pro": "gemini-3-flash",
}

SELECTORS = {
    # 輸入框 — contenteditable div（Gemini 用 Angular，class 帶動態屬性）
    "input": "[contenteditable='true']",

    # 送出按鈕（備用，主要用 Enter 鍵送出）
    "send": "button[aria-label='Send message'], button[aria-label='傳送']",

    # 回應區域 — Angular 自訂元素（新版用 message-content，舊版用 response-element）
    "response": "message-content, response-element",

    # model 回應容器（用來偵測回應開始）
    "model_response": "model-response, message-content",

    # 生成的圖片 — generated-image 容器內的 img.image
    "images": "generated-image img.image",

    # 新對話按鈕
    "new_chat": "button[aria-label='New chat'], button[aria-label='新對話']",

    # 停止生成按鈕（用來偵測生成是否完成）
    "stop_generating": "button[aria-label='Stop generating'], button[aria-label='停止產生']",

    # 下載原尺寸圖片按鈕（每張圖片旁邊的下載按鈕）
    "download_image": "download-generated-image-button button",

    # 模式挑選器
    "mode_picker": "button[aria-label='開啟模式挑選器'], button[aria-label='Open mode picker']",
    "mode_menu_item": "button[role='menuitem']",
    "mode_title": ".mode-title",

    # Tools 選單（圖片生成模式）
    "tools_button": "button:has-text('Tools'), button:has-text('工具'), button:has(img[alt='page_info'])",
    # Scope 限制在 cdk-overlay 內，避免誤抓 composer 的「上傳圖片」按鈕
    # 涵蓋「建立圖像」「建立圖片」兩種命名（Gemini 改版後文字會變動）
    "create_image": (
        ".cdk-overlay-container button:has-text('建立圖像'), "
        ".cdk-overlay-container button:has-text('建立圖片'), "
        ".cdk-overlay-container button:has-text('生成圖像'), "
        ".cdk-overlay-container button:has-text('生成圖片'), "
        ".cdk-overlay-container button:has-text('Create image'), "
        "button[role='menuitem']:has-text('建立圖像'), "
        "button[role='menuitem']:has-text('建立圖片'), "
        "button[role='menuitem']:has-text('Create image')"
    ),
}
