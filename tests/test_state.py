"""Durable state store: same behaviour on SQLite (default, no network) and Postgres (DATABASE_URL).

The Postgres round-trips run only when TEST_DATABASE_URL points at a throwaway database, e.g.
  docker run -d --rm -p 55432:5432 -e POSTGRES_PASSWORD=t postgres:16
  TEST_DATABASE_URL=postgresql://postgres:t@localhost:55432/postgres uv run pytest tests/test_state.py
"""
import os

import pytest

from app import state

PG_URL = os.getenv("TEST_DATABASE_URL")


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=pytest.mark.skipif(
    not PG_URL, reason="set TEST_DATABASE_URL to run against a real Postgres"))])
def store(request, tmp_db, monkeypatch):
    if request.param == "postgres":
        monkeypatch.setenv("DATABASE_URL", PG_URL)
        import psycopg
        with psycopg.connect(PG_URL, autocommit=True) as c:      # clean slate in the throwaway DB
            for t in ("messages", "sessions", "scores_snapshot", "alerts"):
                c.execute(f"DROP TABLE IF EXISTS {t}")
        state._initialised.clear()
    assert state.backend_name() == request.param
    return request.param


def test_snapshot_round_trip(store):
    assert state.last_snapshot() == (None, {})
    state.save_snapshot("2026-11-15T10:00:00+00:00", [("MIA", "wind", 51.9, "moderate"), ("MIA", "composite", 22.7, "low")])
    state.save_snapshot("2026-11-15T16:00:00+00:00", [("MIA", "wind", 57.1, "moderate"), ("MIA", "composite", 24.0, "low")])
    at, snap = state.last_snapshot()
    assert at == "2026-11-15T16:00:00+00:00" and snap[("MIA", "wind")] == (57.1, "moderate")
    st = state.status()
    assert st["last_snapshot_rows"] == 2 and st["snapshots_total_rows"] == 4 and st["state_backend"] == store


def test_alert_log_and_nws_dedupe(store):
    assert not state.nws_alert_seen("ATL", "urn:nws:1")
    state.log_alert("2026-11-15T10:00:00+00:00", "ATL", "nws_alert", {"nws": {"event": "Freeze Warning"}},
                    "urn:nws:1", True, None)
    state.log_alert("2026-11-15T10:00:00+00:00", "PHX", "score_change", {"delta": 15.0}, None, False, "no url")
    assert state.nws_alert_seen("ATL", "urn:nws:1") and not state.nws_alert_seen("MIA", "urn:nws:1")
    rows = state.list_alerts(10)
    assert [r["hub_id"] for r in rows] == ["PHX", "ATL"] and rows[1]["payload"]["nws"]["event"] == "Freeze Warning"


def test_chat_history_round_trip(store):
    state.save_turn("s1", "Rank Midwest by flood", '{"answer": "..."}', "2026-11-15T10:00:00+00:00")
    state.save_turn("s1", "And Chicago?", '{"answer": "CHI"}', "2026-11-15T10:01:00+00:00")  # same session: no dup error
    h = state.load_history("s1", 3)  # last 3 messages -> leading assistant message dropped
    assert [m["role"] for m in h] == ["user", "assistant"] and h[0]["content"] == "And Chicago?"
    assert state.status()["chat_sessions"] == 1


def test_backend_selection(monkeypatch):
    assert state.backend_name() == "sqlite"                          # conftest removes DATABASE_URL
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@db.example/neondb?sslmode=require")
    assert state.backend_name() == "postgres"
    monkeypatch.setenv("DATABASE_URL", "postgres://u:p@h/db")
    assert state.backend_name() == "postgres"


def test_postgres_path_with_fake_driver(monkeypatch):
    """No network: psycopg.connect is replaced; checks DDL and placeholder translation."""
    import psycopg
    executed = []

    class Cur:
        def execute(self, sql, params=()):
            executed.append((sql, params))

        def executemany(self, sql, rows):
            executed.append((sql, list(rows)))

        def fetchall(self):
            return [{"t": None, "n": 0}]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class Conn:
        def cursor(self):
            return Cur()

        def commit(self):
            executed.append(("COMMIT", ()))

        def close(self):
            pass

    monkeypatch.setenv("DATABASE_URL", "postgresql://fake/db")
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: Conn())
    state._initialised.discard("postgresql://fake/db")
    state.save_snapshot("t", [("MIA", "wind", 1.0, "low")])
    sqls = [s for s, _ in executed]
    assert any("BIGSERIAL" in s for s in sqls)                       # Postgres DDL, not SQLite's
    insert = next(s for s in sqls if s.startswith("INSERT INTO scores_snapshot"))
    assert "%s" in insert and "?" not in insert
    assert sqls[-1] == "COMMIT"
