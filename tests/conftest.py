import pytest


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Point the app at an empty throwaway SQLite file."""
    p = tmp_path / "test.sqlite"
    monkeypatch.setenv("DB_PATH", str(p))
    return p
