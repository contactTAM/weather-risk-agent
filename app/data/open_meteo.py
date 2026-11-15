"""Open-Meteo Historical Weather API (archive). Free, no key.

Docs: https://open-meteo.com/en/docs/historical-weather-api
Verified response shape: {latitude, longitude, elevation, daily_units, daily: {time: [...], <var>: [...]}}
Units: snowfall_sum cm, precipitation_sum mm, temperature °C, wind_gusts_10m_max km/h.
"""
from __future__ import annotations

import sqlite3

from app.config import Hub
from app.data.http import get_json
from app.db import now_iso, save_raw

URL = "https://archive-api.open-meteo.com/v1/archive"
DAILY_VARS = ["snowfall_sum", "precipitation_sum", "temperature_2m_min",
              "temperature_2m_max", "wind_gusts_10m_max"]


def fetch(hub: Hub, start: str, end: str) -> dict:
    params = {"latitude": hub.lat, "longitude": hub.lon, "start_date": start, "end_date": end,
              "daily": ",".join(DAILY_VARS), "timezone": hub.timezone}
    return get_json(URL, params=params)


def parse_daily(body: dict) -> list[tuple]:
    d = body["daily"]
    cols = [d[v] for v in DAILY_VARS]
    return [(t, *vals) for t, *vals in zip(d["time"], *cols)]


def store(conn: sqlite3.Connection, hub: Hub, body: dict, start: str, end: str) -> int:
    save_raw(conn, "open_meteo", f"{hub.id}:{start}:{end}", URL, body)
    rows = parse_daily(body)
    conn.execute("DELETE FROM daily_weather WHERE hub_id=?", (hub.id,))
    conn.executemany(
        "INSERT INTO daily_weather VALUES (?,?,?,?,?,?,?)", [(hub.id, *r) for r in rows]
    )
    conn.execute(
        "INSERT OR REPLACE INTO weather_meta VALUES (?,?,?,?,?,?,?)",
        (hub.id, body.get("latitude"), body.get("longitude"), body.get("elevation"), start, end, now_iso()),
    )
    return len(rows)
