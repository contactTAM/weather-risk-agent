"""Durable application state: score snapshots, alert log (incl. NWS dedupe ids) and chat memory.

Backend:
  DATABASE_URL set   -> Postgres (e.g. Neon free tier): survives restarts on an ephemeral host.
  DATABASE_URL unset -> the local SQLite file (same file as the data cache): local runs and tests,
                        no network.
The data cache itself (daily weather, FEMA rows) stays in SQLite on purpose: it is rebuilt from the
shipped seed plus a refresh, so it doesn't need to survive restarts.

SQL is written once with "?" placeholders and translated to "%s" for psycopg. Only the DDL differs.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from typing import Iterator

from app.config import db_path

_SQLITE_DDL = """
CREATE TABLE IF NOT EXISTS sessions (session_id TEXT PRIMARY KEY, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
    role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS scores_snapshot (
    id INTEGER PRIMARY KEY AUTOINCREMENT, computed_at TEXT NOT NULL,
    hub_id TEXT NOT NULL, hazard TEXT NOT NULL, score REAL NOT NULL, level TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, hub_id TEXT NOT NULL,
    kind TEXT NOT NULL, payload TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0, delivery_error TEXT);
"""

_PG_DDL = [
    "CREATE TABLE IF NOT EXISTS sessions (session_id TEXT PRIMARY KEY, created_at TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS messages (
        id BIGSERIAL PRIMARY KEY, session_id TEXT NOT NULL,
        role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS messages_session_idx ON messages (session_id, id)",
    """CREATE TABLE IF NOT EXISTS scores_snapshot (
        id BIGSERIAL PRIMARY KEY, computed_at TEXT NOT NULL,
        hub_id TEXT NOT NULL, hazard TEXT NOT NULL, score DOUBLE PRECISION NOT NULL, level TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS scores_snapshot_time_idx ON scores_snapshot (computed_at)",
    """CREATE TABLE IF NOT EXISTS alerts (
        id BIGSERIAL PRIMARY KEY, created_at TEXT NOT NULL, hub_id TEXT NOT NULL,
        kind TEXT NOT NULL, payload TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0, delivery_error TEXT)""",
    # migration: explicit dedupe key instead of LIKE on the payload
    "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS ref TEXT",
    "CREATE INDEX IF NOT EXISTS alerts_ref_idx ON alerts (kind, hub_id, ref)",
]

_initialised: set[str] = set()


def backend_name() -> str:
    url = os.getenv("DATABASE_URL", "").strip()
    return "postgres" if url.startswith(("postgres://", "postgresql://")) else "sqlite"


def _pg_sql(sql: str) -> str:
    return sql.replace("?", "%s")


class _Conn:
    """Tiny adapter so the functions below are backend-agnostic."""

    def __init__(self, raw, kind: str):
        self.raw, self.kind = raw, kind

    def execute(self, sql: str, params: tuple = ()):
        if self.kind == "postgres":
            cur = self.raw.cursor()
            cur.execute(_pg_sql(sql), params)
            return cur
        return self.raw.execute(sql, params)

    def executemany(self, sql: str, rows: list[tuple]) -> None:
        if self.kind == "postgres":
            self.raw.cursor().executemany(_pg_sql(sql), rows)
        else:
            self.raw.executemany(sql, rows)


def _init_sqlite(conn: sqlite3.Connection) -> None:
    conn.executescript(_SQLITE_DDL)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(alerts)")}
    if "ref" not in cols:  # migration for caches created before the ref column existed
        conn.execute("ALTER TABLE alerts ADD COLUMN ref TEXT")


@contextmanager
def connect() -> Iterator[_Conn]:
    kind = backend_name()
    if kind == "postgres":
        import psycopg
        from psycopg.rows import dict_row
        url = os.environ["DATABASE_URL"].strip()
        raw = psycopg.connect(url, row_factory=dict_row, connect_timeout=15)
        try:
            if url not in _initialised:
                with raw.cursor() as cur:
                    for stmt in _PG_DDL:
                        cur.execute(stmt)
                _initialised.add(url)
            yield _Conn(raw, kind)
            raw.commit()
        finally:
            raw.close()
    else:
        path = db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = sqlite3.connect(path)
        raw.row_factory = sqlite3.Row
        try:
            _init_sqlite(raw)
            yield _Conn(raw, kind)
            raw.commit()
        finally:
            raw.close()


def _rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


# ---------------- score snapshots ----------------
def save_snapshot(computed_at: str, rows: list[tuple[str, str, float, str]]) -> None:
    """rows: (hub_id, hazard, score, level)."""
    with connect() as c:
        c.executemany("INSERT INTO scores_snapshot (computed_at, hub_id, hazard, score, level) VALUES (?,?,?,?,?)",
                      [(computed_at, *r) for r in rows])


def last_snapshot() -> tuple[str | None, dict[tuple[str, str], tuple[float, str]]]:
    with connect() as c:
        row = _rows(c.execute("SELECT max(computed_at) AS t FROM scores_snapshot"))[0]
        if not row["t"]:
            return None, {}
        rows = _rows(c.execute("SELECT hub_id, hazard, score, level FROM scores_snapshot WHERE computed_at=?",
                               (row["t"],)))
    return row["t"], {(r["hub_id"], r["hazard"]): (float(r["score"]), r["level"]) for r in rows}


# ---------------- alerts ----------------
def log_alert(created_at: str, hub_id: str, kind: str, payload: dict, ref: str | None,
              delivered: bool, error: str | None) -> None:
    with connect() as c:
        c.execute("INSERT INTO alerts (created_at, hub_id, kind, ref, payload, delivered, delivery_error) "
                  "VALUES (?,?,?,?,?,?,?)",
                  (created_at, hub_id, kind, ref, json.dumps(payload), int(delivered), error))


def nws_alert_seen(hub_id: str, ref: str) -> bool:
    with connect() as c:
        return bool(_rows(c.execute("SELECT 1 AS x FROM alerts WHERE kind='nws_alert' AND hub_id=? AND ref=? LIMIT 1",
                                    (hub_id, ref))))


def list_alerts(limit: int = 20) -> list[dict]:
    with connect() as c:
        rows = _rows(c.execute("SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (limit,)))
    for r in rows:
        r["payload"] = json.loads(r["payload"])
    return rows


# ---------------- chat memory ----------------
def save_turn(session_id: str, user_msg: str, answer_text: str, ts: str) -> None:
    with connect() as c:
        c.execute("INSERT INTO sessions (session_id, created_at) VALUES (?,?) ON CONFLICT (session_id) DO NOTHING",
                  (session_id, ts))
        c.executemany("INSERT INTO messages (session_id, role, content, created_at) VALUES (?,?,?,?)",
                      [(session_id, "user", user_msg, ts), (session_id, "assistant", answer_text, ts)])


def load_history(session_id: str, max_messages: int) -> list[dict]:
    with connect() as c:
        rows = _rows(c.execute("SELECT role, content FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?",
                               (session_id, max_messages)))
    msgs = [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]
    while msgs and msgs[0]["role"] != "user":
        msgs.pop(0)
    return msgs


# ---------------- status ----------------
def status() -> dict:
    with connect() as c:
        snap = _rows(c.execute("SELECT max(computed_at) AS t, count(*) AS n FROM scores_snapshot"))[0]
        last_rows = 0
        if snap["t"]:
            last_rows = _rows(c.execute("SELECT count(*) AS n FROM scores_snapshot WHERE computed_at=?",
                                        (snap["t"],)))[0]["n"]
        alerts = _rows(c.execute("SELECT count(*) AS n FROM alerts"))[0]["n"]
        sessions = _rows(c.execute("SELECT count(*) AS n FROM sessions"))[0]["n"]
    return {"state_backend": backend_name(), "last_snapshot_at": snap["t"], "last_snapshot_rows": int(last_rows),
            "snapshots_total_rows": int(snap["n"]), "alerts_logged": int(alerts), "chat_sessions": int(sessions)}
