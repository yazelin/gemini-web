"""FastAPI 應用程式入口"""
import asyncio
import json
import logging
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import admin_db, image_store, jobs
from .admin import router as admin_router
from .config import settings
from .official_api import official_generate
from .openclaw_adapter import build_prompt, build_response_parts
from .worker_pool import WorkerPool, QueueFullError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

worker_pool = WorkerPool(
    worker_count=settings.worker_count,
    max_waiting=settings.queue_max_size,
)
_start_time = time.time()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """服務生命週期：啟動 worker pool，結束時清理"""
    admin_db.init_db()
    stale = jobs.fail_stale_running()
    if stale:
        logger.warning("啟動時把 %d 筆卡在 running 的 job 標成 failed", stale)
    worker_pool.set_mode(admin_db.get_setting("dispatch_mode", "round-robin"))
    await worker_pool.start()
    logger.info(
        "服務已啟動，%d 個 worker，dispatch=%s，port %d",
        settings.worker_count, worker_pool.mode, settings.port,
    )
    yield
    await worker_pool.stop()


app = FastAPI(title="Gemini Image API", lifespan=lifespan)
if settings.cors_allow_origins:
    # 讓純前端網頁(例如 comic-studio)可直接從瀏覽器打 /api/*。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allow_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "x-goog-api-key"],
    )
app.include_router(admin_router)
# StaticFiles stats `directory` on every request, even with check_dir=False
# (that flag only skips the *startup* check) — must exist up front, not just
# get created lazily by image_store.save_images() on the first generation,
# or every request before that first save 500s instead of 404ing.
Path(settings.generated_dir).mkdir(parents=True, exist_ok=True)
app.mount(
    "/generated",
    StaticFiles(directory=settings.generated_dir, check_dir=False),
    name="generated",
)


def _identify_caller(request: Request | None) -> str:
    """Resolve the caller's x-goog-api-key/`key` query param to a display
    name for admin History — "static" for a .env key, the issued name for
    an admin-issued dynamic key, "" if no key was presented at all.

    This is the one choke point all traffic funnels through, so it's also
    where a dynamic key's usage stats get bumped; without this, calls
    through those endpoints would never show up in the Keys page's request
    counts.
    """
    if request is None:
        return ""
    candidate = request.headers.get("x-goog-api-key") or request.query_params.get("key")
    if not candidate:
        return ""
    if candidate in settings.api_keys:
        return "static"
    row = admin_db.get_api_key_by_token(candidate)
    if row:
        admin_db.mark_api_key_used(row["id"])
        return row["name"]
    return "unknown key"


def _first_inline_image(body: dict) -> str | None:
    """從 generateContent 的 contents 撈出參考圖的 base64。

    由後往前找（多輪對話時最後一則才是這次要編的圖），一次只取一張——
    瀏覽器路的 edit 流程只上傳得了一張。`inlineData` / `inline_data` 兩種
    寫法都收（Google SDK 送前者，手刻 JSON 的呼叫端常送後者）。
    """
    for content in reversed(body.get("contents") or []):
        for part in (content or {}).get("parts") or []:
            if not isinstance(part, dict):
                continue
            inline = part.get("inlineData") or part.get("inline_data") or {}
            data = inline.get("data")
            mime = inline.get("mimeType") or inline.get("mime_type") or ""
            if data and str(mime).startswith("image/"):
                return data
    return None


async def _dispatch_and_log(
    kind: str,
    prompt: str,
    model: str,
    timeout: int,
    extra: dict | None = None,
    request: Request | None = None,
    api_key_name: str | None = None,
) -> dict:
    """worker_pool.dispatch 包一層記到 admin history db（見 /admin/requests）。

    只記瀏覽器路（worker_pool 這條）；?official=1 強制走官方 API、完全跳過
    worker_pool 的那次成功不會出現在這裡——admin history 看的是 worker pool 流量。

    api_key_name 給呼叫端明確指定時（例如 admin Test 頁挑一把 key 來模擬）優先用它；
    否則從 request 的 header/query 反解——見 _identify_caller。
    """
    start = time.time()
    resolved_key_name = api_key_name if api_key_name is not None else _identify_caller(request)
    # dispatch 會把實際承接的 worker 寫進 ctx。逾時(wait_for 取消)時 result 拿不到,
    # 只能靠 ctx 才知道是哪個 worker —— 沒有它,admin history 的失敗列 worker 欄
    # 永遠是空的,壞掉的 worker 就查不出來(2026-07-30 worker 3 就查了很久)。
    ctx: dict = {}
    try:
        result = await worker_pool.dispatch(kind, prompt, model, timeout, extra=extra, ctx=ctx)
    except Exception as e:
        admin_db.record(
            kind=kind, prompt=prompt, status="failed",
            error=f"{type(e).__name__}: {e}"[:200] or "dispatch failed",
            duration_seconds=time.time() - start, api_key_name=resolved_key_name,
            worker_id=ctx.get("worker_id"),
        )
        raise

    image_paths: list[str] = []
    if result.get("success") and result.get("images"):
        image_paths = image_store.save_images(result["images"])
        image_store.sweep_old(settings.image_retention_days)

    admin_db.record(
        kind=kind,
        prompt=prompt,
        status="succeeded" if result.get("success") else "failed",
        via=result.get("via", "browser"),
        error="" if result.get("success") else str(result.get("message") or result.get("error") or ""),
        duration_seconds=time.time() - start,
        image_paths=image_paths,
        api_key_name=resolved_key_name,
        worker_id=result.get("worker_id"),
        # 文字路（chat / chat-file）的產出就是這段字。出圖路有 image_paths 連得到圖,
        # 文字路原本哪裡都沒存,History 只看得到問了什麼、看不到答了什麼。
        response_text=result.get("text") or "",
    )
    return result


# ── Request / Response 模型 ──


class GenerateRequest(BaseModel):
    prompt: str
    timeout: int = settings.default_timeout


class ChatRequest(BaseModel):
    prompt: str
    timeout: int = 120


class ChatFileRequest(BaseModel):
    """上傳一個檔案 + 問題，取回文字回應。

    file 接受 data URL（data:audio/mpeg;base64,xxx）或純 base64 字串，最大 20 MB。
    filename 只用來決定副檔名——Gemini 靠副檔名判型別，音訊一定要帶 .mp3/.wav 之類。
    """
    prompt: str
    file: str
    filename: str = "upload.bin"
    model: str = ""
    timeout: int = 240


class EditRequest(BaseModel):
    """以參考圖編輯模式生成圖片。

    reference_image 接受
      - data URL（data:image/jpeg;base64,xxx）
      - 純 base64 字串
    最大 10 MB（base64 編碼後）
    """
    prompt: str
    reference_image: str
    timeout: int = settings.default_timeout


# ── 端點 ──


def _strip_data_url(s: str) -> tuple[str, str]:
    """data:image/png;base64,xxx → (b64, mime);純 base64 → (s, image/png)。"""
    if isinstance(s, str) and s.startswith("data:") and "," in s:
        head, b64 = s.split(",", 1)
        mime = head.split(":")[1].split(";")[0] if ":" in head else "image/png"
        return b64, mime
    return s, "image/png"


def _is_valid_api_key(candidate: str | None) -> bool:
    """兩個金鑰來源都認：.env 的 API_KEYS 靜態集合（沿用舊行為），加上
    admin webui 現場發放、存在 admin_db 的動態 key（sha256 雜湊比對）。
    沒帶 key 或兩邊都沒有 → 一律不算有效（無金鑰時預設關閉）。"""
    if not candidate:
        return False
    if candidate in settings.api_keys:
        return True
    row = admin_db.get_api_key_by_token(candidate)
    if row and row["enabled"]:
        admin_db.mark_api_key_used(row["id"])
        return True
    return False


def _has_valid_key(request: Request) -> bool:
    """付費官方路只開放給帶正確 gemini-web key 的呼叫端（consumer worker 會帶
    x-goog-api-key）。"""
    key = request.headers.get("x-goog-api-key") or request.query_params.get("key")
    return _is_valid_api_key(key)


async def _maybe_official(prompt: str, request: Request, reference_image: str | None = None):
    """試官方 Gemini API。回傳 {success, images, via} 或 None（未啟用/未授權/失敗）。
    付費路 → 一定要帶有效 key，否則直接跳過（免費瀏覽器路照常開放）。"""
    if not settings.gemini_official_api_key:
        return None
    if not _has_valid_key(request):
        return None
    img_b64, mime = (None, "image/png")
    if reference_image:
        img_b64, mime = _strip_data_url(reference_image)
    try:
        imgs = await official_generate(prompt, img_b64, mime)
    except Exception as e:  # noqa: BLE001 — fallback 不該讓整個請求爆
        logger.warning("官方 API fallback 失敗：%s", e)
        return None
    if imgs:
        # WARNING 而非 INFO:每次頂替都代表瀏覽器路失敗一次,而且是**付費**產出。
        # 消費端拿到圖完全無感,只有這行 log 會說出「有 worker 壞了、帳單在長」。
        logger.warning("瀏覽器路失敗,改用付費官方 API 產出 %d 張圖(請查 worker 健康)", len(imgs))
        return {"success": True, "images": imgs, "via": "official"}
    return None


@app.post("/api/generate")
async def api_generate(req: GenerateRequest, request: Request, official: int = Query(default=0)):
    """生成圖片"""
    _verify_api_key(request, None)
    # primary 模式 or ?official=1 → 直接走官方 API，不跑瀏覽器
    if official or settings.gemini_official_mode == "primary":
        r = await _maybe_official(req.prompt, request)
        if r:
            return r
    try:
        result = await _dispatch_and_log("generate", req.prompt, "", req.timeout, request=request)
    except QueueFullError:
        r = await _maybe_official(req.prompt, request)
        if r:
            return r
        raise HTTPException(status_code=429, detail="佇列已滿，請稍後再試")
    except asyncio.TimeoutError:
        r = await _maybe_official(req.prompt, request)
        if r:
            return r
        raise HTTPException(status_code=408, detail=f"請求超時（{req.timeout}秒）")
    # 瀏覽器回來但沒成功 → fallback 官方
    if not result.get("success") and settings.gemini_official_mode == "fallback":
        r = await _maybe_official(req.prompt, request)
        if r:
            return r
    return result


@app.post("/api/chat")
async def api_chat(req: ChatRequest, request: Request):
    """文字對話"""
    _verify_api_key(request, None)
    try:
        result = await _dispatch_and_log("chat", req.prompt, "", req.timeout, request=request)
    except QueueFullError:
        raise HTTPException(status_code=429, detail="佇列已滿，請稍後再試")
    except asyncio.TimeoutError:
        raise HTTPException(status_code=408, detail=f"請求超時（{req.timeout}秒）")
    return result


@app.post("/api/chat-file")
async def api_chat_file(req: ChatFileRequest, request: Request):
    """上傳檔案（音訊／文件／圖片）+ 問題 → 文字回應

    跟 /api/edit 的差別：那支要的是圖，這支要的是字。
    典型用途：把一段音樂丟進來問「有沒有人聲、聽到哪些樂器」。
    """
    _verify_api_key(request, None)
    if not req.file:
        raise HTTPException(status_code=400, detail="缺少 file")
    try:
        result = await _dispatch_and_log(
            "chat_file", req.prompt, req.model, req.timeout,
            extra={"file": req.file, "filename": req.filename},
            request=request,
        )
    except QueueFullError:
        raise HTTPException(status_code=429, detail="佇列已滿，請稍後再試")
    except asyncio.TimeoutError:
        raise HTTPException(status_code=408, detail=f"請求超時（{req.timeout}秒）")
    return result


@app.post("/api/edit")
async def api_edit(req: EditRequest, request: Request, official: int = Query(default=0)):
    """以參考圖編輯模式生成圖片"""
    _verify_api_key(request, None)
    if not req.reference_image:
        raise HTTPException(status_code=400, detail="reference_image 不能為空")
    # primary 模式 or ?official=1 → 直接走官方 API，不跑瀏覽器
    if official or settings.gemini_official_mode == "primary":
        r = await _maybe_official(req.prompt, request, req.reference_image)
        if r:
            return r
    try:
        result = await _dispatch_and_log(
            "edit",
            req.prompt,
            "",
            req.timeout,
            extra={"reference_image": req.reference_image},
            request=request,
        )
    except QueueFullError:
        r = await _maybe_official(req.prompt, request, req.reference_image)
        if r:
            return r
        raise HTTPException(status_code=429, detail="佇列已滿，請稍後再試")
    except asyncio.TimeoutError:
        r = await _maybe_official(req.prompt, request, req.reference_image)
        if r:
            return r
        raise HTTPException(status_code=408, detail=f"請求超時（{req.timeout}秒）")
    # 瀏覽器回來但沒成功 → fallback 官方
    if not result.get("success") and settings.gemini_official_mode == "fallback":
        r = await _maybe_official(req.prompt, request, req.reference_image)
        if r:
            return r
    return result


@app.get("/api/health")
async def api_health():
    """健康檢查"""
    statuses = await worker_pool.worker_status()
    alive_count = sum(1 for s in statuses if s["alive"])
    logged_in_count = sum(1 for s in statuses if s["alive"] and s["logged_in"])
    total = len(statuses)

    if alive_count == 0:
        status = "down"
    elif logged_in_count < total:
        status = "degraded"
    else:
        status = "ok"

    return {
        "status": status,
        "workers": statuses,
        "workers_available": sum(1 for s in statuses if s["alive"] and s["logged_in"] and not s["busy"]),
        "workers_total": total,
        "queue_waiting": worker_pool.waiting_count,
        "uptime_seconds": round(time.time() - _start_time),
    }


@app.post("/api/new-chat")
async def api_new_chat():
    """手動重置所有 worker 的 Gemini 對話"""
    from .gemini import new_chat
    results = []
    for i, bm in enumerate(worker_pool._workers):
        if bm.page:
            ok = await new_chat(bm.page)
            results.append({"worker": i, "success": ok})
    return {"results": results}


# ── Google GenAI API 相容端點 ──


def _extract_api_key(request: Request, key: str | None) -> str | None:
    """從 header 或 query string 提取 API key"""
    header_key = request.headers.get("x-goog-api-key")
    if header_key:
        return header_key
    return key


def _verify_api_key(request: Request, key: str | None):
    """驗證 API 金鑰。完全沒設過任何金鑰（.env 跟 admin webui 都沒有）時維持
    原本的開放行為（開發/預設情境）；只要設過一把，就一定要帶對的 key。"""
    actual_key = _extract_api_key(request, key)
    if _is_valid_api_key(actual_key):
        return
    if not settings.api_keys and not admin_db.has_any_dynamic_key():
        return
    raise HTTPException(status_code=403, detail="Invalid API key")


async def _generate_content_impl(
    model: str, body: dict, request: Request | None = None, api_key_name: str | None = None
) -> dict:
    """Google GenAI API 相容端點的核心邏輯,可被 streaming / non-streaming 共用。"""

    # 前置驗證: dump 完整 body 結構 (隱藏 base64 內容避免 log 爆炸)
    # 用來確認 openclaw 多模態請求格式 — 看 inlineData 真的是 contents[].parts[]
    # 還是被包在某個 tool call 裡
    def _redact(obj, max_depth=8, depth=0):
        if depth > max_depth:
            return "<too-deep>"
        if isinstance(obj, dict):
            out = {}
            for k, v in obj.items():
                if k == "data" and isinstance(v, str) and len(v) > 100:
                    out[k] = f"<base64 {len(v)} chars>"
                elif k == "text" and isinstance(v, str) and len(v) > 200:
                    out[k] = v[:200] + f"...<{len(v)-200} more>"
                else:
                    out[k] = _redact(v, max_depth, depth + 1)
            return out
        if isinstance(obj, list):
            return [_redact(x, max_depth, depth + 1) for x in obj]
        return obj

    import json as _json_dbg
    logger.info("RAW BODY KEYS: %s", list(body.keys()))
    logger.info("RAW BODY: %s", _json_dbg.dumps(_redact(body), ensure_ascii=False)[:4000])

    # 透過 adapter 把完整 request body (含 systemInstruction / tools / 多輪歷史)
    # 攤平成單段 prompt。has_function_tools 決定後續是否要嘗試解析 tool_call。
    prompt, has_function_tools, allowed_tool_names = build_prompt(body)

    # Debug: 觀察 prompt 規模,multi-turn 累積後可能超大導致 Gemini Web 卡住
    contents_count = len(body.get("contents", []) or [])
    # 標的是「google-genai 相容端點」這條路,不是某個特定 caller —
    # openclaw / catime / 新聞 agent 都走這條,別再拿消費者名當標籤。
    logger.info(
        "genai request: prompt=%d chars, turns=%d, tools=%d, has_tool_call=%s",
        len(prompt), contents_count, len(allowed_tool_names), has_function_tools,
    )

    if not prompt.strip():
        raise HTTPException(status_code=400, detail="No content in request")

    tools = body.get("tools", []) or []
    has_google_search = any(
        "google_search" in t or "googleSearch" in t
        for t in tools
        if isinstance(t, dict)
    )
    if has_google_search:
        prompt = f"請搜尋最新的即時資訊來回答以下問題（{time.strftime('%Y-%m-%d')}）：\n\n{prompt}"

    gen_config = body.get("generationConfig", {})
    response_mime = gen_config.get("responseMimeType", "")
    response_modalities = gen_config.get("responseModalities", []) or []
    # 大小寫不敏感比對 (openclaw 送 "IMAGE" 全大寫,Google SDK 送 "Image",
    # 文件範例又有 "image";三種都接)
    modalities_lower = {str(m).lower() for m in response_modalities}
    is_image = (
        response_mime.lower().startswith("image/")
        or "image" in modalities_lower
    )

    # 強制 JSON 回應（模擬 responseMimeType: application/json）
    # 注意: 若已注入 tool_call 指令就不再疊加,避免兩種 JSON 規範打架。
    if response_mime == "application/json" and not is_image and not has_function_tools:
        prompt = (
            "You MUST respond in valid JSON format only. "
            "No markdown, no code blocks, no extra explanation. "
            "Output raw JSON.\n\n" + prompt
        )

    # 帶圖 + 要圖 → 走 edit（image-to-image）。不轉的話 build_prompt 會把
    # inlineData 換成 "[inline_data:image/png]" 這串字，圖的位元組就在那一步
    # 掉了，等於「拿純文字 prompt 生圖」，呼叫端送的參考圖形同沒送（而且是
    # HTTP 200，沒有任何錯誤）。ai-brain-site 的格莉奇日記踩了整整一季。
    ref_image = _first_inline_image(body) if is_image else None
    if ref_image:
        # 佔位字串留著只會干擾模型，走 edit 時圖是真的送進去的
        prompt = re.sub(r"\[inline_data:[^\]]*\]\s*", "", prompt).strip()
        kind, extra = "edit", {"reference_image": ref_image}
        logger.info("帶參考圖(%d chars base64)，改走 edit", len(ref_image))
    else:
        kind, extra = ("generate" if is_image else "chat"), None
    timeout = settings.default_timeout

    try:
        result = await _dispatch_and_log(kind, prompt, model, timeout,
                                         extra=extra, request=request, api_key_name=api_key_name)
    except QueueFullError:
        raise HTTPException(status_code=429, detail="Queue full")
    except asyncio.TimeoutError:
        raise HTTPException(status_code=408, detail="Request timeout")

    if not result.get("success"):
        return {
            "error": {
                "code": 400,
                "message": result.get("message", result.get("error", "Unknown error")),
                "status": "FAILED_PRECONDITION",
            }
        }

    # JSON 回應清理（Gemini 網頁版可能加 "JSON\n" 前綴或 code block）
    if not is_image and result.get("text") and not has_function_tools:
        text = result["text"].strip()
        # 去掉 ```json ... ``` code block
        m = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', text)
        if m:
            text = m.group(1).strip()
        # 去掉 "JSON\n" 前綴
        if text.upper().startswith("JSON"):
            text = text[4:].lstrip()
        result["text"] = text

    if is_image:
        parts: list[dict] = []
        for img_data in result.get("images", []):
            if "," in img_data:
                header, b64 = img_data.split(",", 1)
                mime = header.split(":")[1].split(";")[0] if ":" in header else "image/png"
            else:
                b64 = img_data
                mime = "image/png"
            parts.append({"inlineData": {"mimeType": mime, "data": b64}})
        finish_reason = "STOP"
    else:
        parts, finish_reason = build_response_parts(
            result.get("text", ""),
            has_function_tools,
            allowed_tool_names=allowed_tool_names,
        )

    return {
        "candidates": [
            {
                "content": {
                    "parts": parts,
                    "role": "model",
                },
                "finishReason": finish_reason,
            }
        ],
        "modelVersion": result.get("actual_model", model) if is_image else model,
    }


# ── 對外 endpoints (含 /v1beta 與根路徑兩種前綴) ─────────────────────


@app.post("/v1beta/models/{model}:generateContent")
@app.post("/models/{model}:generateContent")
async def genai_generate_content(model: str, request: Request, key: str = Query(default=None)):
    """Google GenAI API 相容端點 (非串流)"""
    _verify_api_key(request, key)
    body = await request.json()
    return await _generate_content_impl(model, body, request=request)


@app.post("/v1beta/models/{model}:streamGenerateContent")
@app.post("/models/{model}:streamGenerateContent")
async def genai_stream_generate_content(
    model: str,
    request: Request,
    key: str = Query(default=None),
    alt: str = Query(default="sse"),
):
    """
    Google GenAI API 相容端點 (串流)。

    Gemini Web 本身沒有 streaming,所以這裡用「假串流」:
    1. 開一個 task 在背景跑 _generate_content_impl
    2. 主迴圈每 15 秒 yield 一個 SSE comment (keep-alive),讓 client 端
       (例如 openclaw) 不會認為 connection idle 而 abort
    3. 背景 task 完成後,yield 真正的 data chunk 並結束 stream
    """
    _verify_api_key(request, key)
    body = await request.json()

    async def event_stream():
        # 1. 立刻送一個 keep-alive comment,讓 client 知道 stream 已建立
        yield ": stream-open\n\n"

        # 2. 在背景跑核心邏輯
        task = asyncio.create_task(_generate_content_impl(model, body, request=request))

        # 3. 每 15 秒一個 SSE comment 心跳;SSE 規格中以 ":" 開頭的行是 comment,
        #    client 端會忽略內容但 TCP 層收到資料就會 reset idle timer
        try:
            while not task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                break
        except asyncio.CancelledError:
            task.cancel()
            raise

        # 4. 取結果並 yield 真正的 data
        try:
            result = task.result()
        except HTTPException as e:
            err = {"error": {"code": e.status_code, "message": e.detail, "status": "FAILED_PRECONDITION"}}
            yield f"data: {json.dumps(err)}\n\n"
            return
        except Exception as e:
            err = {"error": {"code": 500, "message": str(e), "status": "INTERNAL"}}
            yield f"data: {json.dumps(err)}\n\n"
            return

        # 5. 一次吐出完整結果。Google SSE 格式: 每筆事件都是 `data: <json>\n\n`
        yield f"data: {json.dumps(result)}\n\n"

    media_type = "text/event-stream" if alt == "sse" else "application/json"
    return StreamingResponse(event_stream(), media_type=media_type)


# ── 非同步 job：送出拿 id、之後輪詢 ─────────────────────────────────
# 出圖 30-300 秒，同步端點撐不過 Cloudflare Worker / nginx 預設逾時。
# body 跟 generateContent 完全一樣，多一個 model 欄位，消費端不用學新格式。

# asyncio.create_task() 回傳的 Task，event loop 只留弱引用；沒有人握著強
# 引用的話，這個 Task 隨時可能在跑到一半時被 GC 回收 —— 而 _run_job 一開
# 頭就把 job 標成 running，被回收就等於卡死在 running 到下次服務重啟才被
# fail_stale_running() 清掉，違反「每一條離開 running 的路都要有出口」。
# 做法比照 worker_pool.py 的 _pending_resets 慣例：留一個地方存 Task，
# 完成時用 done callback 自動從集合裡移除，不會無限長大。
_background_tasks: set[asyncio.Task] = set()


@app.post("/api/jobs", status_code=202)
async def create_job(request: Request, key: str = Query(default=None)):
    _verify_api_key(request, key)
    body = await request.json()
    # settings 沒有 default_image_model 這個欄位（查過 config.py 確認過，
    # 實際圖片模型 id 定義在 GEMINI_OFFICIAL_MODEL/selectors.py），這裡沒有
    # 現成常數可引用，就用字面值當 fallback；消費端多半會自己帶 model。
    model = body.pop("model", None) or "gemini-2.5-flash-image"
    if not body.get("contents"):
        raise HTTPException(status_code=400, detail="No content in request")
    key_name = _identify_caller(request)
    job_id = jobs.create(model, body, key_name)
    task = asyncio.create_task(_run_job(job_id, model, body, key_name))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return {"id": job_id, "status": "queued"}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str, request: Request, key: str = Query(default=None)):
    _verify_api_key(request, key)
    row = jobs.get(job_id, _identify_caller(request))
    if row is None:
        raise HTTPException(status_code=404, detail="job not found")
    return {
        "id": row["id"],
        "status": row["status"],
        "response": row["response"],
        "error": row["error"],
        "created_at": row["created_at"],
    }


async def _run_job(job_id: str, model: str, body: dict, key_name: str | None) -> None:
    jobs.mark_running(job_id)
    try:
        result = await _generate_content_impl(model, body, request=None, api_key_name=key_name)
        jobs.finish(job_id, result)
    except HTTPException as e:
        jobs.fail(job_id, f"{e.status_code}: {e.detail}")
    except Exception as e:  # noqa: BLE001 — job 失敗要記下來，不能讓它卡在 running
        logger.exception("job %s 失敗", job_id)
        jobs.fail(job_id, f"{type(e).__name__}: {e}")
