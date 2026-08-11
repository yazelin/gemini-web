"""函式裡不准再 `import re` —— 那會把模組層的 re 遮成區域變數。

2026-08-11 踩過:在 `_generate_content_impl` 前段用了模組層的 `re`,但同一個
函式後段留著一行 `import re`,於是 `re` 整個函式都算區域名字,前段那次使用
變成 UnboundLocalError,帶參考圖的請求全部 HTTP 500。單元測試抓不到(那段
要真的走到帶圖分支才會炸),所以改用 AST 直接檢查。
"""
import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"


def _module_level_names(tree: ast.Module) -> set[str]:
    names = set()
    for node in tree.body:                      # 只看 top-level，不 walk 進函式
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname or alias.name)
    return names


def _shadowing_imports(path: Path):
    """只挑「模組層已經 import 過、函式裡又 import 一次」的那種。

    模組層沒有的名字在函式裡 import 是正常寫法（延遲載入、只有某條路徑要用），
    不能一併罵下去。
    """
    tree = ast.parse(path.read_text("utf-8"))
    top = _module_level_names(tree)
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(func):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    name = (alias.asname or alias.name).split(".")[0]
                    if name in top:
                        yield f"{path.name}:{node.lineno} {func.name}() import {name}"


def test_no_function_level_reimport_of_module_level_names():
    offenders = [o for p in SRC.rglob("*.py") for o in _shadowing_imports(p)]
    assert not offenders, (
        "函式裡重複 import 模組層已經有的名字，會把它遮成區域變數："
        + "; ".join(offenders)
    )
