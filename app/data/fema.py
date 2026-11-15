"""OpenFEMA Disaster Declarations Summaries v2. Free, no key.

Docs: https://www.fema.gov/openfema-data-page/disaster-declarations-summaries-v2
One row per (disaster, designated area). We filter to the hub's county and dedupe by disasterNumber.
Statewide/tribal rows (fipsCountyCode '000') are excluded: many are tribal-nation declarations
unrelated to the hub city (verified: Arizona has 20 statewide 'Severe Storm' rows).
"""
from __future__ import annotations

import sqlite3

from app.config import Hub, analysis_window
from app.data.http import get_json
from app.db import save_raw

URL = "https://www.fema.gov/api/open/v2/DisasterDeclarationsSummaries"
SELECT = "disasterNumber,incidentType,declarationType,declarationDate,designatedArea,fipsStateCode,fipsCountyCode"

# Weather-related incident types, grouped by hazard. Everything else (Biological, Fire, ...) is ignored.
HAZARD_GROUPS: dict[str, set[str]] = {
    "winter": {"Winter Storm", "Snowstorm", "Severe Ice Storm", "Freezing"},
    "wind": {"Hurricane", "Tropical Storm", "Typhoon", "Tornado", "Straight-Line Winds"},
    "flood": {"Flood", "Severe Storm", "Coastal Storm", "Dam/Levee Break"},
}


def fetch(hub: Hub) -> dict:
    state, county = hub.county_fips[:2], hub.county_fips[2:]
    win = analysis_window()
    flt = (f"fipsStateCode eq '{state}' and fipsCountyCode eq '{county}' "
           f"and declarationDate ge '{win.fema_start}-01-01T00:00:00.000Z' "
           f"and declarationDate le '{win.fema_end}-12-31T23:59:59.999Z'")
    return get_json(URL, params={"$filter": flt, "$select": SELECT, "$top": 1000})


def parse(body: dict) -> list[dict]:
    seen: dict[int, dict] = {}
    for r in body.get("DisasterDeclarationsSummaries", []):
        seen.setdefault(r["disasterNumber"], r)  # dedupe: one disaster counted once per county
    return list(seen.values())


def store(conn: sqlite3.Connection, hub: Hub, body: dict) -> int:
    save_raw(conn, "fema", hub.id, URL, body)
    rows = parse(body)
    conn.execute("DELETE FROM fema_declarations WHERE hub_id=?", (hub.id,))
    conn.executemany(
        "INSERT INTO fema_declarations VALUES (?,?,?,?,?,?)",
        [(hub.id, r["disasterNumber"], r["incidentType"], r.get("declarationType"),
          r["declarationDate"][:10], r.get("designatedArea")) for r in rows],
    )
    return len(rows)


def hazard_of(incident_type: str) -> str | None:
    for hazard, types in HAZARD_GROUPS.items():
        if incident_type in types:
            return hazard
    return None
