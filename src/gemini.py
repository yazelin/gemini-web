"""Gemini 頁面互動 — 輸入 prompt、等待生成、擷取圖片或文字回應"""
import asyncio
import json
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

        # 1.7 點擊 Tools → Create image 進入圖片生成模式
        switched_to_create_image = False
        try:
            # Debug: 列出頁面上所有按鈕
            all_btns = await page.evaluate("""() => {
                return Array.from(document.querySelectorAll('button')).map(b => ({
                    text: b.innerText.trim().substring(0, 50),
                    aria: (b.getAttribute('aria-label') || '').substring(0, 50),
                })).filter(b => b.text || b.aria);
            }""")
            logger.info("頁面按鈕: %s", json.dumps(all_btns, ensure_ascii=False)[:500])

            # 等 Tools 按鈕出現（頁面載入後可能需要幾秒）
            tools_btn = await page.wait_for_selector(
                SELECTORS["tools_button"], state="visible", timeout=8_000
            )
            if tools_btn:
                await tools_btn.click()
                logger.info("已點擊 Tools 按鈕，等待選單...")
                await asyncio.sleep(1.5)
                # 等 Create image 按鈕出現
                create_img_btn = await page.wait_for_selector(
                    SELECTORS["create_image"], state="visible", timeout=5_000
                )
                if create_img_btn:
                    # 縮短 click timeout，避免 selector 過時時卡 30 秒重試
                    await create_img_btn.click(timeout=5_000)
                    await asyncio.sleep(2)
                    logger.info("已切換至 Create image 模式")
                    switched_to_create_image = True
                    # 重新取得輸入框（模式切換後可能會刷新）
                    input_el = await page.wait_for_selector(
                        SELECTORS["input"], state="visible", timeout=10_000
                    )
                else:
                    logger.warning("找不到 Create image 按鈕，使用 prefix fallback")
                    await page.keyboard.press("Escape")
                    await asyncio.sleep(0.5)
        except Exception as e:
            logger.warning("切換 Create image 模式失敗：%s，使用 prefix fallback", e)
            # 確保關閉可能開啟的選單
            try:
                await page.keyboard.press("Escape")
                await asyncio.sleep(0.3)
            except Exception:
                pass

        if not switched_to_create_image:
            prompt = f"Generate an image: {prompt}"

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
            # 圖片出現後等 3 秒讓 Gemini 完整載入 (含 .loaded class)
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
                    # 240 秒 download timeout (Gemini Pro 高解析度原圖伺服器
                    # 偶爾需要 > 30 秒生成。openclaw 內建 image_generate 工具的
                    # 60 秒上限我們改用自製 skill 繞開,所以這裡可以給寬鬆時間)
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


async def edit_image(
    page: Page,
    prompt: str,
    reference_image_b64: str,
    timeout: int = 120,
) -> dict:
    """以參考圖編輯模式生成圖片：上傳 reference 圖 + 文字 prompt → 編輯後的新圖

    Args:
        page: 已開啟 Gemini 對話的 Playwright Page
        prompt: 編輯指令（建議英文，例：「change the dog's color to black」）
        reference_image_b64: 參考圖。可以是 data:image/...;base64,xxx 或純 base64 字串
        timeout: 整體 timeout 秒數

    Returns:
        同 generate_image：
        {"success": True, "images": [...], "prompt": ..., "elapsed_seconds": ...}
        或 {"success": False, "error": ..., "message": ...}
    """
    import base64 as _b64
    import os
    import tempfile

    start = time.time()

    # 將 reference_image 寫到暫存檔，給 Playwright set_input_files 用
    raw = reference_image_b64
    if raw.startswith("data:"):
        # data:image/jpeg;base64,xxx → 拆出 b64 部分
        try:
            _, raw = raw.split(",", 1)
        except ValueError:
            return _error("invalid_input", "reference_image 格式錯誤")
    try:
        img_bytes = _b64.b64decode(raw)
    except Exception:
        return _error("invalid_input", "reference_image 不是有效的 base64")
    if len(img_bytes) > 10 * 1024 * 1024:
        return _error("invalid_input", "reference_image 超過 10 MB")

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".png", prefix="gemini_ref_")
    os.close(tmp_fd)
    try:
        with open(tmp_path, "wb") as f:
            f.write(img_bytes)

        # 1. 確認輸入框就緒
        input_el = await page.wait_for_selector(
            SELECTORS["input"], state="visible", timeout=15_000
        )
        if not input_el:
            return _error("browser_error", "找不到輸入框")
        await asyncio.sleep(1)

        # 1.5 清 overlay（可能殘留 Deep Research 之類）
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

        # 1.7 image edit 不切 Create image 模式
        # 原因：Create image 模式的 UI 沒有上傳檔案選單；Banana 模型在普通
        # chat 模式下接收「圖片 + prompt」會自動做 image-to-image edit
        switched_to_create_image = False  # 標記給後段 prompt 處理用

        # 2. 上傳 reference image
        # 流程：點 upload button → 選單彈出 → 點「上傳檔案」menuitem → file chooser
        # 兩段 click 都包在 expect_file_chooser 內，由 Playwright 攔截 file dialog
        logger.info("點擊上傳按鈕 + 選單，等 file chooser...")
        try:
            async with page.expect_file_chooser(timeout=15_000) as fc_info:
                await page.click(SELECTORS["upload_button"])
                # 等選單 render（mat-menu Angular 動畫約 200ms）
                await asyncio.sleep(0.8)
                await page.click(SELECTORS["upload_menu_item_local"])
            file_chooser = await fc_info.value
            await file_chooser.set_files(tmp_path)
            logger.info("已 set_files：%s（%d bytes）", tmp_path, len(img_bytes))
        except Exception as e:
            elapsed = round(time.time() - start, 1)
            return _error(
                "upload_failed",
                f"上傳 reference image 失敗：{e}",
                elapsed,
            )

        # 3. 等預覽圖出現（blob: img 是 Gemini 上傳完成的指標）
        try:
            await page.wait_for_function(
                """() => {
                    const imgs = Array.from(document.querySelectorAll('img'));
                    return imgs.some(img => {
                        const src = img.src || '';
                        return src.startsWith('blob:') && (img.naturalWidth || 0) > 30;
                    });
                }""",
                timeout=20_000,
            )
            logger.info("reference image 預覽已出現")
            await asyncio.sleep(1)  # 多等一點讓 UI stabilize
        except Exception:
            elapsed = round(time.time() - start, 1)
            return _error(
                "upload_timeout",
                "上傳檔案後 20 秒內沒看到預覽，可能上傳未成功",
                elapsed,
            )

        # 4. 輸入 prompt（同 generate_image 的 paste pattern）
        if not prompt.strip():
            prompt = "edit this image"
        # Create image 沒切到時前綴提示，否則直接送 prompt
        final_prompt = prompt if switched_to_create_image else f"Edit this image: {prompt}"

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
        }""", final_prompt)
        await asyncio.sleep(1)

        # 5. 送出
        await page.keyboard.press("Enter")
        logger.info("已送出 edit prompt：%s", final_prompt[:80])

        # 6. 等回應出現 — 同 generate_image 的策略
        try:
            wait_ms = max((timeout - 10), 30) * 1000
            await page.wait_for_selector(
                SELECTORS["images"], state="visible", timeout=wait_ms
            )
            logger.info("偵測到圖片元素")
            await asyncio.sleep(3)
        except Exception:
            logger.info("未偵測到圖片，等待回應文字...")
            await asyncio.sleep(5)

        # 7. 檢查是否被拒絕
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

        # 8. 擷取生成的編輯圖（同 generate_image 抓圖邏輯：先試下載按鈕、再退 img.src）
        import base64 as _b64x
        img_els = await page.query_selector_all(SELECTORS["images"])
        if not img_els:
            text = ""
            if response_els:
                text = (await response_els[-1].inner_text()).strip()
            elapsed = round(time.time() - start, 1)
            return {
                "success": False,
                "error": "no_image",
                "message": f"Gemini 未生成編輯後的圖片。回應內容：{text[:200]}",
                "elapsed_seconds": elapsed,
            }

        download_btns = await page.query_selector_all(SELECTORS["download_image"])
        logger.info("找到 %d 個圖片元素，%d 個下載按鈕", len(img_els), len(download_btns))

        images = []
        if download_btns:
            for i, btn in enumerate(download_btns):
                try:
                    if img_els and i < len(img_els):
                        await img_els[i].hover()
                        await asyncio.sleep(0.5)
                    await btn.hover()
                    await asyncio.sleep(0.3)
                    async with page.expect_download(timeout=240_000) as download_info:
                        await page.evaluate("btn => btn.click()", btn)
                    download = await download_info.value
                    dl_path = await download.path()
                    if dl_path:
                        from pathlib import Path
                        raw_bytes = Path(dl_path).read_bytes()
                        b64 = _b64x.b64encode(raw_bytes).decode("ascii")
                        suggested = download.suggested_filename or ""
                        ct = "image/jpeg" if suggested.endswith((".jpg", ".jpeg")) else "image/png"
                        images.append(f"data:{ct};base64,{b64}")
                        logger.info("編輯圖 %d 下載成功，%d bytes", i, len(raw_bytes))
                except Exception as e:
                    logger.warning("編輯圖 %d 下載按鈕失敗：%s，改用 img src", i, e)

        if not images:
            for i, img_el in enumerate(img_els):
                try:
                    src = await img_el.get_attribute("src")
                    if not src:
                        continue
                    if src.startswith("data:image"):
                        # 排除 reference image 自己（雖然應該不在 generated-image 內）
                        images.append(src)
                    elif src.startswith("http"):
                        resp = await page.context.request.get(src)
                        if resp.ok:
                            body = await resp.body()
                            content_type = resp.headers.get("content-type", "image/png")
                            b64 = _b64x.b64encode(body).decode("ascii")
                            images.append(f"data:{content_type};base64,{b64}")
                            logger.info("編輯圖 %d 從 src 下載，%d bytes", i, len(body))
                except Exception as e:
                    logger.warning("編輯圖 %d 擷取失敗：%s", i, e)

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
        return _error("timeout", f"編輯圖片超時（{timeout}秒）", elapsed)
    except Exception as e:
        elapsed = round(time.time() - start, 1)
        logger.exception("Gemini edit_image 互動發生錯誤")
        return _error("browser_error", str(e), elapsed)
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


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

        # 等回應完成。三個結束條件,任一觸發就跳出:
        #   (a) 文字連續 2 次不變且 stop generating 按鈕已消失 = 真正完成
        #   (b) 文字連續 4 次不變 (即使 stop button 還在) = 也視為完成 (按鈕可能延遲消失)
        #   (c) 跑滿 90 秒上限 = 強制跳出
        # 90 秒是給 Pro 模式留的 buffer (Pro stream 通常 20-40 秒,Flash 5-15 秒)
        prev_text = ""
        stable_count = 0
        for _ in range(90):
            await asyncio.sleep(1)
            response_els = await page.query_selector_all(SELECTORS["response"])
            if not response_els:
                continue
            text = (await response_els[-1].inner_text()).strip()
            if text and text == prev_text:
                stable_count += 1
                # 條件 (a): stable + stop 按鈕消失
                if stable_count >= 2:
                    stop_btn = await page.query_selector(SELECTORS["stop_generating"])
                    if not stop_btn:
                        break
                # 條件 (b): 連 4 次穩定 (4 秒沒動) 直接跳出,不管按鈕
                if stable_count >= 4:
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
