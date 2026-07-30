"""進不了「建立圖像」模式時的自我修復(_ensure_create_image_mode)

2026-07-30 worker 3 的故障:頁面停在舊對話上,composer 的「上傳與工具」鈕不在,
選擇器誤中「開啟對話動作選單」→ 選單裡沒有「建立圖像」→ 退回 prefix fallback →
純聊天不會產圖 → 空等 290 秒逾時。而且進了這個狀態就出不來,該 worker 之後每一筆
生圖都失敗(實測 6/6)。修法是第一次進不去就硬重置頁面再試一次。
"""
import pytest


class _FakePage:
    """只實作 _ensure_create_image_mode 會碰到的介面。"""

    async def wait_for_selector(self, *_a, **_k):
        return "input-after-reset"


@pytest.fixture
def patched(monkeypatch):
    """把外部互動換成可觀測的假物件,回傳呼叫記錄。"""
    import src.gemini as g

    calls = {"enter": [], "reset": 0, "dismiss": 0}

    async def fake_dismiss(page):
        calls["dismiss"] += 1

    monkeypatch.setattr(g, "_dismiss_onboarding", fake_dismiss)
    return g, calls


@pytest.mark.asyncio
async def test_first_attempt_succeeds_no_reset(patched, monkeypatch):
    """一次就進得去:不該重置,也不該重試。"""
    g, calls = patched

    async def enter(page, input_el):
        calls["enter"].append(input_el)
        return True, input_el

    async def new_chat(page):
        calls["reset"] += 1
        return True

    monkeypatch.setattr(g, "_enter_create_image_mode", enter)
    monkeypatch.setattr(g, "new_chat", new_chat)

    ok, el = await g._ensure_create_image_mode(_FakePage(), "orig-input")
    assert ok is True
    assert el == "orig-input"
    assert calls["reset"] == 0
    assert len(calls["enter"]) == 1


@pytest.mark.asyncio
async def test_recovers_by_hard_reset(patched, monkeypatch):
    """第一次進不去 → 重置頁面 → 第二次成功,並改用重置後的新輸入框。"""
    g, calls = patched

    async def enter(page, input_el):
        calls["enter"].append(input_el)
        return len(calls["enter"]) >= 2, input_el

    async def new_chat(page):
        calls["reset"] += 1
        return True

    monkeypatch.setattr(g, "_enter_create_image_mode", enter)
    monkeypatch.setattr(g, "new_chat", new_chat)

    ok, el = await g._ensure_create_image_mode(_FakePage(), "stale-input")
    assert ok is True, "重置後應該要救回來"
    assert calls["reset"] == 1, "應該剛好硬重置一次"
    assert calls["enter"] == ["stale-input", "input-after-reset"], "重試要用重置後的輸入框"
    assert calls["dismiss"] == 1, "重置後要再關一次 onboarding 橫幅"
    assert el == "input-after-reset"


@pytest.mark.asyncio
async def test_gives_up_after_second_failure(patched, monkeypatch):
    """兩次都進不去才放棄(呼叫端才會退回 prefix fallback),不無限重試。"""
    g, calls = patched

    async def enter(page, input_el):
        calls["enter"].append(input_el)
        return False, input_el

    async def new_chat(page):
        calls["reset"] += 1
        return True

    monkeypatch.setattr(g, "_enter_create_image_mode", enter)
    monkeypatch.setattr(g, "new_chat", new_chat)

    ok, _ = await g._ensure_create_image_mode(_FakePage(), "x")
    assert ok is False
    assert len(calls["enter"]) == 2
    assert calls["reset"] == 1


@pytest.mark.asyncio
async def test_reset_failure_is_not_fatal(patched, monkeypatch):
    """連重置都失敗時要乾脆回 False,不能丟例外炸掉整筆請求。"""
    g, calls = patched

    async def enter(page, input_el):
        calls["enter"].append(input_el)
        return False, input_el

    async def new_chat(page):
        calls["reset"] += 1
        return False

    monkeypatch.setattr(g, "_enter_create_image_mode", enter)
    monkeypatch.setattr(g, "new_chat", new_chat)

    ok, _ = await g._ensure_create_image_mode(_FakePage(), "x")
    assert ok is False
    assert len(calls["enter"]) == 1, "重置失敗就不該再試第二次"
