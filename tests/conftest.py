import pytest


@pytest.fixture(autouse=True)
def _deterministic_env(monkeypatch):
    """Every test: pinned analysis window and no cloud database, whatever the developer's .env says."""
    monkeypatch.setenv("ANALYSIS_END_YEAR", "2025")
    monkeypatch.delenv("DATABASE_URL", raising=False)


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Point the app at an empty throwaway SQLite file."""
    p = tmp_path / "test.sqlite"
    monkeypatch.setenv("DB_PATH", str(p))
    return p
