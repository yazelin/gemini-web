import pytest

from src import admin_db
from src.config import settings


@pytest.fixture(autouse=True)
def _isolated_admin_storage(tmp_path, monkeypatch):
    """Every test gets its own sqlite db + generated-images dir — nothing
    should touch the real ~/.gemini-web/ on the machine running tests."""
    monkeypatch.setattr(admin_db, "_DB_PATH", tmp_path / "admin.db")
    monkeypatch.setattr(settings, "generated_dir", str(tmp_path / "generated"))
