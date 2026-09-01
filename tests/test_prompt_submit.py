"""送出 prompt 這件事本身要能被驗證，送不出去要立刻收工。

2026-08-31 影片那單白等了 535 秒：prompt 打進 composer 了，但 Enter 沒觸發
送出，字一直躺在框裡（診斷截圖 w2-20260831-152741-video-result.png）。當時
兩道防護網同時是壞的 ——

  1. 「有沒有送出」的檢查用 document.querySelector 重抓輸入框，影片頁上有
     不只一個 contenteditable，抓到的是另一個空的，於是誤判成已送出；
  2. 備援的送出鈕選擇器寫 aria-label='傳送'，實際是「傳送訊息」，配不上。

所以這裡把 Enter → 驗 → 點鈕 → 再驗 這條路整條釘住。
"""
from pathlib import Path

import pytest

import src.gemini as gemini
from src.selectors import SEND_BUTTON_CANDIDATES, SELECTORS


class _Handle:
    """composer 的 ElementHandle；text 會隨著送出而被清空。"""

    def __init__(self, text=""):
        self.text = text

    async def evaluate(self, _js):
        return self.text


class _Locator:
    def __init__(self, page, sel, exists=True, visible=True, enabled=True):
        self._page, self._sel = page, sel
        self._exists, self._visible, self._enabled = exists, visible, enabled

    @property
    def first(self):
        return self

    def nth(self, _i):
        return self

    async def count(self):
        return 1 if self._exists else 0

    async def is_visible(self):
        return self._visible

    async def is_enabled(self):
        return self._enabled

    async def click(self, timeout=None):
        self._page.clicked.append(self._sel)
        self._page.on_send_click()


class _Keyboard:
    def __init__(self, page):
        self._page = page

    async def press(self, key):
        self._page.keys.append(key)
        if key == "Enter":
            self._page.on_enter()


class _Page:
    """假 Gemini 頁面。

    buttons：{selector: (exists, visible, enabled)}，沒列到的一律不存在。
    doc_text：document.querySelector 這條路會讀到的字——刻意跟 handle 不同，
              用來抓「讀錯元素」這個 bug。
    """

    def __init__(self, handle, buttons=None, doc_text="",
                 enter_sends=False, click_sends=True):
        self.handle = handle
        self.buttons = buttons or {}
        self.doc_text = doc_text
        self._enter_sends, self._click_sends = enter_sends, click_sends
        self.keys, self.clicked = [], []
        self.keyboard = _Keyboard(self)

    def on_enter(self):
        if self._enter_sends:
            self.handle.text = ""

    def on_send_click(self):
        if self._click_sends:
            self.handle.text = ""

    def locator(self, sel):
        exists, visible, enabled = self.buttons.get(sel, (False, False, False))
        return _Locator(self, sel, exists, visible, enabled)

    async def evaluate(self, _js, _arg=None):
        return self.doc_text


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch):
    """把送出後的沉澱等待縮掉，測試不該花 3 秒。"""
    monkeypatch.setattr(gemini, "_SUBMIT_SETTLE_SECONDS", 0)


@pytest.fixture
def _no_dump(monkeypatch):
    calls = []

    async def _fake(page, tag, worker_id=None):
        calls.append(tag)
        return "/tmp/fake.png"

    monkeypatch.setattr(gemini, "dump_page_state", _fake)
    return calls


class TestSendSelector:
    def test_covers_the_real_aria_label(self):
        """2026-08-31 實測頁面上的按鈕是 aria-label='傳送訊息'。"""
        assert any("傳送訊息" in s for s in SEND_BUTTON_CANDIDATES)
        assert "傳送訊息" in SELECTORS["send"]

    def test_still_covers_english_and_the_old_label(self):
        joined = " ".join(SEND_BUTTON_CANDIDATES)
        assert "Send message" in joined
        assert "傳送" in joined

    def test_candidates_are_ordered_most_specific_first(self):
        """要按順序試，寬鬆的比對不能排在精確的前面搶到別的按鈕。"""
        assert "傳送訊息" in SEND_BUTTON_CANDIDATES[0]


class TestSubmitPrompt:
    @pytest.mark.asyncio
    async def test_enter_alone_is_enough(self):
        page = _Page(_Handle("貓寫 C#"), enter_sends=True)
        assert await gemini.submit_prompt(page, page.handle, "Create video") == ""
        assert page.keys == ["Enter"]
        assert page.clicked == []      # 送出去了就不該再多點一下

    @pytest.mark.asyncio
    async def test_reads_the_handle_not_document_query_selector(self):
        """影片頁有第二個空的 contenteditable，不能被它騙過去。

        document.querySelector 這條路讀到空字串，但手上的 handle 還有字——
        這正是 2026-08-31 那次誤判「已送出」的現場。
        """
        page = _Page(_Handle("貓寫 C#"), doc_text="",
                     buttons={SEND_BUTTON_CANDIDATES[0]: (True, True, True)},
                     enter_sends=False)
        assert await gemini.submit_prompt(page, page.handle, "Create video") == ""
        assert page.clicked == [SEND_BUTTON_CANDIDATES[0]]

    @pytest.mark.asyncio
    async def test_skips_invisible_send_buttons(self):
        """側欄那次就是點到隱藏元素乾等 10 秒，這裡不要重蹈。"""
        page = _Page(_Handle("貓寫 C#"),
                     buttons={SEND_BUTTON_CANDIDATES[0]: (True, False, True),
                              SEND_BUTTON_CANDIDATES[1]: (True, True, True)})
        assert await gemini.submit_prompt(page, page.handle, "Create video") == ""
        assert page.clicked == [SEND_BUTTON_CANDIDATES[1]]

    @pytest.mark.asyncio
    async def test_fails_fast_when_nothing_sends(self, _no_dump):
        """Enter 沒用、鈕也點不動 → 立刻回錯，不要讓上層等滿 580 秒。"""
        page = _Page(_Handle("貓寫 C#"), buttons={}, enter_sends=False)
        err = await gemini.submit_prompt(page, page.handle, "Create video",
                                         tag="video")
        assert err
        assert "diagnostics/" in err
        assert _no_dump == ["video-not-sent"]

    @pytest.mark.asyncio
    async def test_fails_fast_when_the_click_does_not_take(self, _no_dump):
        """鈕點下去了但字還在框裡，一樣算沒送出。"""
        page = _Page(_Handle("貓寫 C#"),
                     buttons={SEND_BUTTON_CANDIDATES[0]: (True, True, True)},
                     enter_sends=False, click_sends=False)
        assert await gemini.submit_prompt(page, page.handle, "Create video")
        assert _no_dump == ["submit-not-sent"]

    @pytest.mark.asyncio
    async def test_falls_back_to_selector_when_the_handle_is_stale(self):
        """handle 因頁面重繪失效時退回選擇器，而且要看「全部」候選。"""
        class _Stale(_Handle):
            async def evaluate(self, _js):
                raise RuntimeError("Element is not attached to the DOM")

        page = _Page(_Stale("x"), doc_text="", enter_sends=False)
        assert await gemini.submit_prompt(page, page.handle, "Create video") == ""


class TestVerificationRobustness:
    """驗證這件事本身出錯，不該讓整單失敗 —— 還是要去等結果。

    讀不到 composer 內容跟「確定沒送出」是兩回事。前者只是我們瞎了，貿然
    回錯會把本來會成功的單也殺掉。
    """

    @pytest.mark.asyncio
    async def test_unreadable_composer_is_treated_as_sent(self):
        class _Blind:
            async def evaluate(self, _js):
                raise RuntimeError("Execution context was destroyed")

        class _BlindPage(_Page):
            async def evaluate(self, _js, _arg=None):
                raise RuntimeError("Execution context was destroyed")

        page = _BlindPage(_Blind(), enter_sends=False)
        assert await gemini.submit_prompt(page, page.handle, "Create video") == ""


class TestInputSelectorExcludesQuillClipboard:
    """`[contenteditable='true']` 會抓到 Quill 的隱藏貼上緩衝區。

    2026-08-31 頁面實證（Playwright 的 call log）：

        waiting for locator("[contenteditable='true']") to be visible
          35 × locator resolved to hidden
               <div tabindex="-1" class="ql-clipboard" contenteditable="true"></div>

    它排在真正的編輯器前面、永遠隱藏、永遠是空的。兩個災情都是它：
      - document.querySelector 讀它 → 「字還在框裡」被讀成空 → 誤判已送出（空等 535 秒）
      - wait_for_selector(state="visible") 等它變可見 → 白等滿 15 秒（側欄進影片頁失敗）
    """

    def test_input_selector_skips_ql_clipboard(self):
        assert ":not(.ql-clipboard)" in SELECTORS["input"]

    def test_input_selector_still_matches_contenteditable(self):
        assert "contenteditable='true'" in SELECTORS["input"]


class TestResultWaitBudget:
    """內層等待一定要比外層的 asyncio.wait_for 早收工，否則沒有診斷。

    2026-08-31：外層碼表從「請求進來」跑，內層卻從「prompt 送出」才開始算。
    進模式花了 67 秒（側欄 15 + 模式點擊 30 + 重置重試），內層截止點就落到外層
    後面 22 秒 —— 請求被取消，回 408，截圖與元素清單一個字都沒留下。
    """

    def test_fast_setup_leaves_almost_the_whole_budget(self):
        assert gemini._result_wait_budget(580, 0.0) == 580 - gemini._DIAGNOSTIC_MARGIN

    def test_slow_setup_is_deducted(self):
        """setup 花掉的時間要從預算裡扣掉，不能假裝沒發生。"""
        assert gemini._result_wait_budget(580, 67.0) == 580 - gemini._DIAGNOSTIC_MARGIN - 67

    @pytest.mark.parametrize("elapsed", [0, 1, 30, 67, 120, 400, 534, 535, 600])
    def test_inner_deadline_always_precedes_the_outer_one(self, elapsed):
        """核心不變式：setup + 等待 一定要小於外層 timeout。"""
        budget = gemini._result_wait_budget(580, elapsed)
        assert budget == 0 or budget + elapsed < 580

    def test_no_time_left_returns_zero_so_the_caller_can_bail(self):
        """剩餘時間不夠就回 0，讓呼叫端立刻存證收工，而不是硬等到被取消。"""
        assert gemini._result_wait_budget(580, 570.0) == 0
        assert gemini._result_wait_budget(580, 9999.0) == 0


class TestDiagnosticsRecordInputs:
    """卡住現場要記下「頁面上有哪些輸入元素」。

    2026-08-31：影片頁的 [contenteditable] 排除 .ql-clipboard 後一個都沒匹配到，
    代表那頁的輸入框是別種元素 —— 但當時的診斷只記按鈕，答不出是哪一種，只能
    再跑一次才知道。這份清單就是為了不要再跑第二次。
    """

    class _Page:
        url = "https://gemini.google.com/videos"

        def __init__(self):
            self.shot = None

        async def screenshot(self, path=None):
            self.shot = path

        async def title(self):
            return "Google Gemini"

        async def query_selector(self, _sel):
            return object()

        async def evaluate(self, js, _arg=None):
            if "textarea" in js:
                return [{"tag": "textarea", "cls": "video-prompt",
                         "placeholder": "描述你想生成的影片", "visible": True,
                         "text": ""}]
            return []

    @pytest.mark.asyncio
    async def test_dump_records_input_candidates(self, tmp_path, monkeypatch):
        import json

        from src.config import settings

        monkeypatch.setattr(settings, "data_dir", str(tmp_path))
        page = self._Page()
        path = await gemini.dump_page_state(page, "video-sidebar", 2)
        assert path
        info = json.loads(Path(path).with_suffix(".json").read_text(encoding="utf-8"))
        assert info["inputs"][0]["tag"] == "textarea"
        assert info["inputs"][0]["visible"] is True


class TestSidebarFailureLeavesEvidence:
    """側欄那條路是 8/22 驗過能產出 mp4 的路徑，它壞掉要看得到現場。"""

    class _Page:
        def __init__(self):
            self.dumped = []

        async def query_selector(self, _sel):
            return object()

        def locator(self, _sel):
            raise RuntimeError("boom")

    @pytest.mark.asyncio
    async def test_dumps_state_when_the_sidebar_path_throws(self, monkeypatch):
        dumped = []

        async def _fake(page, tag, worker_id=None):
            dumped.append((tag, worker_id))
            return None

        monkeypatch.setattr(gemini, "dump_page_state", _fake)
        page = self._Page()
        ok, _ = await gemini._enter_video_via_sidebar(page, None, 2)
        assert ok is False
        assert dumped == [("video-sidebar", 2)]


class TestGeminiErrorTextIsSharedByBothPaths:
    """Gemini 用文字回錯誤時，影片／音樂那條線也要立刻收工。

    2026-08-31 實證截圖 w2-20260831-162745-video-result.png：送出後 Gemini 幾秒
    就回了「I seem to be encountering an error. Can I try something else for
    you?」——那句話本來就在 _GENERIC_ERROR_PHRASES 裡，圖片那條線看到會馬上
    收工，但媒體那條線沒接上這份偵測，於是照樣空等 468 秒才回 timeout。
    """

    class _El:
        def __init__(self, text):
            self._text = text

        async def inner_text(self):
            return self._text

    class _Page:
        def __init__(self, texts):
            self._texts = texts

        async def query_selector_all(self, _sel):
            return [TestGeminiErrorTextIsSharedByBothPaths._El(t)
                    for t in self._texts]

    @pytest.mark.asyncio
    async def test_detects_the_exact_message_gemini_sent(self):
        page = self._Page(
            ["I seem to be encountering an error. Can I try something else for you?"])
        assert "encountering an error" in await gemini._gemini_error_text(page)

    @pytest.mark.asyncio
    async def test_normal_narration_is_not_an_error(self):
        """生成中 Gemini 常先講一段，不能當成失敗。"""
        page = self._Page(["Sure — here's a 10 second video of a cat coding."])
        assert await gemini._gemini_error_text(page) == ""

    @pytest.mark.asyncio
    async def test_no_response_yet_is_not_an_error(self):
        assert await gemini._gemini_error_text(self._Page([])) == ""

    @pytest.mark.asyncio
    async def test_only_the_latest_response_counts(self):
        """前一輪的錯誤訊息不該讓這一輪直接判死。"""
        page = self._Page(["Something went wrong", "Here is your video"])
        assert await gemini._gemini_error_text(page) == ""

    def test_media_path_consults_it(self):
        import inspect
        src = inspect.getsource(gemini._generate_media)
        assert "_gemini_error_text(" in src
        assert 'return _error("gemini_error"' in src

    def test_image_path_shares_the_same_helper(self):
        """兩條線用同一份清單，以後加字樣只要改一個地方。"""
        import inspect
        src = inspect.getsource(gemini._wait_for_image_or_error)
        assert "_gemini_error_text(" in src
