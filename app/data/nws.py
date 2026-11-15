"""NWS active alerts (api.weather.gov). Free, no key, requires a User-Agent header.

Verified shape: GeoJSON FeatureCollection; features[].properties.{event, severity, urgency, headline, onset, ends}.
Live data: cached for 10 minutes in raw_responses so repeated questions don't hammer the API.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.config import Hub, nws_user_agent
from app.data.http import get_json
from app.db import connect, load_raw, save_raw

URL = "https://api.weather.gov/alerts/active"
CACHE_MINUTES = 10


def parse(body: dict) -> list[dict]:
    out = []
    for f in body.get("features", []):
        p = f.get("properties", {})
        out.append({k: p.get(k) for k in ("event", "severity", "urgency", "certainty",
                                          "headline", "onset", "ends", "senderName")})
    return out


def active_alerts(hub: Hub, use_cache: bool = True) -> dict:
    """Returns {alerts: [...], fetched_at, source, stale: bool}. Falls back to cache if offline."""
    with connect() as conn:
        cached = load_raw(conn, "nws", hub.id)
        if use_cache and cached:
            body, fetched_at = cached
            age = datetime.now(timezone.utc) - datetime.fromisoformat(fetched_at)
            if age < timedelta(minutes=CACHE_MINUTES):
                return {"alerts": parse(body), "fetched_at": fetched_at, "stale": False}
        try:
            body = get_json(URL, params={"point": f"{hub.lat},{hub.lon}"},
                            headers={"User-Agent": nws_user_agent(), "Accept": "application/geo+json"},
                            retries=1, timeout=15)
            save_raw(conn, "nws", hub.id, URL, body)
            return {"alerts": parse(body), "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "stale": False}
        except RuntimeError as e:
            if cached:
                return {"alerts": parse(cached[0]), "fetched_at": cached[1], "stale": True, "error": str(e)}
            return {"alerts": [], "fetched_at": None, "stale": True, "error": str(e)}
