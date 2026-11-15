"""Turn daily weather rows + FEMA rows into indicator values. Pure functions + one DB loader."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from app.data.fema import hazard_of

# Daily thresholds (documented in ARCHITECTURE.md)
ANY_SNOW_CM = 0.1      # "had snowfall" = any measurable snowfall
SNOW_DAY_CM = 1.0
HEAVY_SNOW_CM = 10.0
FREEZE_C = 0.0
GUST_STRONG = 60.0
GUST_SEVERE = 90.0
RAIN_HEAVY = 25.0
RAIN_EXTREME = 50.0
HEAT_C = 35.0

DAY_FLAGS = {
    "snowfall_any_days": lambda r: (r["snowfall_cm"] or 0) >= ANY_SNOW_CM,
    "snow_days_1cm": lambda r: (r["snowfall_cm"] or 0) >= SNOW_DAY_CM,
    "heavy_snow_days": lambda r: (r["snowfall_cm"] or 0) >= HEAVY_SNOW_CM,
    "freeze_days": lambda r: r["tmin_c"] is not None and r["tmin_c"] <= FREEZE_C,
    "gust60_days": lambda r: (r["gust_kmh"] or 0) >= GUST_STRONG,
    "gust90_days": lambda r: (r["gust_kmh"] or 0) >= GUST_SEVERE,
    "rain25_days": lambda r: (r["precip_mm"] or 0) >= RAIN_HEAVY,
    "rain50_days": lambda r: (r["precip_mm"] or 0) >= RAIN_EXTREME,
    "heat_days": lambda r: r["tmax_c"] is not None and r["tmax_c"] >= HEAT_C,
}
COLUMNS = ("snowfall_cm", "precip_mm", "tmin_c", "tmax_c", "gust_kmh")


@dataclass
class HubInputs:
    hub_id: str
    daily: list[dict]
    fema: list[dict]
    grid_km: float | None = None
    meta: dict = field(default_factory=dict)


def yearly_counts(daily: list[dict]) -> dict[int, dict]:
    """{year: {flag: count, ..., n_days, missing_days}}"""
    out: dict[int, dict] = defaultdict(lambda: {k: 0 for k in DAY_FLAGS} | {"n_days": 0, "missing_days": 0})
    for r in daily:
        y = int(r["date"][:4])
        b = out[y]
        b["n_days"] += 1
        if any(r[c] is None for c in COLUMNS):
            b["missing_days"] += 1
        for k, f in DAY_FLAGS.items():
            b[k] += int(f(r))
    return dict(out)


def baseline_indicators(daily: list[dict], fema: list[dict], years: list[int]) -> dict[str, float]:
    """Yearly averages of day counts over `years`, plus FEMA counts per hazard group."""
    yc = yearly_counts(daily)
    present = [y for y in years if y in yc]
    ind: dict[str, float] = {}
    for k in DAY_FLAGS:
        ind[k] = sum(yc[y][k] for y in present) / len(present) if present else 0.0
    for hz in ("winter", "wind", "flood"):
        ind[f"fema_{hz}"] = float(sum(1 for d in fema if hazard_of(d["incident_type"]) == hz))
    return ind


def weather_stat(daily: list[dict], metric: str, year: int) -> dict:
    """e.g. metric='snowfall_any_days', year=2025 -> {count, days, pct}."""
    if metric not in DAY_FLAGS:
        raise ValueError(f"unknown metric {metric}; choose from {sorted(DAY_FLAGS)}")
    yc = yearly_counts(daily).get(year)
    if not yc or yc["n_days"] == 0:
        return {"metric": metric, "year": year, "count": None, "days": 0, "pct": None, "available": False}
    return {"metric": metric, "year": year, "count": yc[metric], "days": yc["n_days"],
            "pct": round(100.0 * yc[metric] / yc["n_days"], 1), "missing_days": yc["missing_days"],
            "available": True}


def load_inputs(conn, hub) -> HubInputs:
    import math
    daily = [dict(r) for r in conn.execute(
        "SELECT date, snowfall_cm, precip_mm, tmin_c, tmax_c, gust_kmh FROM daily_weather WHERE hub_id=? ORDER BY date",
        (hub.id,))]
    fema = [dict(r) for r in conn.execute(
        "SELECT disaster_number, incident_type, declaration_date FROM fema_declarations WHERE hub_id=?", (hub.id,))]
    m = conn.execute("SELECT * FROM weather_meta WHERE hub_id=?", (hub.id,)).fetchone()
    grid_km = None
    if m and m["grid_lat"] is not None:
        la1, lo1, la2, lo2 = map(math.radians, (hub.lat, hub.lon, m["grid_lat"], m["grid_lon"]))
        a = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
        grid_km = round(6371 * 2 * math.asin(math.sqrt(a)), 1)
    return HubInputs(hub.id, daily, fema, grid_km, dict(m) if m else {})
