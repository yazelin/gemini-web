"""Gemini 頁面互動 — 輸入 prompt、等待生成、擷取圖片或文字回應"""
import asyncio
import json
import logging
import re
import time
from pathlib import Path

from playwright.async_api import Page

from .selectors import MODEL_MODE_MAP, SELECTORS

logger = logging.getLogger(__name__)

# 瀏覽器端 JS：把 <img> 畫進 canvas 轉 data URL。
# 用於 blob: src（download 按鈕在 chat-edit 模式不觸發 download 事件時的後備）。
# 同源 blob 不會污染 canvas，可直接 toDataURL。
_CANVAS_EXTRACT_JS = """
(img) => {
  try {
    const w = img.naturalWidth || img.width;
    const h = img.naturalHeight || img.height;
    if (!w || !h) return null;
    const c = document.createElement('canvas');
    c.width = w; c.height = h;
    c.getContext('2d').drawImage(img, 0, 0);
    return c.toDataURL('image/png');
  } catch (e) { return null; }
}
"""

# 瀏覽器端 JS：dump cdk-overlay 內的選單項目文字（debug 上傳/工具選單用）
_DUMP_MENU_JS = """
() => Array.from(document.querySelectorAll(
    ".cdk-overlay-container [role='menuitem'], "
    + ".cdk-overlay-container [role='menuitemcheckbox'], "
    + ".cdk-overlay-container button"
)).map(b => (b.innerText || b.getAttribute('aria-label') || '').trim().substring(0, 30))
 .filter(Boolean)
"""

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

# 瀏覽器端 JS：點掉 onboarding 提示橫幅（如「我知道了」）。
# 這類橫幅會蓋住/攔截下載按鈕的 pointer 事件,害 btn.hover() 空等逾時 →
# 只能退到 canvas 縮圖。用 JS .click() 直接點(不受遮擋影響),把它關掉,
# 下載按鈕才能正常按、拿全解析度原檔。標籤保守列舉,避免誤點真的確認框。
_DISMISS_BANNER_JS = """
() => {
  const LABELS = ['我知道了','知道了','Got it','了解','Dismiss','No thanks','Not now','稍後再說','稍後'];
  const clicked = [];
  document.querySelectorAll('button').forEach(b => {
    const t = (b.innerText || '').trim();
    if (t && t.length < 12 && LABELS.some(l => t === l || t.includes(l))) {
      try { b.click(); clicked.push(t); } catch (e) {}
    }
  });
  return clicked;
}
"""


async def page_ready(page: Page) -> dict:
    """頁面是不是「可以開工」的狀態。

    只檢查輸入框不夠 —— 停在舊對話的頁面**輸入框還在**,少的是 composer 的
    工具鈕,而那正是進圖片模式的入口(2026-07-30 worker 3 就卡在這:health
    一直回報正常,實際上每筆生圖都失敗)。所以兩個都要看。

    回傳 {"ready": bool, "missing": [...]}。
    """
    try:
        has_input = await page.query_selector(SELECTORS["input"]) is not None
        has_tools = await page.query_selector(SELECTORS["tools_button"]) is not None
    except Exception as e:
        return {"ready": False, "missing": ["page_error"], "detail": str(e)[:120]}
    missing = [n for n, ok in (("input", has_input), ("tools_button", has_tools)) if not ok]
    return {"ready": not missing, "missing": missing}


async def dump_page_state(page: Page, tag: str, worker_id: int | None = None) -> str | None:
    """把卡住的現場存下來:截圖 + 網址/標題/按鈕清單。

    「卡在哪」光看 log 很難答,有截圖就一眼看得出是跳了對話框、停在舊對話、
    還是版面根本沒渲染完。存在 <data_dir>/diagnostics/,只留最近 20 份。
    """
    try:
        from .config import settings

        out_dir = Path(settings.data_dir) / "diagnostics"
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        base = out_dir / f"w{worker_id if worker_id is not None else 'x'}-{stamp}-{tag}"

        await page.screenshot(path=f"{base}.png")
        buttons = await page.evaluate("""() => Array.from(document.querySelectorAll('button'))
            .map(b => ({text: b.innerText.trim().slice(0, 40),
                        aria: (b.getAttribute('aria-label') || '').slice(0, 40)}))
            .filter(b => b.text || b.aria)""")
        info = {
            "worker_id": worker_id,
            "tag": tag,
            "url": page.url,
            "title": await page.title(),
            "ready": await page_ready(page),
            "buttons": buttons,
        }
        Path(f"{base}.json").write_text(
            json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.warning("已存卡住現場:%s.png(+.json)", base)

        # 只留最近 20 組,別把磁碟塞爆
        shots = sorted(out_dir.glob("*.png"), key=lambda p: p.stat().st_mtime)
        for old in shots[:-20]:
            old.unlink(missing_ok=True)
            old.with_suffix(".json").unlink(missing_ok=True)
        return f"{base}.png"
    except Exception as e:  # noqa: BLE001 — 診斷失敗不該影響主流程
        logger.warning("存卡住現場失敗:%s", e)
        return None


async def _dismiss_onboarding(page: Page) -> None:
    """點掉「我知道了」之類 onboarding 橫幅。非致命,失敗就算了。"""
    try:
        clicked = await page.evaluate(_DISMISS_BANNER_JS)
        if clicked:
            logger.info("關閉 onboarding 橫幅: %s", clicked)
            # 關掉橫幅會讓 Angular 重繪 composer,舊的 0.3 秒不夠:2026-07-30
            # worker 3 就是關完橫幅 0.4 秒後去點 Tools,點到還沒重繪好的版面 →
            # 選單沒開 → 進不了圖片模式 → 空等到逾時 → 頁面留髒 → 連環失敗。
            # 這個橫幅三天只出現一次,多等一秒的代價可以忽略。
            await asyncio.sleep(1.5)
    except Exception:
        pass


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

# Gemini 自己出包時回的通用錯誤（不是安全拒絕，也不是還在生成中）。
# 2026-08 那次 headless_shell 被拒出圖就是回這幾句，而程式在等圖片元素，
# 於是每筆都空等滿逾時（400 秒）才發現沒圖，錯誤內容也沒被記下來。
# 見到這些就別再等了，直接把原文帶回去。
_GENERIC_ERROR_PHRASES = [
    "I seem to be encountering an error",
    "I'm having a hard time fulfilling your request",
    "Something went wrong",
    "Sorry, something went wrong",
    "發生錯誤",
    "出了點問題",
]


async def _wait_for_image_or_error(page: Page, wait_ms: int) -> str | None:
    """等生成的圖片出現；Gemini 若先回通用錯誤就提早收工。

    回傳踩到的錯誤原文（截斷），沒踩到就回 None——圖出現了、或等到逾時，
    兩種都由呼叫端照原本的邏輯往下走。

    只認「明確的錯誤字樣」而不是「有文字就算完成」：出圖成功時 Gemini 也可能
    先吐一段說明文字，用文字有無當終止條件會在圖還在渲染時就誤判成失敗。
    """
    deadline = time.monotonic() + wait_ms / 1000
    while time.monotonic() < deadline:
        if await page.query_selector(SELECTORS["images"]):
            return None
        els = await page.query_selector_all(SELECTORS["response"])
        if els:
            text = (await els[-1].inner_text()).strip()
            for phrase in _GENERIC_ERROR_PHRASES:
                if phrase.lower() in text.lower():
                    logger.warning("Gemini 回通用錯誤，不再空等：%s", text[:120])
                    return text[:200]
        await asyncio.sleep(2)
    return None


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


async def _enter_create_image_mode(page: Page, input_el):
    """點 Tools → 建立圖像,進入圖片生成模式。

    回傳 (是否成功, 輸入框)。失敗不丟例外——呼叫端會硬重置頁面再試一次,
    兩次都失敗才退回 prefix fallback。
    """
    return await _enter_tool_mode(page, input_el, "create_image", "Create image")


async def _enter_create_video_mode(page: Page, input_el):
    """點 Tools → 建立影片,進入影片生成模式(Veo)。

    跟圖片走同一個工具選單、同一套流程,只有選單項不同。
    """
    return await _enter_tool_mode(page, input_el, "create_video", "Create video")


async def _enter_tool_mode(page: Page, input_el, selector_key: str, label: str):
    """點 Tools → 選單裡的某一項,進入該工具模式。

    圖片與影片共用。原本只有圖片一條,抽出來是因為影片除了選單項不同以外
    每一步都一樣——包括「點完選單不會自動關、要按 Esc 才不會擋住輸入框」
    這種踩過的雷。
    """
    try:
        # Debug: 列出頁面上所有按鈕(頁面停在舊對話時,這份清單就是唯一線索)
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
        if not tools_btn:
            return False, input_el

        await tools_btn.click()
        logger.info("已點擊 Tools 按鈕，等待選單...")
        await asyncio.sleep(1.5)
        # 等 Create image 按鈕出現
        create_img_btn = await page.wait_for_selector(
            SELECTORS[selector_key], state="visible", timeout=5_000
        )
        if not create_img_btn:
            logger.warning("找不到 %s 按鈕", label)
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.5)
            return False, input_el

        # 縮短 click timeout，避免 selector 過時時卡 30 秒重試
        await create_img_btn.click(timeout=5_000)
        await asyncio.sleep(1)
        # 2026-06：「建立圖像」是 menuitemcheckbox，點完選單(overlay)不會
        # 自動關，會擋住輸入框。按 Esc 關掉 overlay；image 模式已開啟，
        # Esc 只關選單不會取消模式（已實機驗證）。
        await page.keyboard.press("Escape")
        await asyncio.sleep(1)
        logger.info("已切換至 %s 模式", label)
        # 重新取得輸入框（模式切換後可能會刷新）
        refreshed = await page.wait_for_selector(
            SELECTORS["input"], state="visible", timeout=10_000
        )
        return True, (refreshed or input_el)

    except Exception as e:
        logger.warning("切換 %s 模式失敗：%s", label, e)
        # 確保關閉可能開啟的選單
        try:
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.3)
        except Exception:
            pass
        return False, input_el


async def probe_video_capability(page: Page) -> bool | None:
    """看這個帳號的工具選單裡有沒有「建立影片」。

    Veo 只給付費層，但**不能用「升級」按鈕在不在來判斷** —— Google One 家庭
    群組分享的方案，帳號畫面上仍可能顯示升級。唯一可靠的是直接開選單看那一項
    在不在。

    回 True / False；連工具選單都打不開時回 None（那是頁面卡住，不是沒能力，
    不該被記成「這個帳號不行」）。探測完會把選單關掉，不改變頁面模式。
    """
    try:
        await _dismiss_onboarding(page)
        tools_btn = await page.wait_for_selector(
            SELECTORS["tools_button"], state="visible", timeout=8_000)
        if not tools_btn:
            return None
        await tools_btn.click()
        await asyncio.sleep(1.5)
        found = await page.query_selector(SELECTORS["create_video"])
        # 順手記下選單裡實際有哪些項目，選單改名時這份 log 就是線索
        items = await page.evaluate("""() => Array.from(
            document.querySelectorAll(".cdk-overlay-container [role='menuitemcheckbox'], "
                                      + ".cdk-overlay-container [role='menuitem']")
        ).map(e => e.innerText.trim().slice(0, 30)).filter(Boolean)""")
        logger.info("工具選單項目：%s → 有影片=%s",
                    json.dumps(items, ensure_ascii=False)[:300], bool(found))
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.5)
        return bool(found)
    except Exception as e:
        logger.warning("探測影片能力失敗：%s", e)
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        return None


async def _ensure_create_video_mode(page: Page, input_el, worker_id: int | None = None):
    """進影片生成模式;第一次失敗就硬重置頁面再試一次。理由同圖片那條。"""
    switched, input_el = await _enter_create_video_mode(page, input_el)
    if switched:
        return True, input_el

    await dump_page_state(page, "video-mode", worker_id)
    logger.warning("進不了 Create video 模式,硬重置頁面後重試一次")
    if not await new_chat(page):
        return False, input_el
    await asyncio.sleep(1)
    retry_input = await page.wait_for_selector(
        SELECTORS["input"], state="visible", timeout=15_000
    )
    if not retry_input:
        return False, input_el
    await _dismiss_onboarding(page)
    switched, input_el = await _enter_create_video_mode(page, retry_input)
    if switched:
        logger.info("重置後成功進入 Create video 模式")
    return switched, input_el


async def generate_video(page: Page, prompt: str, timeout: int = 600,
                         worker_id: int | None = None) -> dict:
    """在 Gemini 頁面產生影片(Veo)並把 mp4 抓下來

    Veo 比生圖慢得多(數分鐘),所以預設 timeout 是 600 秒而不是 60。

    回傳 {"success": True, "video": <base64>, "mime": ..., "elapsed_seconds": ...}
    或 {"success": False, "error": ..., "message": ...}
    """
    import base64

    start = time.time()
    try:
        input_el = await page.wait_for_selector(
            SELECTORS["input"], state="visible", timeout=15_000)
        if not input_el:
            return _error("browser_error", "找不到輸入框")
        await asyncio.sleep(1)
        await _dismiss_onboarding(page)

        switched, input_el = await _ensure_create_video_mode(page, input_el, worker_id)
        if not switched:
            return _error("browser_error",
                          "進不了影片生成模式（工具選單裡找不到「建立影片」，"
                          "可能是帳號沒有這個功能，或選單改版了；診斷截圖見 diagnostics/）",
                          round(time.time() - start, 1))

        await input_el.click()
        await page.keyboard.type(prompt)
        await asyncio.sleep(0.5)
        await page.keyboard.press("Enter")
        logger.info("影片 prompt 已送出，開始等待（最長 %d 秒）", timeout)

        # 等影片元素出現
        deadline = time.monotonic() + timeout
        video_el = None
        while time.monotonic() < deadline:
            video_el = await page.query_selector(SELECTORS["videos"])
            if video_el:
                break
            await asyncio.sleep(5)

        elapsed = round(time.time() - start, 1)
        if not video_el:
            # 抓不到就存證：影片結果的 DOM 還沒實地驗過，這份截圖與元素清單
            # 就是修選擇器的依據
            await dump_page_state(page, "video-result", worker_id)
            media = await page.evaluate("""() => Array.from(
                document.querySelectorAll('video, source, [class*=video], [class*=Video]')
            ).slice(0, 20).map(e => ({tag: e.tagName, cls: (e.className||'').toString().slice(0,60),
                                      src: (e.getAttribute('src')||'').slice(0,80)}))""")
            logger.warning("等不到影片元素，頁面上的影片相關節點：%s",
                           json.dumps(media, ensure_ascii=False)[:800])
            return _error("timeout", f"等了 {timeout} 秒沒等到影片（診斷截圖見 diagnostics/）", elapsed)

        # 優先用下載鈕拿原檔；拿不到就退回 src
        try:
            btn = await page.query_selector(SELECTORS["download_video"])
            if btn:
                async with page.expect_download(timeout=300_000) as info:
                    await btn.click()
                dl = await info.value
                path = await dl.path()
                if path:
                    data = Path(path).read_bytes()
                    return {"success": True, "mime": "video/mp4",
                            "video": base64.b64encode(data).decode("ascii"),
                            "prompt": prompt, "elapsed_seconds": elapsed}
        except Exception as e:
            logger.warning("影片下載鈕失敗，改用 src：%s", e)

        src = await video_el.get_attribute("src")
        if not src:
            src_el = await page.query_selector("video source[src]")
            src = await src_el.get_attribute("src") if src_el else None
        if not src:
            await dump_page_state(page, "video-src", worker_id)
            return _error("browser_error", "影片元素存在但抓不到 src（診斷截圖見 diagnostics/）", elapsed)

        resp = await page.request.get(src)
        if not resp.ok:
            return _error("browser_error", f"影片下載失敗 HTTP {resp.status}", elapsed)
        data = await resp.body()
        return {"success": True, "mime": "video/mp4",
                "video": base64.b64encode(data).decode("ascii"),
                "prompt": prompt, "elapsed_seconds": elapsed}

    except Exception as e:
        logger.error("影片生成失敗：%s", e, exc_info=True)
        return _error("browser_error", str(e)[:300], round(time.time() - start, 1))


async def _ensure_create_image_mode(page: Page, input_el, worker_id: int | None = None):
    """進圖片生成模式;第一次失敗就硬重置頁面再試一次。

    頁面若停在舊對話上,composer 的「上傳與工具」鈕不存在,選擇器會誤中
    「開啟對話動作選單」→ 選單裡沒有「建立圖像」。這個髒狀態不會自己好:
    每筆請求都退回 prefix fallback、純聊天產不出圖、空等到逾時,該 worker
    從此再也生不出圖(2026-07-30 worker 3 實測 6 筆全滅)。所以要重置重試。
    """
    switched, input_el = await _enter_create_image_mode(page, input_el)
    if switched:
        return True, input_el

    # 重置會把現場沖掉,所以先存證:截圖 + 按鈕清單,事後才查得出「卡在哪」
    await dump_page_state(page, "image-mode", worker_id)
    logger.warning("進不了 Create image 模式,硬重置頁面後重試一次")
    if not await new_chat(page):
        return False, input_el
    await asyncio.sleep(1)
    retry_input = await page.wait_for_selector(
        SELECTORS["input"], state="visible", timeout=15_000
    )
    if not retry_input:
        return False, input_el
    await _dismiss_onboarding(page)
    switched, input_el = await _enter_create_image_mode(page, retry_input)
    if switched:
        logger.info("重置後成功進入 Create image 模式")
    return switched, input_el


async def generate_image(page: Page, prompt: str, timeout: int = 60,
                         worker_id: int | None = None) -> dict:
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

        # 1.6 點掉「我知道了」等橫幅（否則會擋住後面的下載按鈕）
        await _dismiss_onboarding(page)

        # 1.7 點擊 Tools → Create image 進入圖片生成模式(進不去會自己重置重試)
        switched_to_create_image, input_el = await _ensure_create_image_mode(
            page, input_el, worker_id
        )

        if not switched_to_create_image:
            logger.warning("重置後仍進不了 Create image 模式,退回 prefix fallback")
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
        # 預留 10 秒給後續處理，避免跟 queue timeout 撞
        wait_ms = max((timeout - 10), 30) * 1000
        err = await _wait_for_image_or_error(page, wait_ms)
        if err:
            return _error("gemini_error", f"Gemini 回錯誤：{err}",
                          round(time.time() - start, 1))
        try:
            await page.wait_for_selector(
                SELECTORS["images"], state="visible", timeout=5_000
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
                    # 先 hover 圖片讓 on-hover-button 顯示。hover 給 5 秒上限:
                    # 某些帳號的 Gemini UI 有殘留橫幅(如「我知道了」)蓋住按鈕,
                    # hover 會空等預設 30 秒。快速失敗 → 退到下面的 canvas 後備。
                    if img_els and i < len(img_els):
                        await img_els[i].hover(timeout=5_000)
                        await asyncio.sleep(0.5)
                    await btn.hover(timeout=5_000)
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

        # 備用：直接從 img src 取圖。三種 src 都要接得住:
        #   data:  → 直接用
        #   http:  → server 端抓
        #   blob: / 無 src → canvas 後備(跟 edit_image 同一招)。worker 1 那類
        #                    帳號生出來的圖是 blob:,少了這條就會 browser_error。
        if not images:
            for i, img_el in enumerate(img_els):
                try:
                    src = await img_el.get_attribute("src")
                    if src and src.startswith("data:image"):
                        images.append(src)
                    elif src and src.startswith("http"):
                        resp = await page.context.request.get(src)
                        if resp.ok:
                            body = await resp.body()
                            content_type = resp.headers.get("content-type", "image/png")
                            b64 = _b64.b64encode(body).decode("ascii")
                            images.append(f"data:{content_type};base64,{b64}")
                            logger.info("圖片 %d 從 src 下載，%d bytes", i, len(body))
                    else:
                        data_url = await img_el.evaluate(_CANVAS_EXTRACT_JS)
                        if data_url:
                            images.append(data_url)
                            logger.info("圖片 %d 從 canvas 擷取，%d chars", i, len(data_url))
                        else:
                            logger.warning("圖片 %d canvas 擷取回傳空（src=%s）", i, str(src)[:40])
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


async def _attach_file(page: Page, tmp_path: str | list[str]) -> None:
    """點「上傳與工具」→ 選單 → file chooser → set_files。

    這段從 edit_image 抽出來，讓 chat_with_file 共用。選單長相各帳號不同
    （有的多一層子選單、有的第一次用會跳同意條款），已知變體全部在這裡。
    失敗直接拋例外，由呼叫端決定錯誤訊息。

    收單一路徑或一串路徑。一次送多張是給參考圖用的:Gemini 網頁版一個
    file chooser 就吃得下整批,而且模型認得順序(2026-08-21 實測,三張純色
    形狀圖把送出順序與檔名字母序故意錯開,它照送出順序答對)。
    """
    logger.info("點擊上傳按鈕 + 選單，等 file chooser...")
    try:
        async with page.expect_file_chooser(timeout=6_000) as fc_info:
            try:
                await page.click(SELECTORS["tools_button"], timeout=8_000)
            except Exception:
                await page.click(SELECTORS["upload_button"], timeout=5_000)
            await asyncio.sleep(0.8)          # 等 mat-menu 動畫
            try:
                menu_items = await page.evaluate(_DUMP_MENU_JS)
                logger.info("上傳與工具選單: %s", json.dumps(menu_items, ensure_ascii=False)[:400])
            except Exception:
                pass
            await page.click(SELECTORS["upload_menu_item_local"])
        file_chooser = await fc_info.value
    except Exception:
        # 沒跳檔案對話框。先印 overlay 留線索，再試兩種已知變體：
        #   1. 同意條款對話框（該帳號第一次用上傳功能）
        #   2. 多一層子選單（從電腦 / 從裝置上傳）
        try:
            overlay = await page.evaluate(_DUMP_MENU_JS)
            logger.warning("沒跳 file chooser，當下 overlay: %s",
                           json.dumps(overlay, ensure_ascii=False)[:400])
        except Exception:
            pass
        file_chooser = None
        for sel in ("upload_consent_accept", "upload_submenu_item_device"):
            try:
                async with page.expect_file_chooser(timeout=8_000) as fc_info:
                    await page.click(SELECTORS[sel], timeout=3_000)
                file_chooser = await fc_info.value
                logger.info("靠 %s 拿到 file chooser", sel)
                break
            except Exception:
                continue
        if file_chooser is None:
            raise RuntimeError("點完上傳檔案沒有 file chooser，同意鈕與子選單都試過了")
    paths = [tmp_path] if isinstance(tmp_path, str) else list(tmp_path)
    await file_chooser.set_files(paths)
    logger.info("已 set_files（%d 個）：%s", len(paths), paths)


def _ref_to_bytes(raw: str) -> tuple[bytes, str]:
    """一張參考圖 → (位元組, 副檔名)。data URL 或純 base64 都收。

    副檔名照 mime 給,不要一律 .png:Gemini 靠副檔名判型別,webp 命名成 png
    在上傳那一步就可能被擋掉,而錯誤訊息只會說沒看到預覽。
    """
    import base64 as _b64
    mime = "image/png"
    if raw.startswith("data:") and "," in raw:
        head, raw = raw.split(",", 1)
        mime = head.split(":")[1].split(";")[0] if ":" in head else mime
    data = _b64.b64decode(raw)
    ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg",
           "image/webp": ".webp", "image/gif": ".gif"}.get(mime.lower(), ".png")
    return data, ext


async def edit_image(
    page: Page,
    prompt: str,
    reference_images: str | list[str],
    timeout: int = 120,
) -> dict:
    """以參考圖編輯模式生成圖片：上傳 reference 圖 + 文字 prompt → 編輯後的新圖

    Args:
        page: 已開啟 Gemini 對話的 Playwright Page
        prompt: 編輯指令（建議英文，例：「change the dog's color to black」）
        reference_images: 參考圖,一張字串或一串字串。每張可以是
            data:image/...;base64,xxx 或純 base64。**順序有意義**:呼叫端的
            prompt 常照「image 1 是畫風、image 2 是角色」指名。
        timeout: 整體 timeout 秒數

    Returns:
        同 generate_image：
        {"success": True, "images": [...], "prompt": ..., "elapsed_seconds": ...}
        或 {"success": False, "error": ..., "message": ...}
    """
    import os
    import shutil as _shutil
    import tempfile

    start = time.time()

    refs = [reference_images] if isinstance(reference_images, str) else list(reference_images)
    refs = [r for r in refs if r]
    if not refs:
        return _error("invalid_input", "沒有參考圖")

    # 全部寫進同一個暫存資料夾,一次 set_files 整批送上去
    tmp_dir = tempfile.mkdtemp(prefix="gemini_ref_")
    tmp_paths: list[str] = []
    try:
        for i, raw in enumerate(refs, 1):
            try:
                img_bytes, ext = _ref_to_bytes(raw)
            except Exception:
                return _error("invalid_input", f"第 {i} 張參考圖不是有效的 base64")
            if len(img_bytes) > 10 * 1024 * 1024:
                return _error("invalid_input", f"第 {i} 張參考圖超過 10 MB")
            # 檔名帶序號,萬一要看 Gemini 畫面上的附件卡片,順序一眼看得出來
            path = os.path.join(tmp_dir, f"ref{i:02d}{ext}")
            with open(path, "wb") as f:
                f.write(img_bytes)
            tmp_paths.append(path)

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

        # 1.6 點掉「我知道了」等橫幅（否則會擋住後面的下載按鈕）
        await _dismiss_onboarding(page)

        # 1.7 image edit 不切 Create image 模式
        # 原因：Create image 模式的 UI 沒有上傳檔案選單；Banana 模型在普通
        # chat 模式下接收「圖片 + prompt」會自動做 image-to-image edit
        switched_to_create_image = False  # 標記給後段 prompt 處理用

        # 2. 上傳 reference image
        # 流程：點「上傳與工具」合併鈕 → 選單彈出 → 點「上傳檔案」menuitem → file chooser
        # 2026-06：Gemini 把上傳併進「上傳與工具」單一鈕，舊的 upload_button
        # (開啟上傳檔案選單) 已不存在。改點 tools_button（generate 路徑已驗證可開
        # 同一個選單），失敗再退回舊 upload_button。兩段 click 都包在
        # expect_file_chooser 內，由 Playwright 攔截 file dialog。
        # 2026-07：部分帳號（實測 ct.opui.2025 / ctgemini2026，worker 1 & 3）點完
        # 「上傳檔案」不會直接跳 file dialog，而是再展開一層子選單（跟「更多上傳
        # 選項」同源），於是 expect_file_chooser 一路等到逾時、edit 100% 失敗。
        # 改成兩段：先用短逾時等 file chooser，沒等到就 dump 當下 overlay（留證據）
        # 並在新選單裡找「從電腦/裝置上傳」這類項目再點一次。
        try:
            await _attach_file(page, tmp_paths)
        except Exception as e:
            elapsed = round(time.time() - start, 1)
            return _error(
                "upload_failed",
                f"上傳 reference image 失敗：{e}",
                elapsed,
            )

        # 3. 等預覽圖出現（blob: img 是 Gemini 上傳完成的指標）
        #    多圖時要等**張數到齊**。只等「至少一張」的話,第一張一掛上去就
        #    往下走,後面幾張還在傳就被送出了,而畫面上完全看不出少了圖。
        try:
            await page.wait_for_function(
                """(want) => {
                    const imgs = Array.from(document.querySelectorAll('img'));
                    const n = imgs.filter(img => {
                        const src = img.src || '';
                        return src.startsWith('blob:') && (img.naturalWidth || 0) > 30;
                    }).length;
                    return n >= want;
                }""",
                arg=len(tmp_paths),
                timeout=20_000 + 10_000 * (len(tmp_paths) - 1),
            )
            logger.info("%d 張 reference image 預覽都出現了", len(tmp_paths))
            await asyncio.sleep(1)  # 多等一點讓 UI stabilize
        except Exception:
            elapsed = round(time.time() - start, 1)
            return _error(
                "upload_timeout",
                f"上傳 {len(tmp_paths)} 張參考圖後沒看齊預覽，可能上傳未成功",
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
        wait_ms = max((timeout - 10), 30) * 1000
        err = await _wait_for_image_or_error(page, wait_ms)
        if err:
            return _error("gemini_error", f"Gemini 回錯誤：{err}",
                          round(time.time() - start, 1))
        try:
            await page.wait_for_selector(
                SELECTORS["images"], state="visible", timeout=5_000
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
                    # chat-edit 模式下這顆下載鈕實測不觸發 download 事件，當作
                    # 快速探測就好（拿全解析度原圖的機會），抓不到立刻退到
                    # canvas/src 後備，避免空等拖長整體時間 + 撞 Cloudflare timeout。
                    async with page.expect_download(timeout=12_000) as download_info:
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
                    if src and src.startswith("data:image"):
                        # 排除 reference image 自己（雖然應該不在 generated-image 內）
                        images.append(src)
                    elif src and src.startswith("http"):
                        resp = await page.context.request.get(src)
                        if resp.ok:
                            body = await resp.body()
                            content_type = resp.headers.get("content-type", "image/png")
                            b64 = _b64x.b64encode(body).decode("ascii")
                            images.append(f"data:{content_type};base64,{b64}")
                            logger.info("編輯圖 %d 從 src 下載，%d bytes", i, len(body))
                    else:
                        # blob: 或無 src — 用 canvas 把渲染出的圖轉 data URL
                        data_url = await img_el.evaluate(_CANVAS_EXTRACT_JS)
                        if data_url:
                            images.append(data_url)
                            logger.info("編輯圖 %d 從 canvas 擷取，%d chars", i, len(data_url))
                        else:
                            logger.warning("編輯圖 %d canvas 擷取回傳空（src=%s）", i, str(src)[:40])
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
        _shutil.rmtree(tmp_dir, ignore_errors=True)


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
        #   (a) 文字連續 2 次不變且 stop generating 按鈕已消失,再過 2 秒複核一次
        #       仍沒變長 = 真正完成。複核是因為長回應(尤其 JSON/code block)在
        #       生成中會「凍結在開頭片段」直到整塊渲染,按鈕 selector 又可能因
        #       UI 改版失效——沒有複核時長回應約 50% 被截斷成開頭幾個字元。
        #   (b) 文字連續 20 次不變 (即使 stop button 還在) = 最後保險
        #       (原本 4 秒:code block 渲染停頓超過 4 秒就被誤判完成而截斷)
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
                # 條件 (a): stable + stop 按鈕消失 + 2 秒複核未再變長
                if stable_count >= 2:
                    stop_btn = await page.query_selector(SELECTORS["stop_generating"])
                    if not stop_btn:
                        await asyncio.sleep(2)
                        confirm_els = await page.query_selector_all(SELECTORS["response"])
                        confirm = ((await confirm_els[-1].inner_text()).strip()
                                   if confirm_els else text)
                        if confirm != text:
                            # 還在變(不一定變長,例如表格重排) → 渲染停頓誤判,繼續等
                            prev_text = confirm
                            stable_count = 0
                            continue
                        break
                # 條件 (b): 連 20 次穩定 (20 秒沒動) 直接跳出,不管按鈕
                if stable_count >= 20:
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


# Gemini 收不到附件時的招牌回覆。它不會報錯,會禮貌地請你上傳,然後照著
# 你的文字描述憑空編一段分析——回來的東西看起來完全正常,只有內容是假的。
_NO_ATTACHMENT_PATTERNS = (
    # 中文:「請先提供」「請上傳」「尚未收到」…。中間常插副詞(先/再/麻煩),
    # 純字串比對會漏——實測就漏過「請先提供或上傳該音訊檔案」。
    r"請\s*(先|再|麻煩)?\s*(提供|上傳|附上)",
    r"(沒有|尚未|未|還沒)\s*(收到|接收到|取得|看到|讀取到)",
    r"(沒有|尚未|未)\s*(提供|上傳|附上)",
    r"(無法|看不到|讀不到)\s*(讀取)?\s*(檔案|音檔|音訊|附件)",
    # 英文
    r"(i\s+)?(don'?t|do not|cannot|can'?t)\s+(see|find|access|hear)",
    r"(no|any)\s+(file|audio|attachment)\s+(was\s+)?(provided|attached|received)",
    r"(haven'?t|have not)\s+received",
    r"please\s+(upload|provide|attach|share)",
)


def _looks_like_no_attachment(text: str) -> bool:
    """只看第一句。

    整段掃、甚至只掃開頭 120 字都會誤判——真的聽過之後的正常回覆裡出現
    「若需要更細的分析請提供時間點」照樣中招。收不到檔案的時候,那句話一定
    是第一句,所以切到第一個句號就夠。
    """
    first = re.split(r"[。！？\n]", (text or "").strip(), maxsplit=1)[0].lower()
    return any(re.search(pat, first) for pat in _NO_ATTACHMENT_PATTERNS)


async def chat_with_file(
    page: Page,
    prompt: str,
    file_b64: str,
    filename: str = "upload.bin",
    timeout: int = 180,
    _retry: bool = True,
) -> dict:
    """上傳一個檔案（音訊／圖片／文件）＋ 文字 prompt，取回**文字**回應。

    跟 edit_image 的差別只有一個：那支要的是圖，這支要的是字。所以上傳段共用
    _attach_file，送出與讀取段直接沿用 chat()——chat() 自己會重新抓輸入框、
    貼字、送出、等回應穩定，附件留在 composer 裡會一起送出去。

    Args:
        page: 已開啟 Gemini 對話的 Playwright Page
        prompt: 要問的問題
        file_b64: 檔案內容。可以是 data:...;base64,xxx 或純 base64
        filename: 原始檔名，只用來決定暫存檔副檔名（Gemini 靠副檔名判型別）
        timeout: 整體 timeout 秒數

    Returns:
        {"success": True, "text": ..., "prompt": ..., "elapsed_seconds": ...}
        或 {"success": False, "error": ..., "message": ...}
    """
    import base64 as _b64
    import os
    import tempfile

    start = time.time()

    raw = file_b64
    if raw.startswith("data:"):
        try:
            _, raw = raw.split(",", 1)
        except ValueError:
            return _error("invalid_input", "file 格式錯誤")
    try:
        data = _b64.b64decode(raw)
    except Exception:
        return _error("invalid_input", "file 不是有效的 base64")
    if not data:
        return _error("invalid_input", "file 是空的")
    if len(data) > 20 * 1024 * 1024:
        return _error("invalid_input", "file 超過 20 MB")

    # 用暫存「資料夾」而不是 mkstemp,好讓檔案保留原本的名字。
    # mkstemp 產生的是 gemini_file_XXXX.mp3,Gemini 畫面上顯示的就是那個亂數名,
    # 後面拿原始檔名去比對附件卡片永遠對不上 → 每次都 upload_timeout。
    import re as _re
    import shutil as _shutil
    base = os.path.basename(filename) or "upload.bin"
    base = _re.sub(r"[^\w.\-]", "_", base)[:80]
    if not os.path.splitext(base)[1]:
        base += ".bin"
    tmp_dir = tempfile.mkdtemp(prefix="gemini_file_")
    tmp_path = os.path.join(tmp_dir, base)
    try:
        with open(tmp_path, "wb") as f:
            f.write(data)

        # 1. 等輸入框、清掉可能擋住上傳鈕的 overlay 與橫幅
        input_el = await page.wait_for_selector(
            SELECTORS["input"], state="visible", timeout=15_000
        )
        if not input_el:
            return _error("browser_error", "找不到輸入框")
        await asyncio.sleep(1)
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
        await _dismiss_onboarding(page)

        # 2. 上傳
        try:
            await _attach_file(page, tmp_path)
        except Exception as e:
            elapsed = round(time.time() - start, 1)
            return _error("upload_failed", f"上傳檔案失敗：{e}", elapsed)

        # 3. 等附件真的掛上去。圖片會出現 blob: 預覽，音訊／文件只會出現
        #    一張帶檔名的卡片，所以兩種訊號都認。沒等到就不要硬送——沒有附件
        #    的話 Gemini 會回「請提供檔案」，那種假成功最難查。
        stem = os.path.splitext(base)[0][:24]
        try:
            await page.wait_for_function(
                """(needle) => {
                    const imgs = Array.from(document.querySelectorAll('img'));
                    if (imgs.some(i => (i.src || '').startsWith('blob:') && (i.naturalWidth || 0) > 30)) {
                        return true;
                    }
                    if (!needle) return false;
                    return (document.body.innerText || '').includes(needle);
                }""",
                arg=stem,
                timeout=30_000,
            )
            # 卡片出現不等於傳完。Gemini 是先畫卡片、背景繼續上傳,太早按送出
            # 就會拿到「請提供音檔」那種假成功(實測 14 次請求中 2 次)。
            # 沉澱時間隨檔案大小走,至少 3 秒。
            settle = max(3.0, len(data) / (1024 * 1024) * 2.0)
            logger.info("附件已掛上：%s,沉澱 %.1f 秒等後端傳完", base, settle)
            await asyncio.sleep(settle)
        except Exception:
            elapsed = round(time.time() - start, 1)
            return _error(
                "upload_timeout",
                f"上傳後 30 秒內沒看到附件（{base}），可能沒真的傳上去",
                elapsed,
            )

        # 4. 送出與讀取沿用 chat()
        result = await chat(page, prompt, timeout)

        # 附件沒真的送到的時候,Gemini 不會報錯,它會很有禮貌地請你上傳檔案,
        # 然後照著你的文字描述憑空編一段分析。那是最難查的一種失敗,所以這裡
        # 主動認出來,開新對話重跑一次。
        if (isinstance(result, dict) and result.get("success")
                and _looks_like_no_attachment(result.get("text", ""))):
            if _retry:
                logger.warning("Gemini 說沒收到檔案,重試一次")
                await new_chat(page)
                await asyncio.sleep(2)
                return await chat_with_file(page, prompt, file_b64, filename, timeout,
                                            _retry=False)
            logger.error("重試後 Gemini 還是說沒收到檔案")
            return _error("attachment_lost",
                          "附件沒送到 Gemini(重試過一次)。回覆內容是憑空推測的,不能用。",
                          round(time.time() - start, 1))

        if isinstance(result, dict):
            result["elapsed_seconds"] = round(time.time() - start, 1)
        return result

    except Exception as e:
        elapsed = round(time.time() - start, 1)
        logger.exception("chat_with_file 發生錯誤")
        return _error("browser_error", str(e), elapsed)
    finally:
        _shutil.rmtree(tmp_dir, ignore_errors=True)


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
