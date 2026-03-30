# CLAUDE.md

## 發佈流程

不要用 `uv publish` 手動發佈。本專案使用 GitHub Release 觸發自動發佈：

1. bump `pyproject.toml` 中的 version
2. commit + push
3. 在 GitHub 上建立 Release（tag 格式：`v0.4.0`）
4. GitHub Actions（`.github/workflows/publish.yml`）自動 build + publish 到 PyPI

## 開發

```bash
uv sync --extra dev
uv run python -m pytest -v
```

## 安裝方式

必須用 `uv tool install` 或 `pipx install`，不要用 `pip install`。
