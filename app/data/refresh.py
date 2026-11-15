"""Refresh the SQLite data cache from the public APIs.  Usage: python -m app.data.refresh [--hub DEN] [--force]

- FEMA declarations are re-fetched on every refresh (14 small calls): new declarations move scores.
- Weather is re-fetched only when the cached window differs from the current rolling window
  (each January, or after a restart restored an older seed), or with force=True.
- Every run is recorded in `refresh_log` (local cache). The alert check uses it to refuse comparing
  scores computed from stale data (e.g. a freshly restored seed) against the durable snapshot.
"""
from __future__ import annotations

import argparse
import json
import os
import threading
import time
from datetime import datetime, timezone

from app.config import analysis_window, load_hubs
from app.data import fema, open_meteo
from app.db import connect, now_iso

_lock = threading.Lock()
_last: dict | None = None


def is_running() -> bool:
    return _lock.locked()


def last_result() -> dict | None:
    return _last


def record_refresh(started_at: str, finished_at: str, ok: bool, window: str, report: list[dict]) -> None:
    with connect() as conn:
        conn.execute("INSERT INTO refresh_log (started_at, finished_at, ok, window, report) VALUES (?,?,?,?,?)",
                     (started_at, finished_at, int(ok), window, json.dumps(report)))


def last_successful_refresh() -> str | None:
    """finished_at of the last fully successful refresh *of this cache* (None after a restart)."""
    with connect() as conn:
        row = conn.execute("SELECT max(finished_at) FROM refresh_log WHERE ok=1").fetchone()
    return row[0] if row else None


def data_age_hours() -> float | None:
    last = last_successful_refresh()
    if not last:
        return None
    return (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() / 3600


def window_changed() -> bool:
    """True if any hub's cached weather doesn't cover exactly the current rolling window."""
    win = analysis_window()
    with connect() as conn:
        rows = {r["hub_id"]: (r["start_date"], r["end_date"])
                for r in conn.execute("SELECT hub_id, start_date, end_date FROM weather_meta")}
    return any(rows.get(h.id) != (win.weather_start, win.weather_end) for h in load_hubs())


def refresh(hub_ids: list[str] | None = None, force: bool = False, pause_s: float | None = None) -> dict:
    """Returns {status, ok, started_at, finished_at, window, report}. One refresh at a time."""
    global _last
    if not _lock.acquire(blocking=False):
        return {"status": "already_running", "ok": False}
    try:
        if pause_s is None:
            pause_s = float(os.getenv("REFRESH_PAUSE_S", "1.0"))
        win = analysis_window()
        start, end = win.weather_start, win.weather_end
        started = now_iso()
        report = []
        hubs = [h for h in load_hubs() if not hub_ids or h.id in hub_ids]
        with connect() as conn:
            for hub in hubs:
                entry = {"hub_id": hub.id}
                have = conn.execute("SELECT start_date, end_date FROM weather_meta WHERE hub_id=?", (hub.id,)).fetchone()
                if not force and have and (have["start_date"], have["end_date"]) == (start, end):
                    entry["weather_days"] = "cached"
                else:
                    try:
                        entry["weather_days"] = open_meteo.store(conn, hub, open_meteo.fetch(hub, start, end), start, end)
                    except Exception as e:  # keep going: partial refresh beats none
                        entry["weather_error"] = str(e)[:200]
                try:
                    entry["fema_disasters"] = fema.store(conn, hub, fema.fetch(hub))
                except Exception as e:
                    entry["fema_error"] = str(e)[:200]
                conn.commit()
                report.append(entry)
                if pause_s:
                    time.sleep(pause_s)  # be polite to free APIs
        ok = bool(report) and not any("weather_error" in e or "fema_error" in e for e in report)
        finished = now_iso()
        record_refresh(started, finished, ok, win.label, report)
        from app import tools
        tools.invalidate_scores()
        _last = {"status": "done", "ok": ok, "started_at": started, "finished_at": finished,
                 "window": win.label, "report": report}
        return _last
    finally:
        _lock.release()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--hub", action="append")
    ap.add_argument("--force", action="store_true", help="re-fetch weather even if cached")
    args = ap.parse_args()
    res = refresh(args.hub, args.force)
    for row in res.get("report", []):
        print(row)
    print("ok:", res.get("ok"), "| window:", res.get("window"))
