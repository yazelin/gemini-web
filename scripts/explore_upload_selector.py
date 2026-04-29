"""探勘 Gemini 網頁的圖片上傳 selector + 上傳後預覽元素。

一次性腳本，跑完輸出 selector 報告即可丟掉。

前提：
- gemini-image-api service 必須是 stopped 狀態（profile 不能被鎖）
- gemini-web 的 profile 已登入 Google

執行：
    cd /home/ct/SDD/gemini-web
    uv run python scripts/explore_upload_selector.py

輸出（stdout）：
    1. 進入 Create image 模式前後的 upload button 比較
    2. 點開選單後的選單項目
    3. 用 expect_file_chooser 上傳一張紅色小圖後的 preview 元素特徵
"""
import asyncio
import io
import json
from pathlib import Path

from playwright.async_api import async_playwright

PROFILE_DIR = "/home/ct/.gemini-image/profiles"  # 跟 prod gemini-web-api 用的同一份
GEMINI_URL = "https://gemini.google.com/app"


def _make_red_png(size: int = 100) -> bytes:
    """產生 100x100 紅色 PNG 給上傳用"""
    from PIL import Image
    img = Image.new("RGB", (size, size), (220, 30, 30))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


async def explore() -> None:
    test_img_path = Path("/tmp/explore_red.png")
    test_img_path.write_bytes(_make_red_png())
    print(f"[fixture] 紅色測試圖：{test_img_path} ({test_img_path.stat().st_size} bytes)")

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            PROFILE_DIR,
            headless=True,  # 探勘只用 evaluate 抓 DOM，不用顯示
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        try:
            print("\n[step 1] 進 Gemini 主頁")
            await page.goto(GEMINI_URL, wait_until="domcontentloaded", timeout=30_000)
            await asyncio.sleep(3)

            # 確認登入狀態
            login_btn = await page.query_selector('a:has-text("登入"), a:has-text("Sign in")')
            if login_btn:
                print("[FAIL] 看到登入按鈕，profile 未登入。請先用 gemini-web 跑一次 login flow。")
                return
            print("[ok] 已登入狀態")

            # 1. 抓 upload button 詳細資訊
            print("\n[step 2] 探勘 upload button DOM")
            upload_info = await page.evaluate("""() => {
                const btn = document.querySelector('button[aria-label="開啟上傳檔案選單"], button[aria-label="Open upload file menu"]');
                if (!btn) return null;
                return {
                    aria_label: btn.getAttribute('aria-label'),
                    classes: btn.className,
                    has_upload_card_button: btn.classList.contains('upload-card-button'),
                    parent_tag: btn.parentElement?.tagName,
                };
            }""")
            print("upload_button:", json.dumps(upload_info, ensure_ascii=False, indent=2))

            # 2. 點開選單，看選單項目
            print("\n[step 3] 點 upload button 看選單長相")
            await page.click('button.upload-card-button[aria-label="開啟上傳檔案選單"], button.upload-card-button[aria-label="Open upload file menu"]')
            await asyncio.sleep(2)
            menu_items = await page.evaluate("""() => {
                // 比較廣泛地撈：mat-menu 內所有 button、所有 menuitem、所有有 click handler 的 div/li
                const overlays = document.querySelectorAll('.cdk-overlay-container');
                const items = [];
                for (const ov of overlays) {
                    // 各種可能的選項元素
                    ov.querySelectorAll('button, [role="menuitem"], [role="option"], a, li[tabindex]').forEach(el => {
                        const text = (el.innerText || '').trim();
                        if (!text) return;
                        items.push({
                            text: text.substring(0, 100),
                            aria: el.getAttribute('aria-label') || '',
                            role: el.getAttribute('role') || '',
                            tag: el.tagName,
                            classes: (el.className || '').substring(0, 100),
                            visible: el.offsetParent !== null,
                        });
                    });
                }
                return items;
            }""")
            print("menu items:", json.dumps(menu_items, ensure_ascii=False, indent=2))

            # 關閉選單
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.5)

            # 3. 用 expect_file_chooser 上傳
            print("\n[step 4] 模擬上傳檔案、抓 preview 元素")
            # 在某些 SPA 中，upload 按鈕點開後選某個 menu item 才觸發 file dialog
            # 先試直接點 upload button 看是否觸發 file_chooser
            chosen = False
            try:
                async with page.expect_file_chooser(timeout=5000) as fc_info:
                    await page.click('button.upload-card-button[aria-label="開啟上傳檔案選單"], button.upload-card-button[aria-label="Open upload file menu"]')
                fc = await fc_info.value
                await fc.set_files(str(test_img_path))
                chosen = True
                print("[ok] 直接點 upload button 觸發 file dialog 成功")
            except Exception as e:
                print(f"[info] 直接點 upload button 沒觸發 file dialog：{e}")
                # fallback：先點 button、再點某個 menu item
                await page.click('button.upload-card-button[aria-label="開啟上傳檔案選單"], button.upload-card-button[aria-label="Open upload file menu"]')
                await asyncio.sleep(1)
                # 找含「圖片」「上傳」「Upload」「Image」等字的 menu item
                try:
                    async with page.expect_file_chooser(timeout=5000) as fc_info:
                        # 嘗試點看起來像「上傳檔案」的選項
                        await page.evaluate("""() => {
                            const items = Array.from(document.querySelectorAll('[role="menuitem"], button'));
                            const target = items.find(el => {
                                const t = (el.innerText || '').toLowerCase();
                                const a = (el.getAttribute('aria-label') || '').toLowerCase();
                                return /上傳|附加|upload|attach|file|電腦|圖片|image/.test(t) ||
                                       /upload|attach|file|image/.test(a);
                            });
                            if (target) target.click();
                        }""")
                    fc = await fc_info.value
                    await fc.set_files(str(test_img_path))
                    chosen = True
                    print("[ok] 點 menu item 後觸發 file dialog 成功")
                except Exception as e2:
                    print(f"[FAIL] menu item 也沒觸發：{e2}")

            if not chosen:
                print("[FAIL] 沒辦法觸發 file dialog，需要人工 debug")
                return

            await asyncio.sleep(3)  # 等預覽 render
            print("\n[step 5] 抓 preview 元素特徵")
            preview_info = await page.evaluate("""() => {
                // 上傳完成後輸入框附近會出現縮圖預覽。掃所有 img + 看 alt/src
                const imgs = Array.from(document.querySelectorAll('img'));
                const candidates = imgs.filter(img => {
                    const src = img.src || '';
                    // 上傳預覽通常是 blob: 或 data: URL
                    return src.startsWith('blob:') || src.startsWith('data:image');
                }).map(img => ({
                    src_prefix: img.src.substring(0, 50),
                    alt: img.alt,
                    classes: img.className?.substring(0, 100),
                    parent_tag: img.parentElement?.tagName,
                    parent_class: img.parentElement?.className?.substring(0, 100),
                    grandparent_tag: img.parentElement?.parentElement?.tagName,
                    grandparent_class: img.parentElement?.parentElement?.className?.substring(0, 100),
                    width: img.naturalWidth || img.width,
                }));
                // 也找 file-preview / attachment 之類的容器
                const containers = Array.from(document.querySelectorAll('[class*="preview"], [class*="attachment"], [class*="upload"], file-preview, attachment-preview')).map(el => ({
                    tag: el.tagName,
                    classes: el.className?.substring(0, 100),
                    visible: el.offsetParent !== null,
                }));
                return {
                    blob_imgs: candidates,
                    preview_containers: containers.slice(0, 10),
                };
            }""")
            print("preview:", json.dumps(preview_info, ensure_ascii=False, indent=2))

            print("\n[step 6] 取消上傳清乾淨（按 Esc + 重整）")
            await page.keyboard.press("Escape")
            await asyncio.sleep(1)

        finally:
            await ctx.close()
            test_img_path.unlink(missing_ok=True)
            print("\n[done] 已關閉瀏覽器、清理 fixture")


if __name__ == "__main__":
    asyncio.run(explore())
