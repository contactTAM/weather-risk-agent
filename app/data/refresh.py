"""Fetch all historical data into the SQLite cache.  Usage: python -m app.data.refresh [--hub DEN]"""
from __future__ import annotations

import argparse
import time

from app.config import analysis_window, load_hubs
from app.data import fema, open_meteo
from app.db import connect


def refresh(hub_ids: list[str] | None = None, force: bool = False) -> list[dict]:
    win = analysis_window()
    start, end = win.weather_start, win.weather_end
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
                entry["fema_error"] = str(e)
            conn.commit()
            report.append(entry)
            time.sleep(1.0)  # be polite to free APIs
    return report


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--hub", action="append")
    ap.add_argument("--force", action="store_true", help="re-fetch weather even if cached")
    args = ap.parse_args()
    for row in refresh(args.hub, args.force):
        print(row)
