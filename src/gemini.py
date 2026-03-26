"""Gemini 頁面互動 — 輸入 prompt、等待生成、擷取圖片"""
import asyncio
import logging
import time

from playwright.async_api import Page

from .selectors import SELECTORS

logger = logging.getLogger(__name__)

# 瀏覽器端 JS：取得圖片 src 資訊（用於 debug）
_IMG_DEBUG_JS = """
(img) => {
    return {
        src: img.src ? img.src.substring(0, 100) : null,
        width: img.naturalWidth,
        height: img.naturalHeight,
        tagName: img.tagName,
        className: img.className,
    };
}
"""

# 瀏覽器端 JS：多策略擷取圖片為 base64
_IMG_TO_BASE64_JS = """
(img) => {
    return new Promise(async (resolve, reject) => {
        const src = img.src || '';

        // 策略 1：src 已經是 data URL → 直接回傳
        if (src.startsWith('data:image')) {
            resolve(src);
            return;
        }

        // 策略 2：用 fetch 取得 blob → 轉 base64（適用 blob: 和 https: URL）
        try {
            const resp = await fetch(src);
            const blob = await resp.blob();
            const reader = new FileReader();
            reader.onloadend = () => resolve(reader.result);
            reader.onerror = () => reject('FileReader 失敗');
            reader.readAsDataURL(blob);
            return;
        } catch (e) {
            // fetch 失敗，嘗試 canvas
        }

        // 策略 3：canvas 繪製（備用）
        try {
            const canvas = document.createElement('canvas');
            canvas.width = img.naturalWidth || img.width;
            canvas.height = img.naturalHeight || img.height;
            const ctx = canvas.getContext('2d');
            ctx.drawImage(img, 0, 0);
            resolve(canvas.toDataURL('image/png'));
        } catch (e) {
            reject('所有擷取策略都失敗：' + e.message);
        }
    });
}
"""

# 拒絕生圖的常見文字片段
_BLOCK_PHRASES = [
    "I can't generate",
    "I'm not able to",
    "無法生成",
    "I can't create",
    "isn't something I can",
    "against my safety",
    "violates my safety",
]


async def generate_image(page: Page, prompt: str, timeout: int = 60) -> dict:
    """在 Gemini 頁面輸入 prompt 並擷取生成的圖片

    Returns:
        {"success": True, "images": [...], "prompt": ..., "elapsed_seconds": ...}
        或 {"success": False, "error": ..., "message": ...}
    """
    start = time.time()

    try:
        # 1. 確認輸入框就緒（等久一點，頁面可能剛導航完）
        input_el = await page.wait_for_selector(
            SELECTORS["input"], state="visible", timeout=15_000
        )
        if not input_el:
            return _error("browser_error", "找不到輸入框")
        # 確保頁面完全就緒
        await asyncio.sleep(1)

        # 2. 清空並輸入 prompt
        await input_el.click()
        await asyncio.sleep(0.3)
        # 用 keyboard.type 模擬逐字輸入（比 fill 更可靠）
        await page.keyboard.type(prompt, delay=30)
        await asyncio.sleep(1)

        # 3. 送出（按 Enter）
        await page.keyboard.press("Enter")
        logger.info("已送出 prompt：%s", prompt[:50])

        # 4. 等待回應完成
        #    策略 A：等��生成的圖片出現（最可靠）
        #    策略 B：���待 stop 按鈕消失（備用）
        logger.info("等待 Gemini 回應...")
        try:
            # 先等圖片出現（預留 10 秒給後續處理，避免跟 queue timeout 撞）
            wait_ms = max((timeout - 10), 30) * 1000
            await page.wait_for_selector(
                SELECTORS["images"], state="visible", timeout=wait_ms
            )
            logger.info("偵測到圖片元素")
            # 圖片出現後再等幾秒確保完全載入（含 class .loaded）
            await asyncio.sleep(3)
        except Exception:
            # 圖片沒出現，可能是文字回覆或被拒絕，也等一下再檢查
            logger.info("未偵測到圖片，等待回應文字...")
            await asyncio.sleep(5)

        # 5. 檢查是否被拒絕
        response_els = await page.query_selector_all(SELECTORS["response"])
        if response_els:
            last_response = response_els[-1]
            text = (await last_response.inner_text()).strip()
            for phrase in _BLOCK_PHRASES:
                if phrase.lower() in text.lower():
                    elapsed = round(time.time() - start, 1)
                    return {
                        "success": False,
                        "error": "content_blocked",
                        "message": text[:200],
                        "elapsed_seconds": elapsed,
                    }

        # 6. 擷取圖片
        img_els = await page.query_selector_all(SELECTORS["images"])
        if not img_els:
            # 可能回了文字而非圖片
            text = ""
            if response_els:
                text = (await response_els[-1].inner_text()).strip()
            elapsed = round(time.time() - start, 1)
            return {
                "success": False,
                "error": "no_image",
                "message": f"Gemini 未生成圖片。回應內容：{text[:200]}",
                "elapsed_seconds": elapsed,
            }

        # 7. 將圖片轉為 base64
        #    用 Playwright API request 直接下載圖片 URL（繞過 CORS）
        logger.info("找到 %d 個圖片元素", len(img_els))
        images = []
        for i, img_el in enumerate(img_els):
            try:
                src = await img_el.get_attribute("src")
                logger.info("圖片 %d src：%s", i, src[:100] if src else "None")

                if not src:
                    continue

                if src.startswith("data:image"):
                    # 已經是 base64
                    images.append(src)
                    logger.info("圖片 %d 已是 base64", i)
                elif src.startswith("http"):
                    # 用 Playwright 的 request context 下載（帶 cookies，繞 CORS）
                    import base64
                    resp = await page.context.request.get(src)
                    if resp.ok:
                        body = await resp.body()
                        content_type = resp.headers.get("content-type", "image/png")
                        b64 = base64.b64encode(body).decode("ascii")
                        data_url = f"data:{content_type};base64,{b64}"
                        images.append(data_url)
                        logger.info("圖片 %d 下載成功，%d bytes", i, len(body))
                    else:
                        logger.warning("圖片 %d 下載失敗：HTTP %d", i, resp.status)
                else:
                    logger.warning("圖片 %d 未知 src 格式：%s", i, src[:80])
            except Exception as e:
                logger.warning("圖片 %d 擷取失敗：%s", i, e)

        elapsed = round(time.time() - start, 1)

        if not images:
            return _error("browser_error", "圖片元素存在但無法擷取（詳見 server log）", elapsed)

        return {
            "success": True,
            "images": images,
            "prompt": prompt,
            "elapsed_seconds": elapsed,
        }

    except asyncio.TimeoutError:
        elapsed = round(time.time() - start, 1)
        return _error("timeout", f"生成超時（{timeout}秒）", elapsed)
    except Exception as e:
        elapsed = round(time.time() - start, 1)
        logger.exception("Gemini 互動發生錯誤")
        return _error("browser_error", str(e), elapsed)


async def new_chat(page: Page) -> bool:
    """重置 Gemini 對話狀態 — 直接導航到首頁（最可靠）"""
    try:
        await page.goto("https://gemini.google.com/app", wait_until="domcontentloaded")
        # 等待輸入框出現，確認頁面就緒
        await page.wait_for_selector(
            SELECTORS["input"], state="visible", timeout=15_000
        )
        await asyncio.sleep(1)
        logger.info("已重置對話（導航至首頁）")
        return True
    except Exception as e:
        logger.warning("重置對話失敗：%s", e)
        return False


def _error(error: str, message: str, elapsed: float = 0) -> dict:
    return {
        "success": False,
        "error": error,
        "message": message,
        "elapsed_seconds": elapsed,
    }
