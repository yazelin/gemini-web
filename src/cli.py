"""Gemini Image CLI — 命令列生圖工具"""
import argparse
import asyncio
import base64
import logging
import shutil
import subprocess
import sys
from pathlib import Path



def _get_commands_dir() -> Path:
    """取得 package 內建 commands 目錄路徑"""
    return Path(__file__).parent / "commands"


def _install_commands():
    """偵測 Claude Code / Gemini CLI 並安裝對應 commands"""
    commands_src = _get_commands_dir()
    if not commands_src.exists():
        print("警告：找不到內建 commands 目錄，跳過 AI Agent 整合")
        return

    installed = []

    # Claude Code: 複製 .md 到 ~/.claude/commands/gemini-web/
    claude_dir = Path.home() / ".claude"
    if claude_dir.exists():
        dest = claude_dir / "commands" / "gemini-web"
        dest.mkdir(parents=True, exist_ok=True)
        for f in commands_src.glob("*.md"):
            shutil.copy2(f, dest / f.name)
        installed.append(f"Claude Code → {dest}")

    # Gemini CLI: 複製 .toml 到 ~/.gemini/commands/gemini-web/
    gemini_dir = Path.home() / ".gemini"
    if gemini_dir.exists():
        dest = gemini_dir / "commands" / "gemini-web"
        dest.mkdir(parents=True, exist_ok=True)
        for f in commands_src.glob("*.toml"):
            shutil.copy2(f, dest / f.name)
        installed.append(f"Gemini CLI  → {dest}")

    if installed:
        print("\nAI Agent commands 已安裝：")
        for line in installed:
            print(f"  {line}")
        print("可用指令：/gemini-web, /generate")
    else:
        print("\n未偵測到 Claude Code 或 Gemini CLI，跳過 commands 安裝")
        print("如需手動安裝，請參考：https://github.com/yazelin/gemini-web#ai-agent-整合")


def _setup_logging(verbose: bool = False):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )


async def _do_login():
    """開啟瀏覽器讓用戶手動登入 Google"""
    from .browser import BrowserManager
    bm = BrowserManager(headless=False)  # login 一律 headed
    await bm.start()

    print("\n瀏覽器已開啟，請登入 Google 帳號。")
    print("登入完成後按 Enter 關閉瀏覽器...")
    await asyncio.get_event_loop().run_in_executor(None, input)

    await bm.stop()
    print("登入狀態已儲存。之後可以用 headless 模式生圖。")


async def _do_chat(prompt: str, verbose: bool):
    """文字對話"""
    from .browser import BrowserManager
    from .gemini import chat, new_chat
    from .config import settings

    bm = BrowserManager(headless=True)
    await bm.start()

    try:
        page = bm.page
        if not page:
            print("錯誤：瀏覽器未啟動", file=sys.stderr)
            sys.exit(1)

        logged_in = await bm.is_logged_in(wait=True)
        if not logged_in:
            print("錯誤：尚未登入 Google，請先執行 `gemini-web login`", file=sys.stderr)
            sys.exit(1)

        print(f"提問中... prompt: {prompt[:60]}{'...' if len(prompt) > 60 else ''}")

        result = await chat(page, prompt, timeout=settings.default_timeout)
        await new_chat(page)

        if not result.get("success"):
            error = result.get("error", "unknown")
            message = result.get("message", "")
            print(f"失敗 [{error}]：{message}", file=sys.stderr)
            sys.exit(1)

        print(result.get("text", ""))
        print(f"\n（{result.get('elapsed_seconds', 0)}秒）", file=sys.stderr)

    finally:
        await bm.stop()


async def _do_generate(prompt: str, output: str, no_watermark: bool, verbose: bool):
    """生成圖片"""
    from .browser import BrowserManager
    from .gemini import generate_image, new_chat
    from .config import settings

    bm = BrowserManager(headless=True)  # generate 一律 headless
    await bm.start()

    try:
        page = bm.page
        if not page:
            print("錯誤：瀏覽器未啟動", file=sys.stderr)
            sys.exit(1)

        # 檢查登入狀態（等待頁面完全載入）
        logged_in = await bm.is_logged_in(wait=True)
        if not logged_in:
            print("錯誤：尚未登入 Google，請先執行 `gemini-web login`", file=sys.stderr)
            sys.exit(1)

        print(f"生成中... prompt: {prompt[:60]}{'...' if len(prompt) > 60 else ''}")

        result = await generate_image(page, prompt, timeout=settings.default_timeout)
        await new_chat(page)

        if not result.get("success"):
            error = result.get("error", "unknown")
            message = result.get("message", "")
            print(f"失敗 [{error}]：{message}", file=sys.stderr)
            sys.exit(1)

        images = result.get("images", [])
        if not images:
            print("失敗：無圖片資料", file=sys.stderr)
            sys.exit(1)

        # 儲存圖片
        output_path = Path(output)
        for i, img_data in enumerate(images):
            if "," in img_data:
                header, b64 = img_data.split(",", 1)
            else:
                b64 = img_data

            raw = base64.b64decode(b64)

            if len(images) == 1:
                save_path = output_path
            else:
                stem = output_path.stem
                ext = output_path.suffix
                save_path = output_path.parent / f"{stem}_{i}{ext}"

            save_path.parent.mkdir(parents=True, exist_ok=True)
            save_path.write_bytes(raw)

            # 去水印
            if no_watermark:
                from .watermark import remove_watermark
                remove_watermark(str(save_path))
                print(f"已存檔（去水印）：{save_path}")
            else:
                print(f"已存檔：{save_path}")

        print(f"完成（{result.get('elapsed_seconds', 0)}秒）")

    finally:
        await bm.stop()


def main():
    parser = argparse.ArgumentParser(
        prog="gemini-web",
        description="Gemini Image — AI 圖片生成 CLI 工具",
    )
    sub = parser.add_subparsers(dest="command", help="可用指令")

    # install
    sub.add_parser("install", help="安裝 Chromium 瀏覽器（首次使用前必須執行）")

    # login
    login_parser = sub.add_parser("login", help="開啟瀏覽器登入 Google")

    # chat
    chat_parser = sub.add_parser("chat", help="文字對話")
    chat_parser.add_argument("prompt", help="提問內容")
    chat_parser.add_argument("-v", "--verbose", action="store_true", help="顯示詳細 log")

    # generate
    gen_parser = sub.add_parser("generate", help="生成圖片")
    gen_parser.add_argument("prompt", help="圖片描述（建議英文）")
    gen_parser.add_argument("-o", "--output", default="output.png", help="輸出檔案路徑（預設 output.png）")
    gen_parser.add_argument("--no-watermark", action="store_true", help="自動移除可見水印")
    gen_parser.add_argument("-v", "--verbose", action="store_true", help="顯示詳細 log")

    # health
    health_parser = sub.add_parser("health", help="檢查服務狀態（API 模式）")

    # serve
    serve_parser = sub.add_parser("serve", help="啟動 API 服務")
    serve_parser.add_argument("--host", default="0.0.0.0")
    serve_parser.add_argument("--port", type=int, default=8070)

    args = parser.parse_args()

    if args.command == "install":
        # 1. 安裝 Chromium
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"])

        # 2. 安裝 AI Agent commands
        _install_commands()

        print("\n下一步：執行 `gemini-web login` 登入 Google")

    elif args.command == "login":
        asyncio.run(_do_login())

    elif args.command == "chat":
        _setup_logging(getattr(args, "verbose", False))
        asyncio.run(_do_chat(args.prompt, getattr(args, "verbose", False)))

    elif args.command == "generate":
        _setup_logging(getattr(args, "verbose", False))
        asyncio.run(_do_generate(args.prompt, args.output, args.no_watermark, getattr(args, "verbose", False)))

    elif args.command == "serve":
        import uvicorn
        uvicorn.run("src.main:app", host=args.host, port=args.port)

    elif args.command == "health":
        import httpx
        from .config import settings
        try:
            resp = httpx.get(f"http://localhost:{settings.port}/api/health", timeout=5)
            import json
            print(json.dumps(resp.json(), indent=2, ensure_ascii=False))
        except Exception as e:
            print(f"服務未啟動或無法連線：{e}", file=sys.stderr)
            sys.exit(1)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
