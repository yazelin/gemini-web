"""Gemini 頁面 DOM selector 集中管理

Gemini 改版時只需更新此檔案的 selector 值。
最後校準日期：2026-06-25（Gemini 把「工具」併進「上傳與工具」單一鈕，
「建立圖片」改名「建立圖像」且為 menuitemcheckbox）
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

    # 上傳檔案按鈕 — 先點開選單，選單裡再選「上傳檔案」才會觸發 file dialog
    "upload_button": (
        "button.upload-card-button[aria-label='開啟上傳檔案選單'], "
        "button.upload-card-button[aria-label='Open upload file menu']"
    ),

    # 上傳檔案選單裡的「上傳檔案」項（從電腦選檔），點下去才會開 file chooser
    # 同 menu 還有「加入雲端硬碟檔案」「相簿」「NotebookLM」三個其他選項，要避開
    "upload_menu_item_local": (
        ".cdk-overlay-container button[role='menuitem']:has-text('上傳檔案'), "
        ".cdk-overlay-container button[role='menuitem']:has-text('Upload files')"
    ),

    # 2026-07：帳號第一次用上傳功能時，點「上傳檔案」會先跳一個同意條款的對話框
    # （overlay 多出「取消 / 同意」兩顆），按下同意才會開 file dialog。worker 1/3
    # 的 edit 全滅就是卡在這。限定在 overlay 內、且避開「取消」。
    "upload_consent_accept": (
        ".cdk-overlay-container button:has-text('同意'), "
        ".cdk-overlay-container button:has-text('接受'), "
        ".cdk-overlay-container button:has-text('繼續'), "
        ".cdk-overlay-container button:has-text('I agree'), "
        ".cdk-overlay-container button:has-text('Agree'), "
        ".cdk-overlay-container button:has-text('Accept'), "
        ".cdk-overlay-container button:has-text('Continue')"
    ),

    # 2026-07：有些帳號點「上傳檔案」後不是直接開 file dialog，而是再展開一層
    # 子選單（從電腦上傳 / 從裝置上傳 / 相簿…）。這條抓子選單裡的本機上傳項；
    # 排除「雲端硬碟」「相簿」「NotebookLM」那幾個會導去別處的。
    "upload_submenu_item_device": (
        ".cdk-overlay-container [role='menuitem']:has-text('從電腦'), "
        ".cdk-overlay-container [role='menuitem']:has-text('從裝置'), "
        ".cdk-overlay-container [role='menuitem']:has-text('本機'), "
        ".cdk-overlay-container [role='menuitem']:has-text('上傳檔案'), "
        ".cdk-overlay-container [role='menuitem']:has-text('From computer'), "
        ".cdk-overlay-container [role='menuitem']:has-text('Upload from device'), "
        ".cdk-overlay-container [role='menuitem']:has-text('Upload files')"
    ),

    # 上傳完成後的縮圖預覽
    # Gemini 上傳預覽圖通常用 blob: URL，掛在 input bar 區域
    "upload_preview_blob": "img[src^='blob:']",

    # Tools 選單（圖片生成模式）
    # 2026-06：Gemini 把「工具」併進「上傳與工具」單一 icon 鈕（innerText 空、
    # 只有 aria-label），所以 :has-text 抓不到，必須用 aria-label。舊文字鈕保留當 fallback。
    "tools_button": (
        "button[aria-label='上傳與工具'], "
        "button[aria-label='Upload files & more'], "
        "button[aria-label='Upload & tools'], "
        "button:has-text('Tools'), button:has-text('工具'), "
        "button:has(img[alt='page_info'])"
    ),
    # Scope 限制在 cdk-overlay 內，避免誤抓 composer 的「上傳圖片」按鈕
    # 涵蓋「建立圖像」「建立圖片」兩種命名；2026-06 起該項是 menuitemcheckbox（非 button）
    "create_image": (
        ".cdk-overlay-container [role='menuitemcheckbox']:has-text('建立圖像'), "
        ".cdk-overlay-container [role='menuitemcheckbox']:has-text('建立圖片'), "
        ".cdk-overlay-container [role='menuitemcheckbox']:has-text('Create image'), "
        ".cdk-overlay-container button:has-text('建立圖像'), "
        ".cdk-overlay-container button:has-text('建立圖片'), "
        ".cdk-overlay-container button:has-text('生成圖像'), "
        ".cdk-overlay-container button:has-text('生成圖片'), "
        ".cdk-overlay-container button:has-text('Create image'), "
        "button[role='menuitem']:has-text('建立圖像'), "
        "button[role='menuitem']:has-text('建立圖片'), "
        "button[role='menuitem']:has-text('Create image')"
    ),

    # 「建立影片」——跟「建立圖像」同一個工具選單裡的另一項（Veo）。
    # 選擇器寫法照抄圖片那條：scope 在 cdk-overlay 內，涵蓋 menuitemcheckbox
    # 與 button 兩種角色、中英文兩種命名。
    "create_video": (
        ".cdk-overlay-container [role='menuitemcheckbox']:has-text('建立影片'), "
        ".cdk-overlay-container [role='menuitemcheckbox']:has-text('製作影片'), "
        ".cdk-overlay-container [role='menuitemcheckbox']:has-text('Create video'), "
        ".cdk-overlay-container [role='menuitemcheckbox']:has-text('Make a video'), "
        ".cdk-overlay-container button:has-text('建立影片'), "
        ".cdk-overlay-container button:has-text('製作影片'), "
        ".cdk-overlay-container button:has-text('生成影片'), "
        ".cdk-overlay-container button:has-text('Create video'), "
        "button[role='menuitem']:has-text('建立影片'), "
        "button[role='menuitem']:has-text('製作影片'), "
        "button[role='menuitem']:has-text('Create video')"
    ),

    # 生成出來的影片。實際 DOM 還沒實地看過，所以候選寫寬一點；
    # 抓不到時 generate_video 會 dump_page_state 存證，照那份修這一條。
    "videos": (
        "generated-video video, "
        "video-container video, "
        "message-content video, "
        "model-response video, "
        "video[src], "
        "video source[src]"
    ),

    # 「創作音樂」——同一個工具選單裡的另一項。四個帳號實測都有。
    "create_music": (
        ".cdk-overlay-container [role='menuitemcheckbox']:has-text('創作音樂'), "
        ".cdk-overlay-container [role='menuitemcheckbox']:has-text('建立音樂'), "
        ".cdk-overlay-container [role='menuitemcheckbox']:has-text('Create music'), "
        ".cdk-overlay-container [role='menuitemcheckbox']:has-text('Make music'), "
        ".cdk-overlay-container button:has-text('創作音樂'), "
        ".cdk-overlay-container button:has-text('建立音樂'), "
        ".cdk-overlay-container button:has-text('Create music'), "
        "button[role='menuitem']:has-text('創作音樂'), "
        "button[role='menuitem']:has-text('建立音樂'), "
        "button[role='menuitem']:has-text('Create music')"
    ),

    # 生成出來的音樂。**不是 <audio> 元素** —— Gemini 給的是一張自訂的專輯卡片，
    # 音訊元素要點下播放才會出現。2026-08-22 實測頁面上一個 <audio> 都沒有，
    # 診斷的按鈕清單裡才看到 aria-label 是「播放音樂」與「下載歌曲」。
    "audios": (
        "button[aria-label='播放音樂'], "
        "button[aria-label*='播放音樂'], "
        "button[aria-label*='Play music'], "
        "audio[src]"
    ),

    # 點「下載歌曲」之後還會再開一個選單，兩個選項：
    #   「影片 / 附封面圖片的音訊」與「僅音訊 / MP3 音軌」
    # 要的是後者。不點這一步的話 expect_download 會空等到逾時。
    "download_audio_format": (
        ".cdk-overlay-container button:has-text('僅音訊'), "
        ".cdk-overlay-container button:has-text('MP3'), "
        ".cdk-overlay-container [role='menuitem']:has-text('僅音訊'), "
        ".cdk-overlay-container [role='menuitem']:has-text('MP3'), "
        "button:has-text('僅音訊'), "
        "button:has-text('MP3 音軌')"
    ),

    "download_audio": (
        "button[aria-label='下載歌曲'], "
        "button[aria-label*='下載歌曲'], "
        "button[aria-label*='下載音樂'], "
        "button[aria-label*='Download song'], "
        "button[aria-label*='Download audio']"
    ),

    # 影片的下載鈕，同樣是候選清單
    "download_video": (
        "download-generated-video-button button, "
        "button[aria-label*='下載影片'], "
        "button[aria-label*='Download video'], "
        "generated-video button[aria-label*='下載'], "
        "generated-video button[aria-label*='Download']"
    ),
}
