"""Gemini 頁面互動 — 輸入 prompt、等待生成、擷取圖片或文字回應"""
import asyncio
import logging
import time

from playwright.async_api import Page

from .selectors import MODEL_MODE_MAP, SELECTORS

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


async def switch_model(page: Page, model: str) -> bool:
    """切換 Gemini 模式（快捷/思考型/Pro）

    Args:
        model: API model name（如 "gemini-2.5-flash", "gemini-3-pro"）

    Returns:
        True 表示切換成功或不需要切換
    """
    target_mode = MODEL_MODE_MAP.get(model)
    if not target_mode:
        logger.info("未知的 model '%s'，使用預設模式", model)
        return True

    try:
        picker = await page.query_selector(SELECTORS["mode_picker"])
        if not picker:
            logger.warning("找不到模式挑選器")
            return False

        # 檢查目前模式
        current_text = (await picker.inner_text()).strip()
        if target_mode in current_text:
            logger.info("目前已是 %s 模式", target_mode)
            return True

        # 開啟模式選單
        await picker.click()
        await asyncio.sleep(0.5)

        # 找到目標選項並點擊
        menu_items = await page.query_selector_all(SELECTORS["mode_menu_item"])
        for item in menu_items:
            title_el = await item.query_selector(SELECTORS["mode_title"])
            if title_el:
                title = (await title_el.inner_text()).strip()
                if title == target_mode:
                    await item.click()
                    # 等待頁面重新載入穩定
                    await asyncio.sleep(2)
                    await page.wait_for_selector(
                        SELECTORS["input"], state="visible", timeout=15_000
                    )
                    await asyncio.sleep(1)
                    logger.info("已切換至 %s 模式", target_mode)
                    return True

        # 沒找到，關閉選單
        await page.keyboard.press("Escape")
        logger.warning("找不到模式 '%s'", target_mode)
        return False

    except Exception as e:
        logger.warning("切換模式失敗：%s", e)
        return False


async def generate_image(page: Page, prompt: str, timeout: int = 60) -> dict:
    """在 Gemini 頁面輸入 prompt 並擷取生成的圖片

    Returns:
        {"success": True, "images": [...], "prompt": ..., "elapsed_seconds": ...}
        或 {"success": False, "error": ..., "message": ...}
    """
    # 確保 prompt 明確要求生圖（避免 Gemini 當成搜尋）
    prompt_lower = prompt.lower().strip()
    needs_prefix = not any(kw in prompt_lower for kw in [
        "draw", "paint", "generate", "create an image", "create a picture",
        "make an image", "make a picture", "illustrate",
        "畫", "繪", "生成圖", "生成一張", "做一張", "設計",
    ])
    if needs_prefix:
        prompt = f"Generate an image: {prompt}"

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

        # 1.5 關閉可能的 overlay 彈窗（如 Deep Research）
        try:
            await page.evaluate("""() => {
                // 移除所有 cdk-overlay-container 的內容
                document.querySelectorAll('.cdk-overlay-container').forEach(el => {
                    el.innerHTML = '';
                });
                // 點擊 ESC 關閉可能的 dialog
            }""")
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.5)
        except Exception:
            pass

        # 2. 輸入 prompt（用 JS 直接寫入 + 模擬 Ctrl+V 貼上事件）
        await input_el.click()
        await asyncio.sleep(0.3)
        # 透過 JS 模擬 clipboard paste（繞過 headless clipboard 限制）
        await input_el.evaluate("""(el, text) => {
            el.focus();
            // 建立 paste 事件，帶上文字資料
            const dt = new DataTransfer();
            dt.setData('text/plain', text);
            const pasteEvent = new ClipboardEvent('paste', {
                clipboardData: dt,
                bubbles: true,
                cancelable: true,
            });
            el.dispatchEvent(pasteEvent);
            // 備用：如果 paste 事件沒觸發，直接設定 innerText
            if (!el.textContent || el.textContent.trim().length === 0) {
                el.innerText = text;
                el.dispatchEvent(new Event('input', { bubbles: true }));
            }
        }""", prompt)
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

        # 7. 透過「下載原尺寸圖片」按鈕取得完整圖片
        import base64 as _b64
        download_btns = await page.query_selector_all(SELECTORS["download_image"])
        logger.info("找到 %d 個圖片元素，%d 個下載按鈕", len(img_els), len(download_btns))

        images = []

        # 優先用下載按鈕（原尺寸，去水印工具能正確處理）
        if download_btns:
            for i, btn in enumerate(download_btns):
                try:
                    logger.info("圖片 %d：嘗試點擊下載按鈕...", i)
                    # 先 hover 圖片讓 on-hover-button 顯示
                    if img_els and i < len(img_els):
                        await img_els[i].hover()
                        await asyncio.sleep(0.5)
                    await btn.hover()
                    await asyncio.sleep(0.3)
                    # 用 JS click（更可靠）+ 長 timeout（伺服器需要時間生成原尺寸）
                    async with page.expect_download(timeout=240_000) as download_info:
                        await page.evaluate("btn => btn.click()", btn)
                    download = await download_info.value
                    logger.info("圖片 %d：下載事件觸發，等待檔案寫入...", i)
                    # 讀取下載的檔案（path() 會等到下載完成）
                    dl_path = await download.path()
                    if dl_path:
                        from pathlib import Path
                        raw = Path(dl_path).read_bytes()
                        b64 = _b64.b64encode(raw).decode("ascii")
                        # 偵測格式
                        suggested = download.suggested_filename or ""
                        ct = "image/jpeg" if suggested.endswith(".jpg") or suggested.endswith(".jpeg") else "image/png"
                        images.append(f"data:{ct};base64,{b64}")
                        logger.info("圖片 %d 下載原尺寸成功，%d bytes（%s）", i, len(raw), suggested)
                except Exception as e:
                    logger.warning("圖片 %d 下載按鈕失敗：%s，改用 img src", i, e)

        # 備用：直接從 img src 下載（可能是縮圖）
        if not images:
            for i, img_el in enumerate(img_els):
                try:
                    src = await img_el.get_attribute("src")
                    if not src:
                        continue
                    if src.startswith("data:image"):
                        images.append(src)
                    elif src.startswith("http"):
                        resp = await page.context.request.get(src)
                        if resp.ok:
                            body = await resp.body()
                            content_type = resp.headers.get("content-type", "image/png")
                            b64 = _b64.b64encode(body).decode("ascii")
                            images.append(f"data:{content_type};base64,{b64}")
                            logger.info("圖片 %d 從 src 下載，%d bytes", i, len(body))
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


async def chat(page: Page, prompt: str, timeout: int = 60) -> dict:
    """在 Gemini 頁面輸入 prompt 並擷取文字回應

    Returns:
        {"success": True, "text": "...", "prompt": ..., "elapsed_seconds": ...}
        或 {"success": False, "error": ..., "message": ...}
    """
    start = time.time()

    try:
        # 1. 確認輸入框就緒
        input_el = await page.wait_for_selector(
            SELECTORS["input"], state="visible", timeout=15_000
        )
        if not input_el:
            return _error("browser_error", "找不到輸入框")
        await asyncio.sleep(1)

        # 1.5 關閉可能的 overlay 彈窗
        try:
            await page.evaluate("""() => {
                document.querySelectorAll('.cdk-overlay-container').forEach(el => {
                    el.innerHTML = '';
                });
            }""")
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.5)
        except Exception:
            pass

        # 2. 輸入 prompt（用 JS 模擬 clipboard paste）
        await input_el.click()
        await asyncio.sleep(0.3)
        await input_el.evaluate("""(el, text) => {
            el.focus();
            const dt = new DataTransfer();
            dt.setData('text/plain', text);
            const pasteEvent = new ClipboardEvent('paste', {
                clipboardData: dt,
                bubbles: true,
                cancelable: true,
            });
            el.dispatchEvent(pasteEvent);
            if (!el.textContent || el.textContent.trim().length === 0) {
                el.innerText = text;
                el.dispatchEvent(new Event('input', { bubbles: true }));
            }
        }""", prompt)
        await asyncio.sleep(1)

        # 3. 送出
        await page.keyboard.press("Enter")
        logger.info("已送出 chat prompt：%s", prompt[:50])

        # 4. 等待回應完成：等 model-response 出現，再等文字穩定
        logger.info("等待 Gemini 回應...")
        wait_ms = max((timeout - 10), 30) * 1000
        try:
            await page.wait_for_selector(
                SELECTORS["model_response"], state="visible", timeout=wait_ms
            )
            logger.info("偵測到 model-response")
        except Exception:
            elapsed = round(time.time() - start, 1)
            return _error("no_response", "Gemini 未回應", elapsed)

        # 等文字穩定（連續 2 次內容不變 = 完成）
        # 上限 30 秒。已經偵測到 model-response 之後,Gemini 串流文字通常 5-15 秒
        # 內結束。若頁面有動態元素 (時間戳/廣告) 導致 text 一直變,30 秒後強制跳出。
        prev_text = ""
        stable_count = 0
        for _ in range(30):
            await asyncio.sleep(1)
            response_els = await page.query_selector_all(SELECTORS["response"])
            if not response_els:
                continue
            text = (await response_els[-1].inner_text()).strip()
            if text and text == prev_text:
                stable_count += 1
                if stable_count >= 2:
                    break
            else:
                stable_count = 0
                prev_text = text

        # 5. 提取文字回應
        response_els = await page.query_selector_all(SELECTORS["response"])
        if not response_els:
            elapsed = round(time.time() - start, 1)
            return _error("no_response", "Gemini 未回應", elapsed)

        last_response = response_els[-1]
        text = (await last_response.inner_text()).strip()

        if not text:
            elapsed = round(time.time() - start, 1)
            return _error("no_response", "Gemini 回應為空", elapsed)

        # 6. 檢查是否被拒絕
        for phrase in _BLOCK_PHRASES:
            if phrase.lower() in text.lower():
                elapsed = round(time.time() - start, 1)
                return {
                    "success": False,
                    "error": "content_blocked",
                    "message": text[:200],
                    "elapsed_seconds": elapsed,
                }

        elapsed = round(time.time() - start, 1)
        return {
            "success": True,
            "text": text,
            "prompt": prompt,
            "elapsed_seconds": elapsed,
        }

    except asyncio.TimeoutError:
        elapsed = round(time.time() - start, 1)
        return _error("timeout", f"回應超時（{timeout}秒）", elapsed)
    except Exception as e:
        elapsed = round(time.time() - start, 1)
        logger.exception("Gemini chat 發生錯誤")
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
