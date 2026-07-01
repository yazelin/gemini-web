"""官方 Gemini Developer API fallback。

瀏覽器路(免費但單線、脆)失敗或流量過大時,直接打 generativelanguage
的 :generateContent(付費、約 10s、穩)。請求/回應格式與 Vertex 相同。
回傳 data URL 列表,與 /api/edit、/api/generate 的 `images` 欄位一致。
"""
import asyncio
import json as _json
import logging
import urllib.request

from .config import settings

logger = logging.getLogger(__name__)

_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:generateContent?key={key}"
)


def _call_sync(prompt: str, image_b64: str | None, mime: str) -> list[str]:
    parts: list[dict] = []
    if image_b64:
        parts.append({"inlineData": {"mimeType": mime, "data": image_b64}})
    parts.append({"text": prompt})
    body = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "responseModalities": ["IMAGE"],
            "imageConfig": {"aspectRatio": "1:1"},
        },
    }
    url = _ENDPOINT.format(
        model=settings.gemini_official_model,
        key=settings.gemini_official_api_key,
    )
    req = urllib.request.Request(
        url,
        data=_json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = _json.loads(resp.read().decode("utf-8"))
    out: list[str] = []
    for cand in data.get("candidates", []):
        for p in cand.get("content", {}).get("parts", []):
            inl = p.get("inlineData") or p.get("inline_data")
            if inl and inl.get("data"):
                m = inl.get("mimeType") or inl.get("mime_type") or "image/png"
                out.append(f"data:{m};base64,{inl['data']}")
    return out


async def official_generate(
    prompt: str, image_b64: str | None = None, mime: str = "image/png"
) -> list[str]:
    """async 包裝(urllib 跑在 executor 避免卡事件迴圈)。無 key 回空 list;
    呼叫失敗會丟例外,由 caller 決定要不要吞。"""
    if not settings.gemini_official_api_key:
        return []
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _call_sync, prompt, image_b64, mime)
