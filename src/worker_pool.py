"""Worker Pool — 管理多個 BrowserManager 實例並行處理請求"""
import asyncio
import base64
import logging
import tempfile
import time
from pathlib import Path
from typing import Any

from .browser import BrowserManager
from .config import settings, get_worker_profile_dir
from .gemini import chat, generate_image, new_chat, switch_model
from .selectors import IMAGE_FALLBACK_MAP

logger = logging.getLogger(__name__)


class QueueFullError(Exception):
    """等待佇列已滿"""
    pass


DISPATCH_MODES = ("round-robin", "spillover")


class WorkerPool:
    """管理 N 個 BrowserManager，依 dispatch mode 分配請求

    mode:
      round-robin — 從上次挑到的下一個 worker 開始找空閒的,序列流量會 0→1→0→1
                    輪流,讓各 Gmail 帳號平均分攤配額(預設)。
      spillover   — 永遠從 worker 0 開始找第一個空閒的,worker 1+ 只在前面全忙時
                    才被叫到(舊行為,單帳號扛主力、其餘備援)。
    """

    def __init__(self, worker_count: int | None = None, max_waiting: int = 10) -> None:
        self._count = worker_count or settings.worker_count
        self._max_waiting = max_waiting
        self._mode = "round-robin"
        self._next = 0  # round-robin 指標
        self._workers: list[BrowserManager] = []
        # 空閒 worker id 的集合 + 一個「有 worker 空出來了」的訊號。
        # 取 worker = 從 set 挑一個移除(同步、原子);還 worker = 加回 set(同步)。
        # 關鍵:_release 是同步的,放進 finally 裡不會被 cancel 打斷 → worker 一定
        # 還得回來,不會像舊版 lock.acquire()+cancel 那樣洩漏鎖把 worker 卡死。
        self._idle_ids: set[int] = set()
        self._free: asyncio.Event | None = None
        # 每個 worker 的「待完成 reset」task。下次請求進來時必須先 await
        # 這個 task,確保上一次的對話已經乾淨重置才開始新請求。
        # 但 _run 不會 block 在 reset 上 — 它會提前 return result,讓 client
        # (例如 openclaw) 在 60 秒 timeout 內收到回應。
        self._pending_resets: list[asyncio.Task | None] = []
        self._waiting = 0

    async def start(self) -> None:
        """啟動所有 worker 的瀏覽器"""
        self._free = asyncio.Event()
        for i in range(self._count):
            profile_dir = get_worker_profile_dir(i)
            bm = BrowserManager(profile_dir=profile_dir)
            await bm.start()
            self._workers.append(bm)
            self._pending_resets.append(None)
            self._idle_ids.add(i)
            logger.info("Worker %d 已啟動（profile: %s）", i, profile_dir)
        self._free.set()

    async def stop(self) -> None:
        """關閉所有 worker"""
        for i, bm in enumerate(self._workers):
            await bm.stop()
            logger.info("Worker %d 已關閉", i)

    async def dispatch(self, kind: str, prompt: str, model: str, timeout: int,
                       extra: dict | None = None, ctx: dict | None = None) -> dict:
        """分配請求到空閒 worker，全忙則等待

        Args:
            kind: "chat" / "generate" / "edit"
            extra: edit 模式用，dict 含 reference_image (base64 或 data URL 字串)

        Raises:
            QueueFullError: 等待數超過上限
            asyncio.TimeoutError: 等待超過 timeout
        """
        if self._waiting >= self._max_waiting:
            raise QueueFullError(f"等待佇列已滿（{self._max_waiting}）")

        self._waiting += 1
        try:
            return await asyncio.wait_for(
                self._acquire_and_run(kind, prompt, model, timeout, extra, ctx),
                timeout=timeout,
            )
        finally:
            self._waiting -= 1

    def _select(self) -> int:
        """從 _idle_ids 依 dispatch mode 挑一個 worker 並「取走」(從 set 移除)。

        呼叫前必須確定 _idle_ids 非空。純同步、無 await → check 到取走之間不會
        被別的 coroutine 插隊(single-thread event loop 的原子區間)。
        """
        if self._mode == "round-robin":
            for off in range(self._count):
                wid = (self._next + off) % self._count
                if wid in self._idle_ids:
                    self._next = (wid + 1) % self._count
                    self._idle_ids.discard(wid)
                    return wid
        # spillover:永遠挑編號最小的空閒 worker(worker 0 主力)
        wid = min(self._idle_ids)
        self._idle_ids.discard(wid)
        return wid

    async def _acquire(self) -> int:
        """等到有空閒 worker,取走一個並回傳其 id(全忙就 await 到有人還回來)。"""
        assert self._free is not None, "pool 尚未 start()"
        while not self._idle_ids:
            self._free.clear()
            await self._free.wait()
        wid = self._select()
        if not self._idle_ids:
            self._free.clear()
        return wid

    def _release(self, wid: int) -> None:
        """把 worker 還回空閒池。同步、絕不 await → 可安全放進 finally,
        即使請求被 cancel / 逾時 / 拋錯,worker 也一定回得來。"""
        self._idle_ids.add(wid)
        if self._free is not None:
            self._free.set()

    def _schedule_reset(self, worker_id: int) -> None:
        """排一次頁面重置,並記進 _pending_resets 讓下一筆請求先 await 它。"""
        bm = self._workers[worker_id]
        if not bm.page:
            return
        logger.warning("Worker %d 請求中斷,排入頁面重置", worker_id)
        self._pending_resets[worker_id] = asyncio.create_task(new_chat(bm.page))

    async def _acquire_and_run(self, kind: str, prompt: str, model: str, timeout: int,
                               extra: dict | None = None, ctx: dict | None = None) -> dict:
        """取一個空閒 worker 跑請求,結束/逾時/出錯都保證把它還回池子。"""
        wid = await self._acquire()
        if ctx is not None:
            # 讓呼叫端在「逾時被取消」時仍知道是哪個 worker(否則 admin history
            # 的失敗列 worker 欄是空的,查不出兇手)
            ctx["worker_id"] = wid
        finished = False
        try:
            result = await self._run(wid, kind, prompt, model, timeout, extra)
            finished = True
            return result
        finally:
            if not finished:
                # 被取消(API 層 wait_for 逾時)或丟例外 → _run 尾端的重置沒跑到,
                # 頁面停在半路(選單開著/舊對話),下一筆接手就會連環失敗
                # (2026-07-30 worker 3 就是這樣一路壞下去)。補排一次重置。
                self._schedule_reset(wid)
            self._release(wid)

    async def _run(self, worker_id: int, kind: str, prompt: str, model: str, timeout: int, extra: dict | None = None) -> dict:
        """在指定 worker 上執行請求"""
        bm = self._workers[worker_id]
        page = bm.page
        if not page:
            return {"success": False, "error": "browser_error", "message": f"Worker {worker_id} 瀏覽器未啟動", "worker_id": worker_id}

        logger.info("Worker %d 處理請求：%s", worker_id, kind)
        start = time.time()

        # 上一次的 reset 還沒做完?先等它完成,保證頁面狀態乾淨
        prev_reset = self._pending_resets[worker_id]
        if prev_reset is not None and not prev_reset.done():
            try:
                await prev_reset
            except Exception as e:
                logger.warning("Worker %d 上次 reset 失敗: %s", worker_id, e)
        self._pending_resets[worker_id] = None

        if model:
            await switch_model(page, model)

        if kind == "chat":
            result = await chat(page, prompt, timeout)
        elif kind == "edit":
            from .gemini import edit_image
            ref_b64 = (extra or {}).get("reference_image", "")
            if not ref_b64:
                self._pending_resets[worker_id] = asyncio.create_task(new_chat(page))
                return {"success": False, "error": "invalid_input", "message": "edit 需要 reference_image", "worker_id": worker_id}
            # 強制切到 Banana 模型（網頁版「快捷」），普通 chat 模式不會做 image-to-image
            await switch_model(page, "gemini-3.1-flash-image-preview")
            result = await edit_image(page, prompt, ref_b64, timeout)
            # edit 模式同 generate：成功時做去浮水印
            if result.get("success") and result.get("images"):
                result["images"] = await asyncio.get_event_loop().run_in_executor(
                    None, _remove_watermarks, result["images"]
                )
        else:
            result = await generate_image(page, prompt, timeout)

            # Pro 圖片生成失敗 → 自動 fallback 到 Flash 重試
            fallback_model = IMAGE_FALLBACK_MAP.get(model) if model else None
            if fallback_model and not result.get("success"):
                logger.info(
                    "Worker %d 圖片生成失敗（%s: %s），fallback 到 %s 重試",
                    worker_id, model, result.get("error", ""), fallback_model,
                )
                await new_chat(page)
                await switch_model(page, fallback_model)
                remaining = timeout - int(time.time() - start)
                if remaining > 30:
                    result = await generate_image(page, prompt, remaining)
                    if result.get("success"):
                        result["actual_model"] = fallback_model
                else:
                    logger.warning("Worker %d fallback 剩餘時間不足 (%ds)，跳過", worker_id, remaining)

            if result.get("success") and result.get("images"):
                result["images"] = await asyncio.get_event_loop().run_in_executor(
                    None, _remove_watermarks, result["images"]
                )

        # Fire-and-forget reset:return result 後在背景重置對話頁面。
        # 下次 _run 進來時會 await 這個 task,確保乾淨狀態。
        # 對 image gen 特別重要 — openclaw 對 image gen 有 60 秒硬編碼 timeout。
        self._pending_resets[worker_id] = asyncio.create_task(new_chat(page))
        result["worker_id"] = worker_id
        return result

    async def worker_status(self, include_account: bool = False) -> list[dict]:
        """回傳每個 worker 的狀態。

        include_account=True 時多帶一個 masked 的登入帳號(讀 profile 的
        Chrome Preferences)。預設 False — 公開的 /api/health 不洩帳號身分,
        只有登入後的 /admin 才帶。
        """
        statuses = []
        for i, bm in enumerate(self._workers):
            alive = await bm.is_alive()
            logged_in = await bm.is_logged_in() if alive else False
            s = {
                "id": i,
                "alive": alive,
                "logged_in": logged_in,
                "busy": i not in self._idle_ids,
            }
            if include_account:
                s["account"] = _profile_account(get_worker_profile_dir(i))
            statuses.append(s)
        return statuses

    @property
    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> None:
        """切換分配模式。未知值回退 round-robin（不 raise，admin 表單容錯）。"""
        self._mode = mode if mode in DISPATCH_MODES else "round-robin"

    @property
    def waiting_count(self) -> int:
        return self._waiting

    @property
    def worker_count(self) -> int:
        return self._count


_account_cache: dict[str, str] = {}


def _mask_email(email: str) -> str:
    """ya***@gmail.com — 留頭 2 碼,遮 local part 其餘。"""
    if "@" not in email:
        return email
    local, domain = email.split("@", 1)
    head = local[:2]
    return f"{head}{'*' * max(3, len(local) - 2)}@{domain}"


def _profile_account(profile_dir: str) -> str:
    """讀 Chrome profile 的登入 email(masked)。讀不到回空字串。

    帳號不會變,快取一份;profile 目錄當 key。
    """
    if profile_dir in _account_cache:
        return _account_cache[profile_dir]
    account = ""
    try:
        import json

        prefs = Path(profile_dir) / "Default" / "Preferences"
        data = json.loads(prefs.read_text(encoding="utf-8"))
        info = data.get("account_info") or []
        email = (info[0].get("email") if info else "") or ""
        account = _mask_email(email) if email else ""
    except (OSError, ValueError, KeyError, IndexError):
        account = ""
    _account_cache[profile_dir] = account
    return account


def _remove_watermarks(images: list[str]) -> list[str]:
    """對 base64 圖片列表去水印（從 main.py 搬過來）"""
    from .watermark import remove_watermark

    cleaned = []
    for img_data in images:
        try:
            if "," in img_data:
                header, b64 = img_data.split(",", 1)
            else:
                header, b64 = "data:image/png;base64", img_data

            raw_bytes = base64.b64decode(b64)

            if raw_bytes[:8] == b'\x89PNG\r\n\x1a\n':
                ext, actual_ct = "png", "image/png"
            elif raw_bytes[:2] == b'\xff\xd8':
                ext, actual_ct = "jpg", "image/jpeg"
            else:
                ext, actual_ct = "png", "image/png"

            with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as tmp:
                tmp.write(raw_bytes)
                tmp_path = tmp.name

            out_path = remove_watermark(tmp_path)
            raw = Path(out_path).read_bytes()
            new_b64 = base64.b64encode(raw).decode("ascii")
            cleaned.append(f"data:{actual_ct};base64,{new_b64}")

            Path(tmp_path).unlink(missing_ok=True)
            if out_path != tmp_path:
                Path(out_path).unlink(missing_ok=True)
        except Exception as e:
            logger.warning("去水印處理失敗：%s", e)
            cleaned.append(img_data)

    return cleaned
