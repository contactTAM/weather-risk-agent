"""SQLite data cache: raw API responses, derived daily weather, FEMA rows (rebuildable from seed + refresh).
Durable state (snapshots, alerts, chat memory) lives in app/state.py.

Why SQLite: single file, zero ops, ships with Python, makes the demo work offline.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from app.config import db_path

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_responses (
    source TEXT NOT NULL, key TEXT NOT NULL, url TEXT NOT NULL,
    fetched_at TEXT NOT NULL, body TEXT NOT NULL,
    PRIMARY KEY (source, key)
);
CREATE TABLE IF NOT EXISTS daily_weather (
    hub_id TEXT NOT NULL, date TEXT NOT NULL,
    snowfall_cm REAL, precip_mm REAL, tmin_c REAL, tmax_c REAL, gust_kmh REAL,
    PRIMARY KEY (hub_id, date)
);
CREATE TABLE IF NOT EXISTS weather_meta (
    hub_id TEXT PRIMARY KEY, grid_lat REAL, grid_lon REAL, elevation_m REAL,
    start_date TEXT, end_date TEXT, fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS fema_declarations (
    hub_id TEXT NOT NULL, disaster_number INTEGER NOT NULL, incident_type TEXT NOT NULL,
    declaration_type TEXT, declaration_date TEXT NOT NULL, designated_area TEXT,
    PRIMARY KEY (hub_id, disaster_number)
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    p = path or db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def save_raw(conn: sqlite3.Connection, source: str, key: str, url: str, body: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO raw_responses VALUES (?,?,?,?,?)",
        (source, key, url, now_iso(), json.dumps(body)),
    )


def load_raw(conn: sqlite3.Connection, source: str, key: str) -> tuple[dict, str] | None:
    row = conn.execute(
        "SELECT body, fetched_at FROM raw_responses WHERE source=? AND key=?", (source, key)
    ).fetchone()
    return (json.loads(row["body"]), row["fetched_at"]) if row else None
